# CPU local-intent experiment

This optional experiment classifies one explicitly named switch-backed device with
a resident Qwen model, runs `switchctl`, and returns the verified result. It is
separate from the production Codex/Telegram worker. It does not change that
worker's model, persistent conversations, queue, or SSH bridge.

For the approved production integration design, see the
[Qwen-first routing architecture](../../docs/architecture/qwen-routing.md),
including component, job, side-effect, and reply-delivery diagrams. The experiment
is a measured building block; it does not implement that architecture yet.

The worker waits in blocking Unix-socket accept/reads. Socket readiness wakes it
immediately; replies go directly to the caller. There is no job/reply polling
interval. llama.cpp runs with `--poll 0 --poll-batch 0`, disabling inference-thread
busy polling too. The model stays loaded while the service runs.

## Install and reproduce

Requirements: Linux x86-64, Python 3.12+, a working systemd user manager, outbound
HTTPS for installation, and configured `switchctl`. No root access, GPU, API key,
Python ML framework, or new agent framework is required.

Run from this repository on the machine controlling the device:

```bash
python3 experiments/local-intent/install.py \
  --device-id YOUR_SWITCH_ID \
  --device-name 'your device name' \
  --device-name 'alias one' --device-name 'alias two'
```

Default: Qwen3.5-2B Q4_K_M, eight CPU threads. `--model 0.8b` selects Qwen3.5-0.8B
Q8_0 for comparison. `--threads N` and `--root PATH` are configurable. The installer
pins the llama.cpp release and model revisions, verifies SHA-256 hashes, copies
the worker directly from this checkout, and writes an installation manifest with
the source commit and worker hash. It can be rerun. Device IDs/aliases go into a
private runtime config, not the repository.

The installer starts `home-agent-local-model.service` and
`home-agent-local-intent.service` as user services. They are not enabled at login
during the experiment. Check model readiness with
`curl http://127.0.0.1:18081/health`; wait for `{"status":"ok"}` before the first
request after startup. Model loading is distinct from warm latency.

Dry run (default) and explicitly authorized execution:

```bash
python3 ~/.local/share/home-agent-local-intent/local_intent.py \
  'Turn on your device name'
python3 ~/.local/share/home-agent-local-intent/local_intent.py --execute \
  'Turn on your device name'
```

To repeat the language checks using the three configured aliases:

```bash
PYTHONPATH=src python3 experiments/local-intent/evaluate.py \
  --device-name 'your device name' --aliases 'alias one' 'alias two' \
  --output /tmp/local-intent-accuracy.jsonl
```

After reviewing accuracy, add `--live` to send ten real alternating on/off commands,
starting on and ending off. This requires owner authorization. The measured socket
round trip includes inference, CLI startup, LAN write, state readback, and reply.
It excludes SSH setup and the two-second pause between requests. Keep raw outputs
private. The CLI itself adds interpreter startup beyond the socket timer.

Stop the experiment with:

```bash
systemctl --user stop home-agent-local-intent home-agent-local-model
```

## Execution boundaries

- The model outputs constrained JSON: `on`, `off`, or `clarify`. Thinking is off;
  the short static prompt is cached. There is no growing conversation or second
  inference to generate a confirmation.
- A configured device alias must occur explicitly in the request. This guard can
  reject a guessed target; it never chooses on/off instead of Qwen. Responses
  expose raw `model_action` separately from guarded `action`.
- Only a configured `switchctl` binary and fixed target can execute. There is no
  shell, arbitrary command generation, or model-controlled device ID.
- Success requires matching target, reachability, and requested power state from
  the CLI. This is device-reported state, not optical feedback.
- Socket directory/file modes are 0700/0600; the model API binds to loopback only.
  A process lock prevents two workers from owning the same socket.
- Failed actions are never retried. Failure after dispatch can leave state
  uncertain. This experiment has no durable queue/replay after restart and is
  not yet a production transport.
- The alias guard is not a general semantic safety guarantee. Expand evaluation
  before exposing the route to arbitrary requests or more devices.

## Initial measurements

An i7-12700K with 64 GB RAM, Qwen3.5-2B Q4_K_M, and eight CPU threads averaged
1.105 s end to end across ten alternating commands (0.984–1.266 s). Inference
averaged about 0.350 s. All ten device responses matched. This small warm sample
is not a reliable tail-latency estimate or a Telegram benchmark.

0.8B Q8 passed 14/24 raw language cases; 2B Q4_K_M passed 21/24. The explicit-device
guard raised the 2B worker result to 24/24. Report raw and guarded scores
separately. The evaluation set informed the guard, so it is not an independent
generalization score. No fine tuning was performed.

Sources: [llama.cpp server](https://github.com/ggml-org/llama.cpp/tree/master/tools/server),
[Qwen3.5-2B](https://huggingface.co/Qwen/Qwen3.5-2B), and
[pinned 2B GGUF](https://huggingface.co/unsloth/Qwen3.5-2B-GGUF/tree/f6d5376be1edb4d416d56da11e5397a961aca8ae).
