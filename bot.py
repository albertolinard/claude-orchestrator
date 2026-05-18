"""Telegram bot. Single-user whitelist. Manages sessions + scheduled jobs.

Per-chat user slug derived from chat_id (or TELEGRAM_USER env override).
"""
import asyncio
import contextlib
import html
import os
import re
import shlex

from croniter import croniter
from telegram import BotCommand, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

BOT_COMMANDS = [
    BotCommand("new", "Create new session (optional: model)"),
    BotCommand("sessions", "List active sessions"),
    BotCommand("use", "Switch active session"),
    BotCommand("stop", "Kill a session"),
    BotCommand("clear", "Forget active session pointer"),
    BotCommand("model", "Show/set default model for this chat"),
    BotCommand("schedule", "Add scheduled job"),
    BotCommand("jobs", "List scheduled jobs"),
    BotCommand("unschedule", "Remove scheduled job"),
    BotCommand("help", "Show help"),
    BotCommand("start", "Show chat_id + user slug"),
]

KNOWN_MODELS = [
    "claude-opus-4-7",
    "claude-sonnet-4-6",
    "claude-haiku-4-5",
    "claude-haiku-4-5-20251001",
    "glm-5.1:cloud",
    "deepseek-v4-flash:cloud",
]


def build_system_prompt(user: str, chat_id: int) -> str:
    """Runtime self-awareness so Claude doesn't propose external schedulers."""
    return f"""You are an AI assistant running inside the "claude-orchestrator" service.
This service is deployed on the user's Kubernetes cluster and exposes:
  - a web UI at the configured orchestrator URL
  - a Telegram bot
  - a built-in cron scheduler

THE USER IS TALKING TO YOU THROUGH THE TELEGRAM BOT RIGHT NOW.
- chat_id: {chat_id}
- user slug: {user}
- HOME: /opt/data/users/{user}/  (persisted on NFS; survives pod restarts)
- workspace cwd: /opt/data/users/{user}/workspace
- kubeconfig: /opt/data/users/{user}/.kube/config  (already mounted)
- subscription auth: /opt/data/users/{user}/.claude/  (already seeded)

You ARE in-cluster. kubectl works against the private k3s API; private IPs
(192.168.x.x, 10.43.x.x) are reachable. Permission mode is bypassPermissions,
so you may execute tools without confirmation prompts.

=== SCHEDULING RECURRING TASKS ===
The orchestrator has a built-in scheduler. When the user asks for "every day",
"every hour", "weekly", or any recurring task, DO NOT propose:
  - system cron / crontab
  - systemd timers
  - Kubernetes CronJob manifests
  - GitHub Actions
  - third-party schedulers (Cronicle, Nomad, Airflow, etc.)

Also DO NOT merely give the user a /schedule command. Create, list, or delete
the schedule yourself using the local helper CLI:

    orchestrator-jobs create --chat-id {chat_id} --user {user} --cron "<5-field UTC cron>" --prompt "<job prompt>"
    orchestrator-jobs list --chat-id {chat_id}
    orchestrator-jobs delete --chat-id {chat_id} --id <job-id>

Example for daily 07:00 America/Fortaleza (UTC-3, no DST):

    orchestrator-jobs create --chat-id {chat_id} --user {user} --cron "0 10 * * *" --prompt "Check pgBackRest status in namespace postgres and report any issues."

Scheduled jobs:
  - run on this same orchestrator pod, with the same access you have now
    (kubeconfig, network, models, NFS data dir);
  - spawn an ephemeral session as user "{user}";
  - deliver their output back to this exact Telegram chat (chat_id {chat_id});
  - tick every minute via a Kubernetes CronJob.

Other scheduler commands available to the user in this chat:
    /jobs                  list scheduled jobs
    /unschedule <id>       remove one
    /model <name>          change default model
    /sessions              list active sessions
    /new                   start a fresh session

When the user asks for recurring work and gives enough timing/task detail,
convert the schedule to a 5-field UTC cron expression, run `orchestrator-jobs
create`, then briefly confirm the job id, cron, and purpose. If they ask why a
schedule is missing, run `orchestrator-jobs list --chat-id {chat_id}` and create
the missing job yourself if the intended schedule is clear from context.
Ask a concise follow-up only when either timing or task details are missing.
Do not write any external scheduling artifact unless they explicitly ask.

=== TELEGRAM OUTPUT STYLE ===
Format answers for Telegram mobile display, not GitHub Markdown:
  - use short sections with bold labels
  - use simple bullets, not nested lists
  - do not use Markdown tables
  - use fenced code blocks only for commands, config, or logs
  - keep answers concise and easy to scan
"""


