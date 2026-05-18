"""Shared session manager. Per-user HOME isolation under /opt/data/users/<user>/.

Session metadata is persisted to /opt/data/users/<user>/sessions/<sid>.json so
sessions survive pod restarts. Clients (subprocesses) are spawned lazily on
first use after restart, using ClaudeAgentOptions.resume to attach to the
conversation history at .claude/projects/.
"""
import json
import os
import re
import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
)

USERS_ROOT = os.environ.get("USERS_ROOT", "/opt/data/users")
USER_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
DEFAULT_MODEL = os.environ.get("CLAUDE_DEFAULT_MODEL", "").strip() or None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Session:
    id: str  # UUID4 — matches Claude CLI session_id under .claude/projects/
    user: str
    cwd: str = ""
    model: str | None = None
    permission_mode: str = "acceptEdits"
    system_prompt: str | None = None
    allowed_tools: list[str] = field(default_factory=list)
    max_turns: int | None = None
    client: ClaudeSDKClient | None = None  # None = ghost, lazy-resume on next query
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    created_at: str = ""
    last_used_at: str = ""


SESSIONS: dict[str, Session] = {}


def slugify_user(raw: str) -> str:
    """Normalize a user identifier to a safe path segment. Raises ValueError if unsafe."""
    if not raw:
        raise ValueError("user is required")
    s = raw.strip().lower().replace(" ", "-")
    if not USER_SLUG_RE.match(s):
        raise ValueError(
            f"invalid user '{raw}': must be lowercase alphanumeric + '-' / '_', "
            "starting with alphanumeric, max 63 chars"
        )
    return s


def user_home(user: str) -> str:
    return os.path.join(USERS_ROOT, user)


def ensure_user_dirs(user: str) -> str:
    """Create per-user HOME + workspace + .claude + sessions dirs. Returns HOME path."""
    home = user_home(user)
    os.makedirs(os.path.join(home, ".claude"), exist_ok=True)
    os.makedirs(os.path.join(home, "workspace"), exist_ok=True)
    os.makedirs(os.path.join(home, "sessions"), exist_ok=True)
    os.chmod(home, 0o700)
    return home


def _meta_path(user: str, sid: str) -> Path:
    return Path(user_home(user)) / "sessions" / f"{sid}.json"


def _save_meta(sess: Session) -> None:
    p = _meta_path(sess.user, sess.id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(
            {
                "id": sess.id,
                "user": sess.user,
                "cwd": sess.cwd,
                "model": sess.model,
                "permission_mode": sess.permission_mode,
                "system_prompt": sess.system_prompt,
                "allowed_tools": sess.allowed_tools,
                "max_turns": sess.max_turns,
                "created_at": sess.created_at,
                "last_used_at": sess.last_used_at,
            },
            indent=2,
        )
    )


def _delete_meta(user: str, sid: str) -> None:
    try:
        _meta_path(user, sid).unlink()
    except FileNotFoundError:
        pass


def load_persisted_sessions() -> None:
    """On startup, scan all users' session metadata and register ghost entries."""
    root = Path(USERS_ROOT)
    if not root.exists():
        return
    for user_dir in root.iterdir():
        sessions_dir = user_dir / "sessions"
        if not sessions_dir.is_dir():
            continue
        for f in sessions_dir.glob("*.json"):
            try:
                d = json.loads(f.read_text())
            except Exception:
                continue
            sid = d.get("id")
            if not sid:
                continue
            SESSIONS[sid] = Session(
                id=sid,
                user=d["user"],
                cwd=d.get("cwd", ""),
                model=d.get("model"),
                permission_mode=d.get("permission_mode", "acceptEdits"),
                system_prompt=d.get("system_prompt"),
                allowed_tools=d.get("allowed_tools") or [],
                max_turns=d.get("max_turns"),
                client=None,
                created_at=d.get("created_at", ""),
                last_used_at=d.get("last_used_at", ""),
            )


