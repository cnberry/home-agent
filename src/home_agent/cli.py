from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import stat
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from openai_codex import AsyncCodex, CodexConfig
from telegram import Bot

from home_agent.backup import BackupError, perform_backup, restore_backup
from home_agent.codex_runtime import CodexRuntime
from home_agent.config import ConfigError, Settings, load_settings
from home_agent.database import Database, QueueFullError
from home_agent.health import enqueue_heartbeat
from home_agent.telegram_gateway import TelegramGateway
from home_agent.worker import safe_error

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


def configure_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # httpx logs full request URLs at INFO, and Telegram embeds the bot token in its URL.
    # Keep transport internals quiet even when application-level verbose logging is enabled.
    for logger_name in ("httpx", "httpcore", "telegram.ext.ExtBot"):
        logging.getLogger(logger_name).setLevel(logging.WARNING)


def _settings(args: argparse.Namespace) -> Settings:
    path = Path(args.config) if getattr(args, "config", None) else None
    return load_settings(path)


def run_agent(args: argparse.Namespace) -> int:
    settings = _settings(args)
    gateway = TelegramGateway(settings, settings.read_telegram_token())
    gateway.run()
    return 0


def queue_heartbeat(args: argparse.Namespace) -> int:
    settings = _settings(args)
    database = Database(settings.database_path, settings.max_queue)
    database.initialize()
    try:
        job = enqueue_heartbeat(settings, database, force=args.force)
    except QueueFullError as exc:
        print(json.dumps({"queued": False, "error": str(exc)}))
        return 1
    print(json.dumps({"queued": job is not None, "job_id": job.id if job else None}))
    return 0


async def authenticate_async(settings: Settings) -> int:
    environment = os.environ.copy()
    environment["CODEX_HOME"] = str(settings.codex_home)
    settings.codex_home.mkdir(parents=True, exist_ok=True, mode=0o700)
    async with AsyncCodex(CodexConfig(cwd=str(settings.workspace), env=environment)) as codex:
        handle = await codex.login_chatgpt_device_code()
        print(f"Open: {handle.verification_url}")
        print(f"Code: {handle.user_code}")
        await handle.wait()
        account = await codex.account(refresh_token=True)
        if account.account is None:
            print("Codex login did not produce an authenticated account.", file=sys.stderr)
            return 1
    print("Codex authentication completed.")
    database = Database(settings.database_path, settings.max_queue)
    database.initialize()
    database.set_metadata("authentication_degraded", False)
    return 0


def authenticate(args: argparse.Namespace) -> int:
    return asyncio.run(authenticate_async(_settings(args)))


async def discover_telegram_id_async(settings: Settings) -> int:
    bot = Bot(settings.read_telegram_token())
    print("Send a private message to the bot within 60 seconds…")
    deadline = asyncio.get_running_loop().time() + 60
    offset: int | None = None
    while asyncio.get_running_loop().time() < deadline:
        updates = await bot.get_updates(offset=offset, timeout=10, allowed_updates=["message"])
        for update in updates:
            offset = update.update_id + 1
            if update.effective_user and update.effective_chat:
                print(
                    json.dumps(
                        {
                            "user_id": update.effective_user.id,
                            "chat_id": update.effective_chat.id,
                            "chat_type": update.effective_chat.type,
                        }
                    )
                )
                return 0
    print("No Telegram message received.", file=sys.stderr)
    return 1


def discover_telegram_id(args: argparse.Namespace) -> int:
    return asyncio.run(discover_telegram_id_async(_settings(args)))


async def doctor_async(
    settings: Settings,
    offline: bool,
    skip_telegram: bool = False,
) -> list[Check]:
    checks: list[Check] = []
    checks.append(Check("workspace", settings.workspace.is_dir(), str(settings.workspace)))
    for name, path in (
        ("database_parent", settings.database_path.parent),
        ("durable_dir", settings.durable_dir),
        ("codex_home", settings.codex_home),
    ):
        checks.append(Check(name, path.is_dir() and os.access(path, os.W_OK), str(path)))
    database = Database(settings.database_path, settings.max_queue)
    try:
        database.initialize()
        checks.append(Check("database", True, str(settings.database_path)))
    except Exception as exc:
        checks.append(Check("database", False, str(exc)))

    token: str | None = None
    if offline or skip_telegram:
        checks.append(Check("telegram_token", True, "online validation skipped"))
    else:
        try:
            token = settings.read_telegram_token()
            token_path = settings.telegram_token_file
            if token_path.exists():
                mode = stat.S_IMODE(token_path.stat().st_mode)
                checks.append(Check("telegram_token_mode", mode & 0o077 == 0, oct(mode)))
            else:
                checks.append(Check("telegram_token", True, "loaded from systemd credentials"))
        except ConfigError as exc:
            checks.append(Check("telegram_token", False, str(exc)))

    if token:
        try:
            bot = Bot(token)
            identity = await bot.get_me()
            checks.append(Check("telegram_api", True, f"@{identity.username}"))
        except Exception as exc:
            checks.append(Check("telegram_api", False, safe_error(exc)))

    if not offline:
        runtime = CodexRuntime(
            settings.workspace,
            settings.codex_home,
            timeout_seconds=settings.turn_timeout_seconds,
            model=settings.model,
            reasoning_effort=settings.reasoning_effort,
        )
        try:
            authenticated = await runtime.authenticated(refresh=True)
            detail = "authenticated" if authenticated else "missing"
            checks.append(Check("codex_auth", authenticated, detail))
        except Exception as exc:
            checks.append(Check("codex_auth", False, str(exc)))
        finally:
            await runtime.close()
    return checks


