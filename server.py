"""Remote Claude Code orchestrator + Telegram bot + scheduled jobs.

HTTP endpoints (browser session cookie, or X-API-Key header / ?api_key= query for service calls):
  POST   /sessions               create session
  GET    /sessions               list active
  DELETE /sessions/{id}          stop
  POST   /sessions/{id}/query    NDJSON stream

  POST   /jobs/tick              run all due jobs (call from cron every minute)

GET / serves web UI.
"""
import asyncio
import base64
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import struct
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import AsyncIterator

from croniter import croniter
from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url, options_to_json_dict
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

import db
import sessions as sess
from sessions import SESSIONS, create_session, kill_session, run_one_shot, stream_query

API_KEY = os.environ.get("ORCHESTRATOR_API_KEY", "")
if not API_KEY:
    raise RuntimeError("Set ORCHESTRATOR_API_KEY before launch.")

WEB_AUTH_USERNAME = os.environ.get("WEB_AUTH_USERNAME", os.environ.get("TELEGRAM_USER", "admin")).strip()
WEB_AUTH_PASSWORD = os.environ.get("WEB_AUTH_PASSWORD", "")
WEB_AUTH_PASSWORD_HASH = os.environ.get("WEB_AUTH_PASSWORD_HASH", "")
WEB_AUTH_TOTP_SECRET = os.environ.get("WEB_AUTH_TOTP_SECRET", "").replace(" ", "")
WEB_SESSION_SECRET = os.environ.get("WEB_SESSION_SECRET", API_KEY)
WEB_SESSION_TTL_SECONDS = int(os.environ.get("WEB_SESSION_TTL_SECONDS", "43200"))
WEB_AUTH_RP_ID = os.environ.get("WEB_AUTH_RP_ID", "")
WEB_AUTH_RP_NAME = os.environ.get("WEB_AUTH_RP_NAME", "Orchestrator")
WEB_AUTH_ORIGIN = os.environ.get("WEB_AUTH_ORIGIN", "")
SESSION_COOKIE = "orchestrator_session"
USERNAME_RE = re.compile(r"^[a-z0-9_-]{1,64}$")
WEBAUTHN_CHALLENGES: dict[str, dict] = {}
RUNNING_JOBS: set[int] = set()

if not WEB_AUTH_USERNAME:
    raise RuntimeError("Set WEB_AUTH_USERNAME before launch.")

ENABLE_BOT = os.environ.get("TELEGRAM_BOT_TOKEN", "") != ""

LOGIN_RATE_LIMIT = int(os.environ.get("LOGIN_RATE_LIMIT", "5"))
LOGIN_RATE_WINDOW = int(os.environ.get("LOGIN_RATE_WINDOW", "60"))
_LOGIN_ATTEMPTS: dict[str, deque] = defaultdict(deque)

# Internal-only callers for /jobs/tick. Defaults to Kubernetes pod + service
# CIDRs plus loopback. Override with INTERNAL_CIDRS env (comma-separated).
_DEFAULT_INTERNAL_CIDRS = "10.42.0.0/16,10.43.0.0/16,127.0.0.0/8,::1/128"
INTERNAL_CIDRS = [
    ipaddress.ip_network(c.strip())
    for c in os.environ.get("INTERNAL_CIDRS", _DEFAULT_INTERNAL_CIDRS).split(",")
    if c.strip()
]


def _client_ip(request: Request) -> str:
    # Trust the immediate peer. We do NOT honor X-Forwarded-For for the
    # internal-only check, because clients could spoof it; only Traefik can
    # spoof the TCP peer, which is itself an internal CIDR.
    return (request.client.host if request.client else "") or ""


def _is_internal_ip(ip: str) -> bool:
    if not ip:
        return False
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in net for net in INTERNAL_CIDRS)


def _real_client_ip(request: Request) -> str:
    """Return the trusted source IP for `require_internal`.

    The TCP peer of any request through Traefik is the Traefik pod, which sits
    inside the cluster pod CIDR — so peer alone would mark every external HTTP
    request as internal. We treat the peer as a trusted proxy only when it
    itself is internal, then take the leftmost X-Forwarded-For entry as the
    real client. External callers cannot reach the orchestrator without going
    through Traefik (no NodePort, no LoadBalancer), so this is sound.
    """
    peer = _client_ip(request)
    xff = request.headers.get("x-forwarded-for", "")
    if peer and _is_internal_ip(peer) and xff:
        return xff.split(",")[0].strip() or peer
    return peer


