"""SQLite store for scheduled jobs and per-chat state."""
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

DB_PATH = os.environ.get("ORCHESTRATOR_DB", "./orchestrator.db")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_db() -> None:
    with conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cron_expr TEXT NOT NULL,
                prompt TEXT NOT NULL,
                chat_id INTEGER NOT NULL,
                cwd TEXT,
                user TEXT NOT NULL DEFAULT '',
                system_prompt TEXT,
                permission_mode TEXT NOT NULL DEFAULT 'acceptEdits',
                allowed_tools TEXT NOT NULL DEFAULT '[]',
                max_turns INTEGER,
                enabled INTEGER NOT NULL DEFAULT 1,
                last_run_at TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS chat_state (
                chat_id INTEGER PRIMARY KEY,
                active_session_id TEXT,
                default_permission_mode TEXT NOT NULL DEFAULT 'acceptEdits',
                default_model TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS web_users (
                username TEXT PRIMARY KEY,
                password_hash TEXT NOT NULL,
                totp_secret TEXT NOT NULL DEFAULT '',
                is_admin INTEGER NOT NULL DEFAULT 0,
                disabled INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS webauthn_credentials (
                credential_id TEXT PRIMARY KEY,
                username TEXT NOT NULL,
                public_key TEXT NOT NULL,
                sign_count INTEGER NOT NULL DEFAULT 0,
                device_type TEXT NOT NULL DEFAULT '',
                backed_up INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(username) REFERENCES web_users(username) ON DELETE CASCADE
            );
            """
        )
        # forward-compatible migrations (ignore if already applied)
        for stmt in (
            "ALTER TABLE jobs ADD COLUMN user TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE jobs ADD COLUMN model TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE chat_state ADD COLUMN default_model TEXT NOT NULL DEFAULT ''",
        ):
            try:
                c.execute(stmt)
            except sqlite3.OperationalError:
                pass


@contextmanager
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(DB_PATH, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL;")
    try:
        yield c
    finally:
        c.close()


# --- jobs ---

def create_job(
    *,
    cron_expr: str,
    prompt: str,
    chat_id: int,
    user: str,
    cwd: str | None,
    system_prompt: str | None,
    permission_mode: str,
    allowed_tools: list[str],
    max_turns: int | None,
    model: str = "",
) -> int:
    with conn() as c:
        cur = c.execute(
            """INSERT INTO jobs
            (cron_expr, prompt, chat_id, user, cwd, system_prompt, permission_mode,
             allowed_tools, max_turns, model, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                cron_expr,
                prompt,
                chat_id,
                user,
                cwd or "",  # legacy jobs.cwd is NOT NULL on pre-0.4.x deployments
                system_prompt,
                permission_mode,
                json.dumps(allowed_tools),
                max_turns,
                model,
                _now(),
            ),
        )
        return cur.lastrowid


def list_jobs(chat_id: int | None = None) -> list[dict]:
    q = "SELECT * FROM jobs"
    args: tuple = ()
    if chat_id is not None:
        q += " WHERE chat_id = ?"
        args = (chat_id,)
    q += " ORDER BY id"
    with conn() as c:
        rows = c.execute(q, args).fetchall()
        return [_row_to_job(r) for r in rows]


def get_job(job_id: int) -> dict | None:
    with conn() as c:
        row = c.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return _row_to_job(row) if row else None


def delete_job(job_id: int, chat_id: int | None = None) -> bool:
    with conn() as c:
        if chat_id is None:
            cur = c.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
        else:
            cur = c.execute(
                "DELETE FROM jobs WHERE id = ? AND chat_id = ?", (job_id, chat_id)
            )
        return cur.rowcount > 0


def mark_ran(job_id: int) -> None:
    with conn() as c:
        c.execute("UPDATE jobs SET last_run_at = ? WHERE id = ?", (_now(), job_id))


def _row_to_job(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["allowed_tools"] = json.loads(d["allowed_tools"] or "[]")
    d["enabled"] = bool(d["enabled"])
    return d


# --- chat state ---

def get_chat_state(chat_id: int) -> dict:
    with conn() as c:
        row = c.execute("SELECT * FROM chat_state WHERE chat_id = ?", (chat_id,)).fetchone()
        if row:
            return dict(row)
        c.execute(
            "INSERT INTO chat_state (chat_id, updated_at) VALUES (?, ?)",
            (chat_id, _now()),
        )
        row = c.execute("SELECT * FROM chat_state WHERE chat_id = ?", (chat_id,)).fetchone()
        return dict(row)


def set_active_session(chat_id: int, session_id: str | None) -> None:
    with conn() as c:
        get_chat_state(chat_id)  # ensure row exists
        c.execute(
            "UPDATE chat_state SET active_session_id = ?, updated_at = ? WHERE chat_id = ?",
            (session_id, _now(), chat_id),
        )


def set_default_model(chat_id: int, model: str) -> None:
    with conn() as c:
        get_chat_state(chat_id)
        c.execute(
            "UPDATE chat_state SET default_model = ?, updated_at = ? WHERE chat_id = ?",
            (model, _now(), chat_id),
        )


# --- web users ---

def count_web_users() -> int:
    with conn() as c:
        return int(c.execute("SELECT COUNT(*) FROM web_users").fetchone()[0])


def create_web_user(
    *,
    username: str,
    password_hash: str,
    totp_secret: str = "",
    is_admin: bool = False,
    disabled: bool = False,
) -> dict:
    now = _now()
    with conn() as c:
        c.execute(
            """INSERT INTO web_users
            (username, password_hash, totp_secret, is_admin, disabled, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (username, password_hash, totp_secret, int(is_admin), int(disabled), now, now),
        )
        return get_web_user(username) or {}


def list_web_users() -> list[dict]:
    with conn() as c:
        rows = c.execute(
            """SELECT username, totp_secret, is_admin, disabled, created_at, updated_at
            FROM web_users ORDER BY username"""
        ).fetchall()
        return [_row_to_web_user(r) for r in rows]


def get_web_user(username: str) -> dict | None:
    with conn() as c:
        row = c.execute("SELECT * FROM web_users WHERE username = ?", (username,)).fetchone()
        return _row_to_web_user(row) if row else None


def update_web_user(
    username: str,
    *,
    password_hash: str | None = None,
    totp_secret: str | None = None,
    is_admin: bool | None = None,
    disabled: bool | None = None,
) -> dict | None:
    assignments = ["updated_at = ?"]
    args: list = [_now()]
    if password_hash is not None:
        assignments.append("password_hash = ?")
        args.append(password_hash)
    if totp_secret is not None:
        assignments.append("totp_secret = ?")
        args.append(totp_secret)
    if is_admin is not None:
        assignments.append("is_admin = ?")
        args.append(int(is_admin))
    if disabled is not None:
        assignments.append("disabled = ?")
        args.append(int(disabled))
    args.append(username)
    with conn() as c:
        c.execute(f"UPDATE web_users SET {', '.join(assignments)} WHERE username = ?", args)
    return get_web_user(username)


def delete_web_user(username: str) -> bool:
    with conn() as c:
        cur = c.execute("DELETE FROM web_users WHERE username = ?", (username,))
        return cur.rowcount > 0


def _row_to_web_user(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["is_admin"] = bool(d["is_admin"])
    d["disabled"] = bool(d["disabled"])
    d["totp_enabled"] = bool(d.get("totp_secret"))
    return d


def list_webauthn_credentials(username: str | None = None) -> list[dict]:
    q = "SELECT * FROM webauthn_credentials"
    args: tuple = ()
    if username is not None:
        q += " WHERE username = ?"
        args = (username,)
    q += " ORDER BY created_at"
    with conn() as c:
        return [_row_to_webauthn_credential(r) for r in c.execute(q, args).fetchall()]


def get_webauthn_credential(credential_id: str) -> dict | None:
    with conn() as c:
        row = c.execute("SELECT * FROM webauthn_credentials WHERE credential_id = ?", (credential_id,)).fetchone()
        return _row_to_webauthn_credential(row) if row else None


def create_webauthn_credential(
    *,
    credential_id: str,
    username: str,
    public_key: str,
    sign_count: int,
    device_type: str = "",
    backed_up: bool = False,
) -> dict:
    now = _now()
    with conn() as c:
        c.execute(
            """INSERT INTO webauthn_credentials
            (credential_id, username, public_key, sign_count, device_type, backed_up, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (credential_id, username, public_key, sign_count, device_type, int(backed_up), now, now),
        )
        return get_webauthn_credential(credential_id) or {}


def update_webauthn_sign_count(credential_id: str, sign_count: int) -> None:
    with conn() as c:
        c.execute(
            "UPDATE webauthn_credentials SET sign_count = ?, updated_at = ? WHERE credential_id = ?",
            (sign_count, _now(), credential_id),
        )


def delete_webauthn_credential(credential_id: str, username: str | None = None) -> bool:
    with conn() as c:
        if username is None:
            cur = c.execute("DELETE FROM webauthn_credentials WHERE credential_id = ?", (credential_id,))
        else:
            cur = c.execute(
                "DELETE FROM webauthn_credentials WHERE credential_id = ? AND username = ?",
                (credential_id, username),
            )
        return cur.rowcount > 0


def _row_to_webauthn_credential(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["backed_up"] = bool(d["backed_up"])
    return d
