"""Private event-driven submission and completion socket, owned by the queue daemon."""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import os
import socket
import time
import uuid
from pathlib import Path
from typing import Any

from home_agent.database import Database
from home_agent.performance import accepted


class ControlServer:
    def __init__(self, database: Database, worker: Any, path: Path):
        self.database, self.worker, self.path = database, worker, path
        self.changed = asyncio.Event()
        self.server: asyncio.AbstractServer | None = None
        self.lock_fd: int | None = None
        self.heartbeat: Any = None

    async def start(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_fd = os.open(str(self.path) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self.lock_fd)
            self.lock_fd = None
            raise RuntimeError("Another queue daemon owns the socket") from None
        self.path.unlink(missing_ok=True)
        self.server = await asyncio.start_unix_server(self.handle, path=self.path, limit=20000)
        os.chmod(self.path, 0o600)

    def notify(self) -> None:
        self.changed.set()

    async def close(self) -> None:
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        if self.lock_fd is not None:
            self.path.unlink(missing_ok=True)
            os.close(self.lock_fd)
            self.lock_fd = None

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        received = time.monotonic_ns()
        try:
            payload = json.loads(await asyncio.wait_for(reader.readline(), 5))
            if payload.get("op") == "wake":
                self.worker.wake()
                writer.write(b'{"ok":true}\n')
                await writer.drain()
                return
            if payload.get("op") == "heartbeat":
                job = await asyncio.to_thread(self.heartbeat, bool(payload.get("force")))
                self.worker.wake()
                writer.write(
                    json.dumps(
                        {"queued": job is not None, "job_id": job.id if job else None}
                    ).encode()
                    + b"\n"
                )
                await writer.drain()
                return
            prompt = payload["prompt"]
            if not isinstance(prompt, str) or not 1 <= len(prompt) <= 12000:
                raise ValueError("Invalid prompt")
            key = payload["request_id"]
            if not isinstance(key, str) or len(key) > 80:
                raise ValueError("Invalid request ID")
            job = self.database.enqueue("telegram", prompt, submission_key=key)
            assert job
            accepted(self.database, job, received, payload.get("source", "bridge"))
            self.worker.wake()
            writer.write(json.dumps({"job_id": job.id}).encode() + b"\n")
            await writer.drain()
            while True:
                self.changed.clear()
                current = self.database.get_job(job.id)
                if current and current.status not in ("queued", "running"):
                    writer.write(
                        json.dumps(
                            {
                                "job_id": job.id,
                                "status": current.status,
                                "response": current.response,
                                "error": current.error,
                            }
                        ).encode()
                        + b"\n"
                    )
                    await writer.drain()
                    return
                await self.changed.wait()
        except Exception as exc:
            writer.write(json.dumps({"error": type(exc).__name__}).encode() + b"\n")
            with contextlib.suppress(ConnectionError, OSError):
                await writer.drain()
        finally:
            writer.close()


def submit(
    path: Path,
    prompt: str,
    timeout: float = 3000,
    source: str = "bridge",
    request_id: str | None = None,
) -> dict[str, Any]:
    with socket.socket(socket.AF_UNIX) as sock:
        sock.settimeout(timeout)
        sock.connect(str(path))
        sock.sendall(
            json.dumps(
                {"prompt": prompt, "request_id": request_id or str(uuid.uuid4()), "source": source}
            ).encode()
            + b"\n"
        )
        with sock.makefile("rb") as stream:
            first: dict[str, Any] = json.loads(stream.readline())
            if "error" in first:
                return first
            result: dict[str, Any] = json.loads(stream.readline())
            return result


def wake(path: Path) -> None:
    with socket.socket(socket.AF_UNIX) as sock:
        sock.settimeout(3)
        sock.connect(str(path))
        sock.sendall(b'{"op":"wake"}\n')
        sock.recv(1024)


def heartbeat(path: Path, force: bool) -> dict[str, Any]:
    with socket.socket(socket.AF_UNIX) as sock:
        sock.settimeout(30)
        sock.connect(str(path))
        sock.sendall(json.dumps({"op": "heartbeat", "force": force}).encode() + b"\n")
        with sock.makefile("rb") as stream:
            result: dict[str, Any] = json.loads(stream.readline())
            return result