def require_internal(request: Request) -> None:
    if not _is_internal_ip(_real_client_ip(request)):
        raise HTTPException(status_code=403, detail="internal-only endpoint")


def _rate_limit_login(request: Request) -> None:
    # XFF is fine for rate-limit keying (worst case: attacker rotates XFF and
    # still costs them, while honest users behind NAT share a bucket).
    key = (request.headers.get("x-forwarded-for") or _client_ip(request)).split(",")[0].strip()
    if not key:
        return
    now = time.time()
    window = _LOGIN_ATTEMPTS[key]
    cutoff = now - LOGIN_RATE_WINDOW
    while window and window[0] < cutoff:
        window.popleft()
    if len(window) >= LOGIN_RATE_LIMIT:
        raise HTTPException(status_code=429, detail="too many login attempts")
    window.append(now)


def _check_api_key(
    x_api_key: str = Header(default=""),
    api_key: str = Query(default=""),
) -> bool:
    supplied = x_api_key or api_key
    return bool(supplied) and secrets.compare_digest(supplied, API_KEY)


def check_api_key(
    x_api_key: str = Header(default=""),
    api_key: str = Query(default=""),
) -> None:
    if not _check_api_key(x_api_key, api_key):
        raise HTTPException(status_code=401, detail="bad api key")


def _require_header_user(x_user: str = Header(default="")) -> str:
    if not x_user.strip():
        raise HTTPException(status_code=400, detail="X-User header required")
    return x_user.strip()


def _hash_password(password: str, *, iterations: int = 260_000) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return "pbkdf2_sha256${}${}${}".format(
        iterations,
        base64.b64encode(salt).decode(),
        base64.b64encode(digest).decode(),
    )


def _verify_password(password: str, password_hash: str) -> bool:
    if password_hash:
        try:
            scheme, iterations_s, salt_b64, hash_b64 = password_hash.split("$", 3)
            if scheme != "pbkdf2_sha256":
                return False
            expected = base64.b64decode(hash_b64)
            salt = base64.b64decode(salt_b64)
            actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, int(iterations_s))
            return secrets.compare_digest(actual, expected)
        except Exception:
            return False
    return False


def _bootstrap_admin_user() -> None:
    if db.count_web_users() > 0:
        return
    password_hash = WEB_AUTH_PASSWORD_HASH or (_hash_password(WEB_AUTH_PASSWORD) if WEB_AUTH_PASSWORD else "")
    if not password_hash:
        raise RuntimeError("Set WEB_AUTH_PASSWORD or WEB_AUTH_PASSWORD_HASH to bootstrap the first admin user.")
    db.create_web_user(
        username=WEB_AUTH_USERNAME,
        password_hash=password_hash,
        totp_secret=WEB_AUTH_TOTP_SECRET,
        is_admin=True,
    )
    print(f"[auth] bootstrapped admin web user {WEB_AUTH_USERNAME!r}")


def _totp_code(secret: str, counter: int) -> str:
    key = base64.b32decode(secret.upper(), casefold=True)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return f"{value % 1_000_000:06d}"


def _verify_totp(code: str, secret: str) -> bool:
    if not secret:
        return True
    clean = "".join(ch for ch in code if ch.isdigit())
    if len(clean) != 6:
        return False
    counter = int(time.time() // 30)
    return any(secrets.compare_digest(clean, _totp_code(secret, counter + skew)) for skew in (-1, 0, 1))


def _sign(data: str) -> str:
    return hmac.new(WEB_SESSION_SECRET.encode(), data.encode(), hashlib.sha256).hexdigest()


def _make_session_token(user: str) -> str:
    payload = {
        "user": user,
        "exp": int(time.time()) + WEB_SESSION_TTL_SECONDS,
        "nonce": secrets.token_urlsafe(12),
    }
    data = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
    return f"{data}.{_sign(data)}"


def _read_session_token(token: str) -> str | None:
    try:
        data, sig = token.split(".", 1)
        if not secrets.compare_digest(sig, _sign(data)):
            return None
        padded = data + ("=" * (-len(data) % 4))
        payload = json.loads(base64.urlsafe_b64decode(padded.encode()))
        if int(payload.get("exp", 0)) < int(time.time()):
            return None
        user = str(payload.get("user", "")).strip()
        return user or None
    except Exception:
        return None


def _request_origin(request: Request) -> str:
    if WEB_AUTH_ORIGIN:
        return WEB_AUTH_ORIGIN.rstrip("/")
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}"


