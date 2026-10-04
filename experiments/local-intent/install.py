"""Install the pinned CPU experiment as user services; Python 3.12+ on Linux x86-64."""

import argparse
import hashlib
import json
import shutil
import subprocess
import tarfile
import urllib.request
from pathlib import Path

RELEASE = "b10964"
ARCHIVE = "llama-b10964-bin-ubuntu-x64.tar.gz"
RUNTIME_SHA = "9abf88aea48a55d0f80edb1ee20220b186848cca0b4e919d71518cfd7ca67443"
MODELS = {
    "0.8b": (
        "ggml-org/Qwen3.5-0.8B-GGUF",
        "8fea620810c4afa23dd6443f999a48574c1611a3",
        "Qwen3.5-0.8B-Q8_0.gguf",
        "37ae482d336108d23516fa35e8e0c4126688d81018b87178a18d752a1357814f",
    ),
    "2b": (
        "unsloth/Qwen3.5-2B-GGUF",
        "f6d5376be1edb4d416d56da11e5397a961aca8ae",
        "Qwen3.5-2B-Q4_K_M.gguf",
        "aaf42c8b7c3cab2bf3d69c355048d4a0ee9973d48f16c731c0520ee914699223",
    ),
}


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def download(url, destination, expected):
    if not destination.exists():
        partial = destination.with_suffix(".part")
        urllib.request.urlretrieve(url, partial)
        if digest(partial) != expected:
            raise RuntimeError(f"Checksum mismatch: {destination.name}")
        partial.rename(destination)
    if digest(destination) != expected:
        raise RuntimeError(f"Checksum mismatch: {destination.name}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root", type=Path, default=Path.home() / ".local/share/home-agent-local-intent"
    )
    parser.add_argument("--model", choices=MODELS, default="2b")
    parser.add_argument("--device-id", required=True)
    parser.add_argument("--device-name", action="append", required=True)
    parser.add_argument("--threads", type=int, default=8)
    args = parser.parse_args()
    root = args.root.expanduser().resolve()
    if any(c.isspace() for c in str(root)) or "%" in str(root):
        parser.error("Installation root cannot contain whitespace or percent signs")
    if args.threads < 1:
        parser.error("--threads must be positive")
    root.mkdir(parents=True, exist_ok=True)
    repo, revision, filename, sha = MODELS[args.model]
    archive = root / ARCHIVE
    download(
        f"https://github.com/ggml-org/llama.cpp/releases/download/{RELEASE}/{ARCHIVE}",
        archive,
        RUNTIME_SHA,
    )
    binary = root / "runtime" / f"llama-{RELEASE}" / "llama-server"
    # Extract the verified archive on every install, repairing modified runtime files.
    with tarfile.open(archive) as bundle:
        bundle.extractall(root / "runtime", filter="data")
    download(f"https://huggingface.co/{repo}/resolve/{revision}/{filename}", root / filename, sha)
    checkout = Path(__file__).resolve().parents[2]
    source = checkout / "src/home_agent/local_intent.py"
    shutil.copy2(source, root / "local_intent.py")
    config = root / "device.json"
    config.write_text(
        json.dumps({"device_id": args.device_id, "device_names": args.device_name}, indent=2) + "\n"
    )
    config.chmod(0o600)
    model_service = f"""[Unit]
Description=Home Agent experimental CPU intent model
[Service]
ExecStart={binary} -m {root / filename} --host 127.0.0.1 --port 18081 \\
    -ngl 0 -t {args.threads} -tb {args.threads} -c 2048 -np 1 \\
    --poll 0 --poll-batch 0 --reasoning off --threads-http 2
WorkingDirectory={root}
Restart=on-failure
RestartSec=3
UMask=0077
[Install]
WantedBy=default.target
"""
    worker_service = f"""[Unit]
Description=Home Agent experimental local intent socket worker
After=home-agent-local-model.service
Requires=home-agent-local-model.service
[Service]
Type=notify
ExecStart=/usr/bin/python3 {root / "local_intent.py"} --serve --config {config}
WorkingDirectory={root}
Restart=on-failure
RestartSec=3
UMask=0077
[Install]
WantedBy=default.target
"""
    units = Path.home() / ".config/systemd/user"
    units.mkdir(parents=True, exist_ok=True)
    for name, contents in [
        ("home-agent-local-model", model_service),
        ("home-agent-local-intent", worker_service),
    ]:
        path = units / (name + ".service")
        path.write_text(contents)
    commit = subprocess.check_output(
        ["git", "-C", str(checkout), "rev-parse", "HEAD"], text=True
    ).strip()
    manifest = {
        "runtime_release": RELEASE,
        "runtime_sha256": RUNTIME_SHA,
        "model_repo": repo,
        "model_revision": revision,
        "model_file": filename,
        "model_sha256": sha,
        "source_commit": commit,
        "worker_sha256": digest(source),
        "threads": args.threads,
    }
    (root / "installation.json").write_text(json.dumps(manifest, indent=2) + "\n")
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(
        [
            "systemctl",
            "--user",
            "restart",
            "home-agent-local-model.service",
            "home-agent-local-intent.service",
        ],
        check=True,
    )
    print(json.dumps(manifest, indent=2))
    print("Services started. They are intentionally not enabled at login during the experiment.")


if __name__ == "__main__":
    main()