def update_system_prompt(sid: str, system_prompt: str | None) -> None:
    sess = SESSIONS[sid]
    if sess.system_prompt == system_prompt:
        return
    sess.system_prompt = system_prompt
    _save_meta(sess)


OLLAMA_LOCAL_URL = os.environ.get("OLLAMA_LOCAL_URL", "http://127.0.0.1:11434")


def _is_ollama_cloud(model: str | None) -> bool:
    """Models with the `:cloud` suffix route through the in-pod Ollama daemon
    (sidecar at 127.0.0.1:11434), which proxies to Ollama Cloud."""
    return bool(model and model.endswith(":cloud"))


def _build_options(sess: Session, *, resume: bool) -> ClaudeAgentOptions:
    env = {
        "HOME": user_home(sess.user),
        "USER": sess.user,
        "XDG_CONFIG_HOME": os.path.join(user_home(sess.user), ".config"),
        "TMPDIR": os.path.join(user_home(sess.user), "tmp"),
    }
    os.makedirs(env["TMPDIR"], exist_ok=True)
    model_for_sdk = sess.model
    if _is_ollama_cloud(sess.model):
        # Mirror `ollama launch claude --model <m>` env so the Claude CLI talks
        # to the local Ollama daemon (Anthropic-compatible endpoint) instead of
        # api.anthropic.com. The daemon forwards :cloud models to Ollama Cloud.
        env["ANTHROPIC_AUTH_TOKEN"] = "ollama"
        env["ANTHROPIC_BASE_URL"] = OLLAMA_LOCAL_URL
        env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = sess.model
        env["ANTHROPIC_DEFAULT_SONNET_MODEL"] = sess.model
        env["ANTHROPIC_DEFAULT_OPUS_MODEL"] = sess.model
        env["CLAUDE_CODE_SUBAGENT_MODEL"] = sess.model
        env["CLAUDE_CODE_USE_OPENAI"] = "1"
        # SDK adds `--model <m>` arg; Anthropic CLI rejects ":cloud" suffix on
        # its --model flag, so let the ANTHROPIC_DEFAULT_*_MODEL vars route it.
        model_for_sdk = None
    kwargs: dict = dict(
        cwd=sess.cwd,
        system_prompt=sess.system_prompt,
        permission_mode=sess.permission_mode,
        allowed_tools=sess.allowed_tools or [],
        max_turns=sess.max_turns,
        env=env,
    )
    if model_for_sdk:
        kwargs["model"] = model_for_sdk
    if resume:
        kwargs["resume"] = sess.id
    else:
        kwargs["session_id"] = sess.id
    return ClaudeAgentOptions(**kwargs)


async def _ensure_client(sess: Session, *, resume: bool) -> None:
    if sess.client is not None:
        return
    ensure_user_dirs(sess.user)
    client = ClaudeSDKClient(options=_build_options(sess, resume=resume))
    await client.connect()
    sess.client = client


async def create_session(
    *,
    user: str,
    cwd: str | None,
    system_prompt: str | None,
    permission_mode: str,
    allowed_tools: list[str],
    max_turns: int | None,
    model: str | None = None,
) -> str:
    user = slugify_user(user)
    home = ensure_user_dirs(user)
    effective_cwd = cwd or os.path.join(home, "workspace")
    os.makedirs(effective_cwd, exist_ok=True)
    effective_model = (model or DEFAULT_MODEL) or None

    sid = str(uuid.uuid4())
    now = _now()
    sess = Session(
        id=sid,
        user=user,
        cwd=effective_cwd,
        model=effective_model,
        permission_mode=permission_mode,
        system_prompt=system_prompt,
        allowed_tools=allowed_tools or [],
        max_turns=max_turns,
        created_at=now,
        last_used_at=now,
    )
    await _ensure_client(sess, resume=False)
    SESSIONS[sid] = sess
    _save_meta(sess)
    return sid