def _rp_id(request: Request) -> str:
    if WEB_AUTH_RP_ID:
        return WEB_AUTH_RP_ID
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return host.split(":", 1)[0]


def _public_credential_descriptors(username: str | None = None) -> list[PublicKeyCredentialDescriptor]:
    return [
        PublicKeyCredentialDescriptor(id=base64url_to_bytes(c["credential_id"]))
        for c in db.list_webauthn_credentials(username)
    ]


def _public_user(user: dict) -> dict:
    return {
        "username": user["username"],
        "is_admin": user["is_admin"],
        "disabled": user["disabled"],
        "totp_enabled": user["totp_enabled"],
        "passkey_count": len(db.list_webauthn_credentials(user["username"])),
    }


def require_auth_user(
    x_api_key: str = Header(default=""),
    api_key: str = Query(default=""),
    x_user: str = Header(default=""),
    session_cookie: str = Cookie(default="", alias=SESSION_COOKIE),
) -> str:
    if _check_api_key(x_api_key, api_key):
        return _require_header_user(x_user)
    user = _read_session_token(session_cookie)
    record = db.get_web_user(user) if user else None
    if not record or record["disabled"]:
        raise HTTPException(status_code=401, detail="login required")
    return user


def require_admin_user(user: str = Depends(require_auth_user)) -> str:
    record = db.get_web_user(user)
    if not record or not record["is_admin"]:
        raise HTTPException(status_code=403, detail="admin required")
    return user


class CreateBody(BaseModel):
    cwd: str | None = None  # defaults to /opt/data/users/<user>/workspace
    system_prompt: str | None = None
    permission_mode: str = "acceptEdits"
    allowed_tools: list[str] | None = None
    max_turns: int | None = None
    model: str | None = None


class QueryBody(BaseModel):
    prompt: str


class LoginBody(BaseModel):
    username: str
    password: str
    totp: str | None = None


class CreateUserBody(BaseModel):
    username: str
    password: str
    is_admin: bool = False
    enable_totp: bool = True


class UpdateUserBody(BaseModel):
    password: str | None = None
    is_admin: bool | None = None
    disabled: bool | None = None
    reset_totp: bool = False
    clear_totp: bool = False


class PasskeyLoginOptionsBody(BaseModel):
    username: str | None = None


class PasskeyCredentialBody(BaseModel):
    challenge_id: str
    credential: dict


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    _bootstrap_admin_user()
    sess.load_persisted_sessions()
    print(f"[startup] loaded {len(SESSIONS)} persisted session(s) as ghosts")
    warmer_task = asyncio.create_task(sess.oauth_warmer_loop())
    bot_app = None
    bot_task = None
    if ENABLE_BOT:
        from bot import build_app, set_bot_commands
        bot_app = build_app()
        await bot_app.initialize()
        await set_bot_commands(bot_app)
        await bot_app.start()
        bot_task = asyncio.create_task(bot_app.updater.start_polling(drop_pending_updates=True))
        print("[bot] telegram polling started")

    try:
        yield
    finally:
        if bot_app:
            try:
                await bot_app.updater.stop()
            except Exception:
                pass
            try:
                await bot_app.stop()
            except Exception:
                pass
            try:
                await bot_app.shutdown()
            except Exception:
                pass
            if bot_task:
                bot_task.cancel()
        warmer_task.cancel()
        await sess.detach_all_sessions()


app = FastAPI(lifespan=lifespan)

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/auth/status")
async def auth_status(session_cookie: str = Cookie(default="", alias=SESSION_COOKIE)) -> dict:
    user = _read_session_token(session_cookie)
    record = db.get_web_user(user) if user else None
    if not record or record["disabled"]:
        return {"authenticated": False, "user": None, "is_admin": False, "totp_required": False}
    return {
        "authenticated": True,
        "user": user,
        "is_admin": record["is_admin"],
        "totp_required": record["totp_enabled"],
    }


