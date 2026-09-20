# Initial CPU routing baseline

Measured 2026-09-20 against implementation commit
`d2e4e48996d7dfb0919cc535f9b17c50dea57671` with Qwen3.5-2B-Q4_K_M,
pinned llama.cpp b10964, 8 CPU threads, and the production daemon queue.
The host uses an Intel i7-12700K; inference did not use a GPU.

Twenty sequential alternating commands to one authorized LAN light all produced
verified native readback. All 20 CLI launches occurred within one second of host
receipt. The first uncached request is included; the device's initial state was
restored. Raw evidence is private. These are bridge-to-daemon timings, not
millisecond phone-to-Telegram measurements.

| Duration | Median | p95 | Maximum |
| --- | ---: | ---: | ---: |
| Durable acceptance | 6.636 ms | 9.534 ms | 18.383 ms |
| Receipt to queue claim | 13.663 ms | 19.245 ms | 34.803 ms |
| Qwen inference | 342.846 ms | 356.354 ms | 732.714 ms |
| Receipt to native CLI launch | 440.489 ms | 451.209 ms | 850.880 ms |
| Receipt to verified completion | 747.868 ms | 1,508.616 ms | 1,712.954 ms |

Five read-only device-family checks succeeded, as did clarification guards and
normal Codex fallback. A deliberate model outage sent a read-only request through
Codex in 5.426 seconds; the resident model was restored afterward. Fault injection
has a separate measurement source. One result delivered through the production
Telegram outbox on its first attempt: API duration 891.451 ms, host receipt to
API acknowledgement 1,817.650 ms. That test used synthetic daemon intake.

The guarded mixed-language evaluation passed 29/29 scenarios with device
execution simulated. This is routing-plus-validation accuracy, not an independent
claim about the model's raw classifications. Local checks passed 145 Home Agent
Python tests, lint and type checks; the private adapter suite passed 98 panel
Python tests plus repository/installer/JavaScript checks. Public CI passed on
Python 3.10, 3.12 and 3.14, including clean Linux installation checks.

Twenty successes do not establish >99% production reliability. Native preflight
reads, physical device latency and FIFO contention remain significant: one climate
status query spent about 5.9 seconds inside its native adapter. Do not apply the
light's issue latency result to all capability families.

For subsequent iterations, keep model/runtime/catalog/release versions, source,
job/attempt identifiers, and the original sample window. Compare p50/p95/max and
missing timing counts, clarification/fallback rates and verified outcomes using
`agentctl performance`. Retain failed trials; separate injected faults and
synthetic benchmarks from real Telegram traffic. See the
[architecture](qwen-routing.md#performance-evidence-and-goals) for exact boundaries.

A subsequent real owner Telegram message reached the production Qwen route:
durable acceptance 30.238 ms, queue receipt to claim 42.756 ms, inference 350.603 ms,
and verified read-only completion 954.162 ms. This confirms real Telegram intake,
not just the synthetic submission path. These durations start at host receipt;
this sample's Telegram delivery duration was not extracted from the private outbox.
