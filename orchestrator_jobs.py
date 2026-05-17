#!/usr/bin/env python3
"""CLI helper for agents to manage built-in orchestrator scheduled jobs."""
import argparse
import json
import os
import sys

from croniter import croniter

import db
from bot import build_system_prompt, user_for


def _chat_id(value: str) -> int:
    try:
        return int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("chat_id must be an integer") from exc


def _create(args: argparse.Namespace) -> int:
    if not croniter.is_valid(args.cron):
        print(f"Invalid cron: {args.cron}", file=sys.stderr)
        return 2
    user = args.user or user_for(args.chat_id)
    state = db.get_chat_state(args.chat_id)
    job_id = db.create_job(
        cron_expr=args.cron,
        prompt=args.prompt,
        chat_id=args.chat_id,
        user=user,
        cwd=args.cwd,
        system_prompt=build_system_prompt(user, args.chat_id),
        permission_mode=args.permission_mode,
        allowed_tools=[],
        max_turns=None,
        model=args.model if args.model is not None else (state.get("default_model", "") or ""),
    )
    print(json.dumps({"created": True, "id": job_id, "cron": args.cron, "prompt": args.prompt}, ensure_ascii=False))
    return 0


def _list(args: argparse.Namespace) -> int:
    jobs = db.list_jobs(chat_id=args.chat_id)
    print(json.dumps({"jobs": jobs}, ensure_ascii=False, default=str))
    return 0


def _delete(args: argparse.Namespace) -> int:
    deleted = db.delete_job(args.id, chat_id=args.chat_id)
    print(json.dumps({"deleted": deleted, "id": args.id}, ensure_ascii=False))
    return 0 if deleted else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage orchestrator scheduled jobs.")
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create", help="Create a scheduled job")
    create.add_argument("--chat-id", type=_chat_id, required=True)
    create.add_argument("--user", default="")
    create.add_argument("--cron", required=True)
    create.add_argument("--prompt", required=True)
    create.add_argument("--cwd", default=None)
    create.add_argument("--permission-mode", default=os.environ.get("TELEGRAM_PERMISSION_MODE", "bypassPermissions"))
    create.add_argument("--model", default=None)
    create.set_defaults(func=_create)

    list_cmd = sub.add_parser("list", help="List scheduled jobs")
    list_cmd.add_argument("--chat-id", type=_chat_id, required=True)
    list_cmd.set_defaults(func=_list)

    delete = sub.add_parser("delete", help="Delete a scheduled job")
    delete.add_argument("--chat-id", type=_chat_id, required=True)
    delete.add_argument("--id", type=int, required=True)
    delete.set_defaults(func=_delete)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