@app.post("/auth/login", dependencies=[Depends(_rate_limit_login)])
async def auth_login(body: LoginBody, response: Response) -> dict:
    username = body.username.strip().lower()
    record = db.get_web_user(username)
    if not record or record["disabled"]:
        raise HTTPException(status_code=401, detail="bad username or password")
    if not _verify_password(body.password, record["password_hash"]):
        raise HTTPException(status_code=401, detail="bad username or password")
    if record["totp_secret"] and not _verify_totp(body.totp or "", record["totp_secret"]):
        raise HTTPException(status_code=401, detail="bad 2fa code")
    token = _make_session_token(username)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=WEB_SESSION_TTL_SECONDS,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )
    return {"authenticated": True, "user": username, "is_admin": record["is_admin"], "totp_required": record["totp_enabled"]}


@app.post("/auth/logout")
async def auth_logout(response: Response) -> dict:
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"authenticated": False}


@app.get("/auth/passkeys")
async def auth_list_passkeys(user: str = Depends(require_auth_user)) -> dict:
    credentials = db.list_webauthn_credentials(user)
    return {
        "passkeys": [
            {
                "credential_id": c["credential_id"],
                "device_type": c["device_type"],
                "backed_up": c["backed_up"],
                "created_at": c["created_at"],
                "updated_at": c["updated_at"],
            }
            for c in credentials
        ]
    }


@app.delete("/auth/passkeys/{credential_id}")
async def auth_delete_passkey(credential_id: str, user: str = Depends(require_auth_user)) -> dict:
    if not db.delete_webauthn_credential(credential_id, user):
        raise HTTPException(status_code=404, detail="no such passkey")
    return {"deleted": credential_id}


@app.post("/auth/passkeys/register/options")
async def auth_passkey_register_options(request: Request, user: str = Depends(require_auth_user)) -> dict:
    options = generate_registration_options(
        rp_id=_rp_id(request),
        rp_name=WEB_AUTH_RP_NAME,
        user_name=user,
        user_id=user.encode(),
        user_display_name=user,
        exclude_credentials=_public_credential_descriptors(user),
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.PREFERRED,
        ),
    )
    challenge_id = bytes_to_base64url(options.challenge)
    WEBAUTHN_CHALLENGES[challenge_id] = {"type": "register", "user": user, "challenge": options.challenge}
    return {"challenge_id": challenge_id, "options": options_to_json_dict(options)}


@app.post("/auth/passkeys/register/verify")
async def auth_passkey_register_verify(
    body: PasskeyCredentialBody,
    request: Request,
    user: str = Depends(require_auth_user),
) -> dict:
    challenge = WEBAUTHN_CHALLENGES.pop(body.challenge_id, None)
    if not challenge or challenge.get("type") != "register" or challenge.get("user") != user:
        raise HTTPException(status_code=400, detail="bad passkey challenge")
    verified = verify_registration_response(
        credential=body.credential,
        expected_challenge=challenge["challenge"],
        expected_rp_id=_rp_id(request),
        expected_origin=_request_origin(request),
        require_user_verification=False,
    )
    credential_id = bytes_to_base64url(verified.credential_id)
    db.create_webauthn_credential(
        credential_id=credential_id,
        username=user,
        public_key=bytes_to_base64url(verified.credential_public_key),
        sign_count=verified.sign_count,
        device_type=str(verified.credential_device_type.value),
        backed_up=verified.credential_backed_up,
    )
    return {"credential_id": credential_id}


@app.post("/auth/passkeys/login/options")
async def auth_passkey_login_options(body: PasskeyLoginOptionsBody, request: Request) -> dict:
    username = (body.username or "").strip().lower() or None
    if username and (not db.get_web_user(username) or db.get_web_user(username)["disabled"]):
        raise HTTPException(status_code=401, detail="bad username")
    options = generate_authentication_options(
        rp_id=_rp_id(request),
        allow_credentials=_public_credential_descriptors(username),
        user_verification=UserVerificationRequirement.PREFERRED,
    )
    challenge_id = bytes_to_base64url(options.challenge)
    WEBAUTHN_CHALLENGES[challenge_id] = {"type": "login", "user": username, "challenge": options.challenge}
    return {"challenge_id": challenge_id, "options": options_to_json_dict(options)}


