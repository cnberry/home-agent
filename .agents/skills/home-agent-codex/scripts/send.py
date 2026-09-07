#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Send a queued turn to Home Agent Codex")
    parser.add_argument("message", nargs="*")
    return parser.parse_args()


def ssh_host() -> str:
    host = os.environ.get("HOME_AGENT_SSH_HOST", "").strip()
    if not host:
        configured_path = os.environ.get("HOME_AGENT_SSH_HOST_FILE")
        path = (
            Path(configured_path)
            if configured_path
            else Path.home() / ".config/home-agent/ssh-host"
        )
        try:
            host = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ValueError(
                f"Home Agent SSH destination is not configured in {path}: {exc}"
            ) from exc
    if not host or any(character.isspace() for character in host) or host.startswith("-"):
        raise ValueError("Home Agent SSH destination is invalid")
    return host


def main() -> int:
    args = parse_args()
    message = " ".join(args.message) if args.message else sys.stdin.read()
    for prefix in (
        "@homeAgentCodex",
        "$home-agent-codex",
        "@strawberryCodex",
        "$strawberry-codex",
    ):
        if message.lower().startswith(prefix.lower()) and (
            len(message) == len(prefix) or message[len(prefix)] in " :,-\n\r\t"
        ):
            message = message[len(prefix) :].lstrip(" :,-\n")
            break
    if not message.strip():
        print("No message was provided for Home Agent Codex.", file=sys.stderr)
        return 2

    try:
        host = ssh_host()
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    command = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=8",
        "-o",
        "ServerAliveInterval=30",
        "-o",
        "ServerAliveCountMax=4",
        "--",
        host,
        "home-agent-codex-bridge",
    ]
    try:
        result = subprocess.run(
            command,
            input=message,
            text=True,
            check=False,
            capture_output=True,
        )
    except OSError as exc:
        print(f"Could not start SSH: {exc}", file=sys.stderr)
        return 1
    if result.returncode == 0:
        print(f"[HomeAgentCodex] {result.stdout.rstrip()}")
    else:
        sys.stderr.write(result.stderr)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