def doctor(args: argparse.Namespace) -> int:
    checks = asyncio.run(doctor_async(_settings(args), args.offline, args.skip_telegram))
    if args.json:
        print(json.dumps([asdict(check) for check in checks], indent=2))
    else:
        for check in checks:
            print(f"{'ok' if check.ok else 'FAIL':4} {check.name}: {check.detail}")
    return 0 if all(check.ok for check in checks) else 1


def backup(args: argparse.Namespace) -> int:
    settings = _settings(args)
    result = perform_backup(settings.durable_dir, settings.backup_checkout, push=not args.no_push)
    print(json.dumps(asdict(result)))
    return 0


def restore(args: argparse.Namespace) -> int:
    settings = _settings(args)
    manifest = restore_backup(settings.backup_checkout, settings.durable_dir)
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


def initialize_database(args: argparse.Namespace) -> int:
    settings = _settings(args)
    Database(settings.database_path, settings.max_queue).initialize()
    print(settings.database_path)
    return 0


def bridge(args: argparse.Namespace) -> int:
    settings = _settings(args)
    prompt = sys.stdin.read().strip()
    if not prompt:
        print("Bridge prompt is empty.", file=sys.stderr)
        return 2
    if len(prompt) > settings.max_input_chars:
        print(
            f"Bridge prompt exceeds {settings.max_input_chars} characters.",
            file=sys.stderr,
        )
        return 2

    database = Database(settings.database_path, settings.max_queue)
    database.initialize()
    try:
        job = database.enqueue("telegram", prompt)
    except QueueFullError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if job is None:  # pragma: no cover - enqueue without deduplication always returns a job
        print("Bridge job was not queued.", file=sys.stderr)
        return 1

    deadline = time.monotonic() + args.wait_timeout
    while time.monotonic() < deadline:
        current = database.get_job(job.id)
        if current is None:
            print(f"Bridge job #{job.id} disappeared.", file=sys.stderr)
            return 1
        if current.status == "completed":
            print(current.response or "")
            return 0
        if current.status in {"failed", "cancelled", "uncertain"}:
            detail = current.error or "unknown error"
            print(f"Bridge job #{job.id} {current.status}: {detail}", file=sys.stderr)
            return 1
        time.sleep(0.5)

    print(
        f"Timed out waiting for bridge job #{job.id}; it remains queued or running.",
        file=sys.stderr,
    )
    return 124


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentctl")
    parser.add_argument("--config", help="configuration path")
    parser.add_argument("--verbose", action="store_true")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("run", help="run the Telegram gateway").set_defaults(func=run_agent)
    heartbeat = subparsers.add_parser("heartbeat", help="enqueue a heartbeat")
    heartbeat.add_argument("--force", action="store_true")
    heartbeat.set_defaults(func=queue_heartbeat)
    subparsers.add_parser("auth", help="authenticate Codex with a device code").set_defaults(
        func=authenticate
    )
    subparsers.add_parser("telegram-id", help="print the next Telegram sender ID").set_defaults(
        func=discover_telegram_id
    )
    doctor_parser = subparsers.add_parser("doctor", help="validate installation health")
    doctor_parser.add_argument("--offline", action="store_true")
    doctor_parser.add_argument(
        "--skip-telegram",
        action="store_true",
        help="skip Telegram validation but still check Codex authentication",
    )
    doctor_parser.add_argument("--json", action="store_true")
    doctor_parser.set_defaults(func=doctor)
    backup_parser = subparsers.add_parser("backup", help="back up durable state")
    backup_parser.add_argument("--no-push", action="store_true")
    backup_parser.set_defaults(func=backup)
    subparsers.add_parser("restore", help="restore durable state").set_defaults(func=restore)
    subparsers.add_parser("init-db", help="initialize the runtime database").set_defaults(
        func=initialize_database
    )
    bridge_parser = subparsers.add_parser(
        "bridge", help="queue a Telegram-thread turn and print its response"
    )
    bridge_parser.add_argument("--wait-timeout", type=int, default=3000)
    bridge_parser.set_defaults(func=bridge)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    configure_logging(args.verbose)
    try:
        code = args.func(args)
    except (ConfigError, BackupError) as exc:
        LOGGER.error("%s", exc)
        code = 2
    raise SystemExit(code)
