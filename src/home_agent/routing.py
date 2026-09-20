"""Qwen interpretation and a fixed, separately owned home-config execution adapter."""

from __future__ import annotations

import asyncio
import contextlib
import http.client
import json
import math
import os
import re
import signal
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from home_agent.performance import Performance


class LocalRouter:
    def __init__(self, path: Path):
        self.config = json.loads(path.read_text())
        self.catalog = json.loads(Path(self.config["catalog"]).read_text())
        self.mode = self.config.get("mode", "qwen-first")
        if self.mode not in ("qwen-first", "observe", "codex-only"):
            raise ValueError("Invalid routing mode")
        self.process: asyncio.subprocess.Process | None = None
        self.interrupted = False
        self.failures = 0
        self.retry_at = 0.0

    def candidates(self, prompt: str) -> list[dict[str, Any]]:
        matches = []
        for c in self.catalog["capabilities"]:
            for name in c["aliases"]:
                for m in re.finditer(r"(?<!\w)" + re.escape(name) + r"(?!\w)", prompt, re.I):
                    matches.append((m.start(), m.end(), c))
        result = {}
        for start, end, c in matches:
            if not any(a <= start and end <= b and b - a > end - start for a, b, _ in matches):
                result[c["id"]] = c
        return list(result.values())

    def classify(self, prompt: str, candidates: list[dict[str, Any]]) -> dict[str, Any]:
        options = [{"op": i + 1, "request": c["label"]} for i, c in enumerate(candidates)]
        instruction = (
            "Translate the user request into ONE operation below. Output JSON with "
            "only op (the operation number). "
            "op=0 means clarify/no action. op=-1 means complex request for another agent. "
            "Questions about current state use status. Do not act on negations, "
            "hypothetical, quoted or future commands. "
            "Temperature requests select the temperature operation.\n"
            + json.dumps(options, separators=(",", ":"))
        )
        body = {
            "messages": [
                {"role": "system", "content": instruction},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0,
            "max_tokens": 12,
            "cache_prompt": True,
            "chat_template_kwargs": {"enable_thinking": False},
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "intent",
                    "schema": {
                        "type": "object",
                        "properties": {
                            "op": {"type": "integer", "enum": [-1, 0, *range(1, len(options) + 1)]},
                        },
                        "required": ["op"],
                        "additionalProperties": False,
                    },
                },
            },
        }
        conn = http.client.HTTPConnection(
            "127.0.0.1",
            self.config.get("port", 18081),
            timeout=self.config.get("inference_timeout_seconds", 1.5),
        )
        try:
            conn.request(
                "POST",
                "/v1/chat/completions",
                json.dumps(body),
                {"Content-Type": "application/json"},
            )
            response = conn.getresponse()
            data = json.loads(response.read())
            if response.status != 200 or data["choices"][0]["finish_reason"] != "stop":
                raise ValueError("Incomplete inference")
            result: dict[str, Any] = json.loads(data["choices"][0]["message"]["content"])
            result["model_timings"] = data.get("timings", {})
            return result
        finally:
            conn.close()

    async def run(
        self, prompt: str, perf: Performance, before_dispatch: Callable[[], None]
    ) -> dict[str, Any]:
        self.interrupted = False
        if self.mode == "codex-only":
            return {"outcome": "fallback", "reason": "codex_only"}
        candidates = self.candidates(prompt)
        if not candidates:
            if re.search(
                r"\b(lights?|tv|pool|spa|hot tub|gate|thermostat|air conditioner)\b", prompt, re.I
            ):
                return {
                    "outcome": "clarify",
                    "reply": "Which configured device and action do you mean?",
                }
            return {"outcome": "fallback", "reason": "outside_catalog"}
        if len({(c["service"], c["target"]) for c in candidates}) != 1 or re.search(
            r"\b(and|or|then)\b", prompt, re.I
        ):
            return {"outcome": "fallback", "reason": "multiple_targets"}
        perf.data.update(
            engine="qwen",
            catalog_version=self.catalog["version"],
            model=self.config.get("model", "qwen-local"),
            eligible=True,
        )
        if time.monotonic() < self.retry_at:
            return {"outcome": "fallback", "reason": "local_circuit_open"}
        started = time.monotonic_ns()
        try:
            decision = await asyncio.to_thread(self.classify, prompt, candidates)
        except Exception as exc:
            self.failures += 1
            if self.failures >= 3:
                self.retry_at = time.monotonic() + 30
            perf.data["inference_ms"] = (time.monotonic_ns() - started) / 1e6
            return {"outcome": "fallback", "reason": "local_inference_" + type(exc).__name__}
        self.failures = 0
        self.retry_at = 0.0
        perf.data["inference_ms"] = (time.monotonic_ns() - started) / 1e6
        perf.event("interpreted", decision=decision)
        if self.interrupted:
            return {"outcome": "cancelled", "reply": "Cancelled before dispatch."}
        op = decision.get("op")
        if self.mode == "observe":
            return {"outcome": "fallback", "reason": "observe_mode"}
        if op == -1:
            return {"outcome": "fallback", "reason": "complex_request"}
        if type(op) is not int or not 1 <= op <= len(candidates):
            return {
                "outcome": "clarify",
                "reply": "Please specify the device and the immediate action you want.",
            }
        c = candidates[op - 1]
        perf.data.update(capability=c["id"], mutation=c["mutation"])
        # Explicit negative/conditional requests cannot become immediate local writes.
        if c["mutation"] and re.search(
            r"\b(don't|do not|never|not|tomorrow|later|if|when|then|unless|after|before|"
            r"how|explain|example|say|said|quote|pretend|would)\b",
            prompt,
            re.I,
        ):
            return {
                "outcome": "clarify",
                "reply": "Please confirm the immediate action; I have not changed anything.",
            }
        value = None
        if c.get("parameter") == "temperature":
            numbers = [float(x) for x in re.findall(r"(?<!\w)-?\d+(?:\.\d+)?", prompt)]
            value = numbers[0] if len(numbers) == 1 else None
            decision["value"] = value
            units = re.findall(
                r"(?:\d\s*°?\s*|\b)([FCK])\b|\b(celsius|fahrenheit|kelvin)\b", prompt, re.I
            )
            units = [letter or word[0] for letter, word in units]
            decision["unit"] = units[0].upper() if units else c["unit"]
            if (
                type(value) not in (int, float)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value not in numbers
                or not c["min"] <= value <= c["max"]
                or not float(value).is_integer()
                or decision.get("unit", c["unit"]) != c["unit"]
            ):
                return {
                    "outcome": "clarify",
                    "reply": (
                        f"Please specify a temperature between {c['min']} "
                        f"and {c['max']} {c['unit']}."
                    ),
                }
        elif c.get("parameter") == "mode":
            modes = [
                m
                for m in c["enum"]
                if re.search(
                    r"(?<!\w)" + re.escape(m).replace(r"\-", "[- ]") + r"(?!\w)", prompt, re.I
                )
            ]
            modes = [m for m in modes if not any(m != n and m in n for n in modes)]
            if len(modes) != 1:
                return {"outcome": "clarify", "reply": "Please specify a supported heating mode."}
            decision["value"] = modes[0]
        elif not c.get("parameter") and ("value" in decision or "unit" in decision):
            return {
                "outcome": "clarify",
                "reply": "Please specify one supported action without extra parameters.",
            }
        payload = {"capability": c["id"], "catalog_version": self.catalog["version"]}
        payload.update({k: decision[k] for k in ("value", "unit") if k in decision})
        perf.event("validated", capability=c["id"], mutation=c["mutation"])
        result = await self.execute(payload, perf, before_dispatch)
        if result["outcome"] == "not_sent":
            if result.get("error_type") == "ValueError":
                return {
                    "outcome": "clarify",
                    "reply": "That action is unavailable. Please check the device status.",
                }
            return {"outcome": "fallback", "reason": "executor_not_sent"}
        if result["outcome"] == "uncertain":
            # Enforced read-only recovery: same adapter, read_only path, no GO permission.
            recovery = await self.execute(
                {**payload, "read_only": True},
                perf,
                lambda: (_ for _ in ()).throw(RuntimeError("Recovery cannot write")),
            )
            perf.event("reconciled", outcome=recovery["outcome"])
            if recovery["outcome"] == "observed":
                result["reply"] += " Current observation: " + recovery["reply"]
        return result

    async def execute(
        self, payload: dict[str, Any], perf: Performance, before_dispatch: Callable[[], None]
    ) -> dict[str, Any]:
        started = time.monotonic_ns()
        dispatched = False
        process = await asyncio.create_subprocess_exec(
            *self.config["executor"],
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        self.process = process
        assert process.stdin is not None and process.stdout is not None
        process.stdin.write(json.dumps(payload).encode() + b"\n")
        await process.stdin.drain()
        try:
            async with asyncio_timeout(self.config.get("executor_timeout_seconds", 180)):
                while line := await process.stdout.readline():
                    event: dict[str, Any] = json.loads(line)
                    if event["event"] == "dispatch_ready":
                        if self.interrupted:
                            raise RuntimeError("Cancelled before dispatch")
                        before_dispatch()
                        dispatched = True
                        perf.event("action_dispatch_started", capability=payload["capability"])
                        process.stdin.write(b"GO\n")
                        await process.stdin.drain()
                    elif event["event"] == "driver_issued":
                        perf.issued(event["monotonic_ns"])
                    elif event["event"] == "result":
                        await process.wait()
                        perf.data["device_ms"] = (time.monotonic_ns() - started) / 1e6
                        return event
                raise RuntimeError("Adapter ended without result")
        except Exception as exc:
            return {
                "outcome": "uncertain" if dispatched else "not_sent",
                "reply": "Device state is uncertain; no action was repeated."
                if dispatched
                else "The request was not sent.",
                "error_type": type(exc).__name__,
            }
        finally:
            if process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGTERM)
                await process.wait()
            self.process = None

    async def interrupt(self) -> None:
        self.interrupted = True
        if self.process and self.process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.process.pid, signal.SIGTERM)


# Python 3.10-compatible deadline around a serial adapter protocol.
class asyncio_timeout:
    def __init__(self, seconds: float):
        self.seconds = seconds
        self.handle: asyncio.TimerHandle | None = None
        self.expired = False

    async def __aenter__(self) -> None:
        task = asyncio.current_task()

        def expire() -> None:
            self.expired = True
            if task:
                task.cancel()

        self.handle = asyncio.get_running_loop().call_later(self.seconds, expire)

    async def __aexit__(self, *_: Any) -> None:
        if self.handle:
            self.handle.cancel()
        if self.expired:
            raise TimeoutError("Adapter deadline exceeded")
