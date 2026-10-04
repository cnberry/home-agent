#!/usr/bin/env python3
"""Install the pinned CPU interpreter as an isolated, boot-enabled system service."""

import argparse
import json
import os
import runpy
import shutil
import subprocess
import tarfile
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("/opt/home-agent-local-model"))
    parser.add_argument("--cache", type=Path)
    parser.add_argument("--threads", type=int, default=8)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    if any(c.isspace() or c == "%" for c in str(root)) or args.threads < 1:
        parser.error("Use a simple absolute installation path and positive thread count")
    pins = runpy.run_path(
        str(Path(__file__).resolve().parents[1] / "experiments/local-intent/install.py")
    )
    repo, revision, filename, sha = pins["MODELS"]["2b"]
    manifest = {
        "runtime_release": pins["RELEASE"],
        "runtime_sha256": pins["RUNTIME_SHA"],
        "model_repo": repo,
        "model_revision": revision,
        "model_file": filename,
        "model_sha256": sha,
        "threads": args.threads,
    }
    if args.dry_run:
        print(json.dumps(manifest, indent=2))
        return
    if os.geteuid() != 0:
        parser.error("System installation requires root")
    root.mkdir(parents=True, exist_ok=True)
    for name, checksum, url in [
        (
            pins["ARCHIVE"],
            pins["RUNTIME_SHA"],
            f"https://github.com/ggml-org/llama.cpp/releases/download/{pins['RELEASE']}/{pins['ARCHIVE']}",
        ),
        (filename, sha, f"https://huggingface.co/{repo}/resolve/{revision}/{filename}"),
    ]:
        cached = args.cache / name if args.cache else None
        if cached and cached.is_file() and pins["digest"](cached) == checksum:
            shutil.copyfile(cached, root / name)
        pins["download"](url, root / name, checksum)
        (root / name).chmod(0o644)
    with tarfile.open(root / pins["ARCHIVE"]) as bundle:
        bundle.extractall(root / "runtime", filter="data")
    if subprocess.run(
        ["id", "home-agent-model"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    ).returncode:
        subprocess.run(
            [
                "useradd",
                "--system",
                "--no-create-home",
                "--shell",
                "/usr/sbin/nologin",
                "home-agent-model",
            ],
            check=True,
        )
    binary = root / "runtime" / f"llama-{pins['RELEASE']}" / "llama-server"
    unit = f"""[Unit]
Description=Home Agent pinned CPU Qwen interpreter
After=network.target
[Service]
User=home-agent-model
Group=home-agent-model
ExecStart={binary} -m {root / filename} --host 127.0.0.1 --port 18081 \\
    -ngl 0 -t {args.threads} -tb {args.threads} -c 2048 -np 1 \\
    --poll 0 --poll-batch 0 --reasoning off --threads-http 2
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
ProtectHome=true
ProtectSystem=strict
PrivateTmp=true
UMask=0077
[Install]
WantedBy=multi-user.target
"""
    Path("/etc/systemd/system/home-agent-model.service").write_text(unit)
    (root / "installation.json").write_text(json.dumps(manifest, indent=2) + "\n")
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "enable", "--now", "home-agent-model.service"], check=True)
    subprocess.run(["systemctl", "restart", "home-agent-model.service"], check=True)
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
