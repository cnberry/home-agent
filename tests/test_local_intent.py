"""Local worker contract: explicit targets, verified results, socket wakeups, no replay."""

import json
import os
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock

import pytest

from home_agent.local_intent import handle, request, switch


@pytest.mark.parametrize(
    "prompt,action,live,expected",
    [
        ("Turn on test lights", "on", True, True),
        ("Turn off test lights", "off", True, True),
        ("Turn off test lights", "off", False, False),
        ("Are test lights on?", "clarify", True, False),
        ("Turn it on", "on", True, False),
        ("Turn on bedroom lights", "on", True, False),
    ],
)
def test_only_named_live_actions_execute(prompt, action, live, expected):
    classifier = Mock(config={"device_names": ["test lights"]})
    classifier.classify.return_value = (action, {}, {})
    execute = Mock(return_value={"enabled": action == "on", "reachable": True})
    result = handle({"prompt": prompt, "execute": live}, classifier, execute)
    assert result["executed"] == expected
    assert execute.called == expected


@pytest.mark.parametrize(
    "response",
    [
        [{"id": "test", "enabled": False, "reachable": True}],
        [{"id": "test", "enabled": True, "reachable": False}],
        [{"id": "wrong", "enabled": True, "reachable": True}],
        [],
    ],
)
def test_unverified_device_response_cannot_succeed(monkeypatch, response):
    monkeypatch.setattr(
        "home_agent.local_intent.subprocess.run",
        Mock(return_value=Mock(stdout=json.dumps(response))),
    )
    with pytest.raises(RuntimeError):
        switch("on", {"device_id": "test"})


def test_socket_worker_wakes_after_idle_and_does_not_replay_failure(tmp_path):
    class Model(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            response = json.dumps(
                {
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {"content": '{"action":"on"}'},
                        }
                    ]
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(response)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(response)

        def log_message(self, *_):
            pass

    model = ThreadingHTTPServer(("127.0.0.1", 0), Model)
    thread = threading.Thread(target=model.serve_forever, daemon=True)
    thread.start()
    writes = tmp_path / "writes"
    device = tmp_path / "fake-switch"
    device.write_text(
        f"#!{sys.executable}\nfrom pathlib import Path\n"
        f"with Path({str(writes)!r}).open('a') as f: f.write('attempt\\n')\n"
        "raise SystemExit(1)\n"
    )
    device.chmod(0o700)
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "port": model.server_port,
                "device_id": "test",
                "device_names": ["test lights"],
                "switchctl": str(device),
            }
        )
    )
    path = tmp_path / "private" / "worker.sock"
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as ready:
        ready.bind(str(tmp_path / "ready"))
        ready.settimeout(10)
        env = dict(os.environ, NOTIFY_SOCKET=str(tmp_path / "ready"))
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "home_agent.local_intent",
                "--serve",
                "--config",
                str(config),
                "--socket",
                str(path),
            ],
            env=env,
        )
        try:
            assert ready.recv(64) == b"READY=1"
            assert path.stat().st_mode & 0o777 == 0o600
            assert request("Turn on test lights", socket_path=path)["action"] == "on"
            # The model closes HTTP after each reply: a later request must still work.
            assert request("Turn on test lights", socket_path=path)["action"] == "on"
            result = request("Turn on test lights", execute=True, socket_path=path)
            assert "error" in result
            assert writes.read_text() == "attempt\n"
            assert (
                request("Turn on other lights", execute=True, socket_path=path)["action"]
                == "clarify"
            )
            assert writes.read_text() == "attempt\n"
        finally:
            process.terminate()
            process.wait(timeout=5)
            model.shutdown()
            model.server_close()
            thread.join(timeout=5)