@app.post("/auth/passkeys/login/verify", dependencies=[Depends(_rate_limit_login)])
async def auth_passkey_login_verify(body: PasskeyCredentialBody, request: Request, response: Response) -> dict:
    challenge = WEBAUTHN_CHALLENGES.pop(body.challenge_id, None)
    if not challenge or challenge.get("type") != "login":
        raise HTTPException(status_code=400, detail="bad passkey challenge")
    raw_id = body.credential.get("rawId") or body.credential.get("id")
    credential_id = raw_id if isinstance(raw_id, str) else ""
    credential = db.get_webauthn_credential(credential_id)
    if not credential:
        raise HTTPException(status_code=401, detail="unknown passkey")
    if challenge.get("user") and credential["username"] != challenge["user"]:
        raise HTTPException(status_code=401, detail="passkey does not match user")
    record = db.get_web_user(credential["username"])
    if not record or record["disabled"]:
        raise HTTPException(status_code=401, detail="user disabled")
    verified = verify_authentication_response(
        credential=body.credential,
        expected_challenge=challenge["challenge"],
        expected_rp_id=_rp_id(request),
        expected_origin=_request_origin(request),
        credential_public_key=base64url_to_bytes(credential["public_key"]),
        credential_current_sign_count=credential["sign_count"],
        require_user_verification=False,
    )
    db.update_webauthn_sign_count(credential_id, verified.new_sign_count)
    response.set_cookie(
        SESSION_COOKIE,
        _make_session_token(record["username"]),
        max_age=WEB_SESSION_TTL_SECONDS,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )
    return {"authenticated": True, "user": record["username"], "is_admin": record["is_admin"], "totp_required": record["totp_enabled"]}


@app.get("/auth/users")
async def auth_list_users(_: str = Depends(require_admin_user)) -> dict:
    users = []
    for user in db.list_web_users():
        public = _public_user(user)
        public["created_at"] = user["created_at"]
        public["updated_at"] = user["updated_at"]
        users.append(public)
    return {"users": users}


@app.post("/auth/users")
async def auth_create_user(body: CreateUserBody, _: str = Depends(require_admin_user)) -> dict:
    username = body.username.strip().lower()
    if not USERNAME_RE.match(username):
        raise HTTPException(status_code=400, detail="username must be lowercase letters, numbers, dash, or underscore")
    if len(body.password) < 12:
        raise HTTPException(status_code=400, detail="password must be at least 12 characters")
    if db.get_web_user(username):
        raise HTTPException(status_code=409, detail="user already exists")
    totp_secret = base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=") if body.enable_totp else ""
    user = db.create_web_user(
        username=username,
        password_hash=_hash_password(body.password),
        totp_secret=totp_secret,
        is_admin=body.is_admin,
    )
    return {
        "user": _public_user(user),
        "totp_secret": totp_secret,
    }


@app.patch("/auth/users/{username}")
async def auth_update_user(username: str, body: UpdateUserBody, admin: str = Depends(require_admin_user)) -> dict:
    username = username.strip().lower()
    if not db.get_web_user(username):
        raise HTTPException(status_code=404, detail="no such user")
    password_hash = None
    if body.password is not None:
        if len(body.password) < 12:
            raise HTTPException(status_code=400, detail="password must be at least 12 characters")
        password_hash = _hash_password(body.password)
    totp_secret = None
    new_totp_secret = ""
    if body.reset_totp:
        new_totp_secret = base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")
        totp_secret = new_totp_secret
    elif body.clear_totp:
        totp_secret = ""
    if username == admin and body.disabled is True:
        raise HTTPException(status_code=400, detail="cannot disable your own user")
    user = db.update_web_user(
        username,
        password_hash=password_hash,
        totp_secret=totp_secret,
        is_admin=body.is_admin,
        disabled=body.disabled,
    )
    return {
        "user": _public_user(user),
        "totp_secret": new_totp_secret,
    }