async def set_bot_commands(app: Application) -> None:
    await app.bot.set_my_commands(BOT_COMMANDS)


async def _post_init(app: Application) -> None:
    await set_bot_commands(app)

import db
from sessions import SESSIONS, create_session, kill_session, stream_query, slugify_user

ALLOWED_CHAT_ID = int(os.environ.get("TELEGRAM_ALLOWED_CHAT_ID", "0"))
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_USER_OVERRIDE = os.environ.get("TELEGRAM_USER", "").strip()
TELEGRAM_PERMISSION_MODE = os.environ.get("TELEGRAM_PERMISSION_MODE", "bypassPermissions").strip()

TG_MSG_LIMIT = 3800  # leave headroom under 4096
TG_FORMAT_LIMIT = 3200  # formatted HTML is larger than source text

HELP_TEXT = """Commands:
/start            show your chat_id + user slug
/new [cwd]        create new session
/sessions         list active sessions
/use <sid>        set active session
/stop <sid>       kill session
/clear            forget active session pointer
/schedule "<cron>" <prompt>   schedule recurring job
/jobs             list your scheduled jobs
/unschedule <id>  remove job
/help             this message

Plain text => prompt for your active session."""


def authorized(update: Update) -> bool:
    if ALLOWED_CHAT_ID == 0:
        return True  # discovery mode
    return update.effective_chat and update.effective_chat.id == ALLOWED_CHAT_ID


def user_for(chat_id: int) -> str:
    if TELEGRAM_USER_OVERRIDE:
        return slugify_user(TELEGRAM_USER_OVERRIDE)
    return slugify_user(str(chat_id))


async def reply(update: Update, text: str) -> None:
    chat = update.effective_chat
    if chat is None:
        return
    for chunk in telegram_chunks(text):
        await send_telegram_html(chat, chunk)


async def send_telegram_html(target, text: str, **kwargs) -> None:
    try:
        await target.send_message(text=text, parse_mode=ParseMode.HTML, **kwargs)
    except BadRequest as e:
        if "parse entities" not in str(e).lower():
            raise
        await target.send_message(text=_telegram_plain_fallback(text), **kwargs)


def _telegram_plain_fallback(text: str) -> str:
    plain = re.sub(r"</?(?:b|i|code|pre)>", "", text)
    return html.unescape(plain)


def telegram_chunks(text: str) -> list[str]:
    chunks: list[str] = []
    for raw in _split_plain_text(text, TG_FORMAT_LIMIT):
        formatted = markdown_to_telegram_html(raw)
        if len(formatted) <= TG_MSG_LIMIT:
            chunks.append(formatted)
            continue
        chunks.extend(html.escape(part) for part in _split_plain_text(raw, TG_MSG_LIMIT))
    return chunks or [""]