async def kill_session(sid: str) -> None:
    """User-initiated kill: disconnect client AND drop persisted metadata."""
    sess = SESSIONS.pop(sid, None)
    if sess:
        if sess.client is not None:
            try:
                await sess.client.disconnect()
            except Exception:
                pass
        _delete_meta(sess.user, sess.id)


async def detach_all_sessions() -> None:
    """Shutdown helper: disconnect every client but keep metadata for resume."""
    for sess in list(SESSIONS.values()):
        if sess.client is not None:
            try:
                await sess.client.disconnect()
            except Exception:
                pass
            sess.client = None


async def stream_query(sid: str, prompt: str) -> AsyncIterator[dict]:
    sess = SESSIONS.get(sid)
    if not sess:
        raise KeyError(f"no such session: {sid}")
    async with sess.lock:
        await _ensure_client(sess, resume=True)
        sess.last_used_at = _now()
        _save_meta(sess)
        await sess.client.query(prompt)
        async for msg in sess.client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        yield {"type": "text", "content": block.text}
                    elif isinstance(block, ToolUseBlock):
                        yield {"type": "tool", "name": block.name, "input": block.input}
            elif isinstance(msg, ResultMessage):
                yield {
                    "type": "done",
                    "cost_usd": getattr(msg, "total_cost_usd", None),
                    "turns": getattr(msg, "num_turns", None),
                }


async def oauth_warmer_loop(
    *,
    check_interval: int = 1800,
    refresh_threshold: int = 3600,
) -> None:
    """Force Claude CLI to refresh OAuth tokens before they expire.

    Tokens at .credentials.json carry expiresAt (ms). When expiry is within
    `refresh_threshold` seconds, fire a minimal one-shot to trigger the CLI's
    internal refresh path. Without this, an idle pod's tokens go stale.
    """
    import time
    while True:
        try:
            root = Path(USERS_ROOT)
            if root.exists():
                for user_dir in root.iterdir():
                    cred = user_dir / ".claude" / ".credentials.json"
                    if not cred.is_file():
                        continue
                    try:
                        oauth = json.loads(cred.read_text()).get("claudeAiOauth", {})
                        exp_ms = int(oauth.get("expiresAt", 0))
                    except Exception:
                        continue
                    if exp_ms == 0:
                        continue
                    remaining = exp_ms / 1000 - time.time()
                    if remaining > refresh_threshold:
                        continue
                    user = user_dir.name
                    print(f"[oauth-warmer] refreshing {user} (expires in {int(remaining)}s)")
                    try:
                        await run_one_shot(
                            user=user,
                            cwd=None,
                            system_prompt=None,
                            permission_mode="default",
                            allowed_tools=[],
                            max_turns=1,
                            prompt="ok",
                            model=None,
                        )
                    except Exception as e:
                        print(f"[oauth-warmer] {user} refresh failed: {e}")
        except Exception as e:
            print(f"[oauth-warmer] loop error: {e}")
        await asyncio.sleep(check_interval)


async def run_one_shot(
    *,
    user: str,
    cwd: str | None,
    system_prompt: str | None,
    permission_mode: str,
    allowed_tools: list[str],
    max_turns: int | None,
    prompt: str,
    model: str | None = None,
) -> dict:
    sid = await create_session(
        user=user,
        cwd=cwd,
        system_prompt=system_prompt,
        permission_mode=permission_mode,
        allowed_tools=allowed_tools,
        max_turns=max_turns,
        model=model,
    )
    try:
        text_parts: list[str] = []
        tools: list[str] = []
        cost = None
        async for ev in stream_query(sid, prompt):
            if ev["type"] == "text":
                text_parts.append(ev["content"])
            elif ev["type"] == "tool":
                tools.append(ev["name"])
            elif ev["type"] == "done":
                cost = ev.get("cost_usd")
        return {"text": "\n".join(text_parts), "tools": tools, "cost_usd": cost}
    finally:
        await kill_session(sid)