@app.post("/sessions")
async def create_session_endpoint(body: CreateBody, user: str = Depends(require_auth_user)) -> dict:
    try:
        sid = await create_session(
            user=user,
            cwd=body.cwd,
            system_prompt=body.system_prompt,
            permission_mode=body.permission_mode,
            allowed_tools=body.allowed_tools or [],
            max_turns=body.max_turns,
            model=body.model,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    sess_obj = SESSIONS[sid]
    return {"session_id": sid, "user": sess_obj.user, "cwd": sess_obj.cwd, "model": sess_obj.model}


@app.get("/sessions")
async def list_sessions(user: str = Depends(require_auth_user)) -> dict:
    return {
        "sessions": [
            {"id": s.id, "user": s.user, "cwd": s.cwd, "model": s.model}
            for s in SESSIONS.values()
            if s.user == user
        ]
    }


@app.delete("/sessions/{sid}")
async def stop_session(sid: str, user: str = Depends(require_auth_user)) -> dict:
    sess_obj = SESSIONS.get(sid)
    if not sess_obj:
        raise HTTPException(404, "no such session")
    if sess_obj.user != user:
        raise HTTPException(403, "not your session")
    await kill_session(sid)
    return {"stopped": sid}


@app.post("/sessions/{sid}/query")
async def query_session(sid: str, body: QueryBody, user: str = Depends(require_auth_user)) -> StreamingResponse:
    sess_obj = SESSIONS.get(sid)
    if not sess_obj:
        raise HTTPException(404, "no such session")
    if sess_obj.user != user:
        raise HTTPException(403, "not your session")

    def line(payload: dict) -> bytes:
        return (json.dumps(payload) + "\n").encode()

    async def gen() -> AsyncIterator[bytes]:
        async for ev in stream_query(sid, body.prompt):
            yield line(ev)

    return StreamingResponse(gen(), media_type="application/x-ndjson")


@app.post("/jobs/tick", dependencies=[Depends(require_internal), Depends(check_api_key)])
async def jobs_tick() -> dict:
    """Run any jobs whose cron fires in the current minute."""
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    fired: list[int] = []
    for job in db.list_jobs():
        if not job["enabled"]:
            continue
        job_id = job["id"]
        if job_id in RUNNING_JOBS:
            continue
        if _ran_this_minute(job.get("last_run_at"), now):
            continue
        if croniter.match(job["cron_expr"], now):
            fired.append(job_id)
            RUNNING_JOBS.add(job_id)
            db.mark_ran(job_id)
            asyncio.create_task(_run_job(job))
    return {"fired": fired, "checked_at": now.isoformat()}


def _ran_this_minute(value: str | None, now: datetime) -> bool:
    if not value:
        return False
    try:
        ran_at = datetime.fromisoformat(value)
    except ValueError:
        return False
    if ran_at.tzinfo is None:
        ran_at = ran_at.replace(tzinfo=timezone.utc)
    return ran_at.astimezone(timezone.utc).replace(second=0, microsecond=0) == now


async def _run_job(job: dict) -> None:
    import traceback
    jid = job["id"]
    try:
        print(f"[job {jid}] starting cron={job['cron_expr']} user={job['user']} model={job.get('model')!r}", flush=True)
        result = await run_one_shot(
            user=job["user"] or str(job["chat_id"]),
            cwd=job["cwd"],
            system_prompt=job["system_prompt"],
            permission_mode=job["permission_mode"],
            allowed_tools=job["allowed_tools"],
            max_turns=job["max_turns"],
            prompt=job["prompt"],
            model=job.get("model") or None,
        )
        db.mark_ran(jid)
        print(f"[job {jid}] run_one_shot ok cost=${(result.get('cost_usd') or 0):.4f}", flush=True)
        if ENABLE_BOT:
            from bot import post_to_chat
            tools_part = f"\n[tools: {', '.join(result['tools'])}]" if result["tools"] else ""
            cost_part = f"\n[cost: ${result['cost_usd']:.4f}]" if result["cost_usd"] else ""
            text = f"⏰ Job #{jid} ({job['cron_expr']})\n\n{result['text']}{tools_part}{cost_part}"
            try:
                await post_to_chat(job["chat_id"], text)
                print(f"[job {jid}] delivered", flush=True)
            except Exception as e:
                print(f"[job {jid}] delivery failed: {e!r}", flush=True)
                traceback.print_exc()
    except Exception as e:
        print(f"[job {jid}] run failed: {e!r}", flush=True)
        traceback.print_exc()
        if ENABLE_BOT:
            from bot import post_to_chat
            try:
                await post_to_chat(job["chat_id"], f"⚠ Job #{jid} failed: {e}")
            except Exception as e2:
                print(f"[job {jid}] failure-notification also failed: {e2!r}", flush=True)
    finally:
        RUNNING_JOBS.discard(jid)