def _split_plain_text(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        cut = remaining.rfind("\n\n", 0, limit)
        if cut < limit // 2:
            cut = remaining.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()
    if remaining:
        chunks.append(remaining)
    return chunks


def markdown_to_telegram_html(text: str) -> str:
    """Convert common model Markdown into Telegram-safe HTML."""
    parts: list[str] = []
    pos = 0
    for match in re.finditer(r"```([^\n`]*)\n?(.*?)```", text, flags=re.DOTALL):
        if match.start() > pos:
            parts.append(_format_telegram_text(text[pos : match.start()]))
        code = match.group(2).strip("\n")
        parts.append(f"\n<pre><code>{html.escape(code)}</code></pre>\n")
        pos = match.end()
    if pos < len(text):
        parts.append(_format_telegram_text(text[pos:]))
    return "".join(parts).strip()


def _format_telegram_text(text: str) -> str:
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        heading = re.match(r"^#{1,6}\s+(.+)$", stripped)
        bullet = re.match(r"^[-*+]\s+(.+)$", stripped)
        numbered = re.match(r"^(\d+)[.)]\s+(.+)$", stripped)
        if heading:
            lines.append(f"<b>{_format_inline(heading.group(1))}</b>")
        elif bullet:
            lines.append(f"• {_format_inline(bullet.group(1))}")
        elif numbered:
            lines.append(f"{numbered.group(1)}. {_format_inline(numbered.group(2))}")
        else:
            lines.append(_format_inline(line))
    return "\n".join(lines)


def _format_inline(text: str) -> str:
    escaped = html.escape(text)
    codes: list[str] = []

    def stash_code(match: re.Match) -> str:
        codes.append(match.group(1))
        return f"@@CODE{len(codes) - 1}@@"

    escaped = re.sub(r"`([^`\n]+)`", stash_code, escaped)
    escaped = re.sub(r"\*\*([^*\n]+)\*\*", lambda m: f"<b>{m.group(1)}</b>", escaped)
    for i, code in enumerate(codes):
        escaped = escaped.replace(f"@@CODE{i}@@", f"<code>{code}</code>")
    return escaped


def _tool_display(event: dict) -> str:
    name = str(event.get("name") or "tool")
    tool_input = event.get("input")
    command = _extract_tool_command(tool_input)
    label = "Bash" if name.lower() in {"bash", "shell", "exec", "exec_command", "functions.exec_command"} else name
    if command:
        return f"🔧 {label}: `{_preview_command(command)}`"
    return f"🔧 {label}"


def _extract_tool_command(value) -> str:
    if isinstance(value, dict):
        for key in ("command", "cmd", "script", "shell_command"):
            item = value.get(key)
            if isinstance(item, str) and item.strip():
                return item.strip()
        for item in value.values():
            command = _extract_tool_command(item)
            if command:
                return command
    elif isinstance(value, list):
        for item in value:
            command = _extract_tool_command(item)
            if command:
                return command
    elif isinstance(value, str) and value.strip():
        return value.strip()
    return ""


def _preview_command(command: str, limit: int = 180) -> str:
    single_line = " ".join(command.split())
    if len(single_line) <= limit:
        return single_line
    return single_line[: limit - 1].rstrip() + "…"


async def cmd_start(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    cid = update.effective_chat.id
    if not authorized(update):
        await reply(update, f"Not authorized. Your chat_id: {cid}")
        return
    await reply(update, f"Hi. chat_id: {cid}\nuser slug: {user_for(cid)}\n\n{HELP_TEXT}")


async def cmd_help(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        return
    await reply(update, HELP_TEXT)


async def cmd_new(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        return
    chat_id = update.effective_chat.id
    user = user_for(chat_id)
    state = db.get_chat_state(chat_id)

    cwd: str | None = None
    model: str | None = state.get("default_model") or None
    for arg in ctx.args:
        if arg.startswith("model="):
            model = arg.split("=", 1)[1] or None
        elif arg.startswith("cwd="):
            cwd = arg.split("=", 1)[1] or None
        elif cwd is None:
            cwd = arg

    try:
        sid = await create_session(
            user=user,
            cwd=cwd,
            system_prompt=build_system_prompt(user, chat_id),
            permission_mode=TELEGRAM_PERMISSION_MODE,
            allowed_tools=[],
            max_turns=None,
            model=model,
        )
    except Exception as e:
        await reply(update, f"Create failed: {e}")
        return
    db.set_active_session(chat_id, sid)
    sess = SESSIONS[sid]
    model_line = f"model: `{sess.model}`" if sess.model else "model: `<CLI default>`"
    await reply(update, f"Session created.\nID: `{sid}`\nuser: `{user}`\n{model_line}")


async def cmd_model(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        return
    chat_id = update.effective_chat.id
    state = db.get_chat_state(chat_id)
    if not ctx.args:
        current = state.get("default_model") or "<CLI default>"
        opts = "\n  ".join(KNOWN_MODELS)
        await reply(
            update,
            f"Default model: `{current}`\n\n"
            f"Set with: /model <name>\nClear with: /model default\n\n"
            f"Known models:\n  {opts}",
        )
        return
    name = ctx.args[0].strip()
    if name.lower() in ("default", "clear", "unset", "none"):
        db.set_default_model(chat_id, "")
        await reply(update, "Cleared. New sessions use CLI default.")
        return
    db.set_default_model(chat_id, name)
    await reply(update, f"Default model set to `{name}`. Applies to new sessions / jobs.")


async def cmd_sessions(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        return
    if not SESSIONS:
        await reply(update, "No active sessions.")
        return
    lines = [f"{s.id}  user={s.user}  cwd={s.cwd}" for s in SESSIONS.values()]
    await reply(update, "Active sessions:\n" + "\n".join(lines))


async def cmd_use(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        return
    if not ctx.args:
        await reply(update, "Usage: /use <session_id>")
        return
    sid = ctx.args[0]
    if sid not in SESSIONS:
        await reply(update, f"No such session: {sid}")
        return
    db.set_active_session(update.effective_chat.id, sid)
    await reply(update, f"Active session: {sid}")


async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        return
    if not ctx.args:
        await reply(update, "Usage: /stop <session_id>")
        return
    sid = ctx.args[0]
    await kill_session(sid)
    state = db.get_chat_state(update.effective_chat.id)
    if state["active_session_id"] == sid:
        db.set_active_session(update.effective_chat.id, None)
    await reply(update, f"Stopped {sid}.")


async def cmd_clear(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        return
    db.set_active_session(update.effective_chat.id, None)
    await reply(update, "Active session cleared.")


async def cmd_schedule(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        return
    raw = update.message.text.removeprefix("/schedule").strip()
    if not raw:
        await reply(
            update,
            'Usage: /schedule "<cron 5-field>" <prompt>\n'
            'Example: /schedule "0 9 * * *" Summarize yesterday\'s logs.',
        )
        return
    try:
        parts = shlex.split(raw)
    except ValueError as e:
        await reply(update, f"Parse error: {e}")
        return
    if len(parts) < 2:
        await reply(update, "Need cron expression AND prompt.")
        return
    cron_expr, *prompt_parts = parts
    prompt = " ".join(prompt_parts).strip()
    if not croniter.is_valid(cron_expr):
        await reply(update, f"Invalid cron: {cron_expr}")
        return
    chat_id = update.effective_chat.id
    state = db.get_chat_state(chat_id)
    job_user = user_for(chat_id)
    job_id = db.create_job(
        cron_expr=cron_expr,
        prompt=prompt,
        chat_id=chat_id,
        user=job_user,
        cwd=None,
        system_prompt=build_system_prompt(job_user, chat_id),
        permission_mode=TELEGRAM_PERMISSION_MODE,
        allowed_tools=[],
        max_turns=None,
        model=state.get("default_model", "") or "",
    )
    await reply(update, f"Scheduled job #{job_id}\ncron: {cron_expr}\nprompt: {prompt}")


async def cmd_jobs(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        return
    jobs = db.list_jobs(chat_id=update.effective_chat.id)
    if not jobs:
        await reply(update, "No scheduled jobs.")
        return
    lines = []
    for j in jobs:
        last = j["last_run_at"] or "never"
        lines.append(f"#{j['id']}  {j['cron_expr']}  last={last}\n  {j['prompt'][:120]}")
    await reply(update, "\n".join(lines))


async def cmd_unschedule(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        return
    if not ctx.args:
        await reply(update, "Usage: /unschedule <job_id>")
        return
    try:
        jid = int(ctx.args[0])
    except ValueError:
        await reply(update, "Job ID must be integer.")
        return
    ok = db.delete_job(jid, chat_id=update.effective_chat.id)
    await reply(update, f"Deleted job {jid}." if ok else f"Job {jid} not found.")


async def _typing_keepalive(chat) -> None:
    """Refresh Telegram TYPING action every 4s until cancelled."""
    try:
        while True:
            try:
                await chat.send_action(ChatAction.TYPING)
            except Exception:
                pass
            await asyncio.sleep(4)
    except asyncio.CancelledError:
        return


async def on_text(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        return
    chat_id = update.effective_chat.id
    state = db.get_chat_state(chat_id)
    sid = state["active_session_id"]
    if not sid or sid not in SESSIONS:
        await reply(update, "No active session. /new to create one.")
        return
    prompt = update.message.text
    chat = update.effective_chat
    typing_task = asyncio.create_task(_typing_keepalive(chat))
    try:
        async for event in stream_query(sid, prompt):
            if event["type"] == "text":
                await reply(update, event["content"])
            elif event["type"] == "tool":
                await reply(update, _tool_display(event))
            elif event["type"] == "done":
                cost = event.get("cost_usd")
                if cost is not None:
                    await reply(update, f"— done · ${cost:.4f} —")
    except Exception as e:
        await reply(update, f"Error: {e}")
    finally:
        typing_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await typing_task


def build_app() -> Application:
    if not BOT_TOKEN:
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN before launching bot.")
    app = Application.builder().token(BOT_TOKEN).post_init(_post_init).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("sessions", cmd_sessions))
    app.add_handler(CommandHandler("use", cmd_use))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(CommandHandler("model", cmd_model))
    app.add_handler(CommandHandler("schedule", cmd_schedule))
    app.add_handler(CommandHandler("jobs", cmd_jobs))
    app.add_handler(CommandHandler("unschedule", cmd_unschedule))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    return app


async def post_to_chat(chat_id: int, text: str) -> None:
    from telegram import Bot
    bot = Bot(token=BOT_TOKEN)
    for chunk in telegram_chunks(text):
        await send_telegram_html(bot, chunk, chat_id=chat_id)
