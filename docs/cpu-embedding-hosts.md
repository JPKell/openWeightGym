# CPU embedding hosts

LoadCoach serves an embedder with `gpus = 0`, `gpu-layers = 0` and
`embedding = true`. Turnstile's named-machine placement routes ordinary
`/v1/embeddings` requests to it with a machine-specific signed key. CPU-only
execution is an explicit model setting; a GPU model does not silently fall back
to CPU. Compute, front and remote workers now admit CPU model loads against observed
host RAM and existing service ceilings, and evict idle CPU models as described
below (deployed and endpoint-verified 2026-10-16).

For the 2026-10-30 native runtime refresh and rollback paths, see [installation](installation.md). The measurements below retain their original runtime/date.

## Live workers, 2026-10-16

Both workers serve the same `Qwen3-Embedding-0.6B-Q8_0.gguf` weights, whose
SHA-256 is `06507c7b42688469c4e7298b0a1e16deff06caf291cf0a5b278c308249c3e439`.
The CPU-only llama.cpp runtime reports build 906, commit `5ad1c5da0`. Each
worker has its own private key, model settings and data directory. Turnstile's
`[machines.front_cpu]` and `[machines.remote_cpu]` entries are in front's
private `weightroom.toml`; private pre-change copies were kept there. Both
machines appeared ready in System → Machines, with the common weights hash
grouped correctly.

| Worker | Endpoint | Model settings | Service limits | Verified request through front |
|---|---|---|---|---|
| `front_cpu` on front | `127.0.0.1:9442` | alias `qwen3-embedding-0.6b-cpu`; 8,192 context; 2 threads; 1 slot; last-token pooling | `loadcoach-front-cpu.service`: 200% CPU, 4G high / 6G maximum RAM, no swap | HTTP 200, 1,024 dimensions, 7 prompt tokens; warm call 0.337 s; service memory about 1.06 GB |
| `remote_cpu` on `kellrondgames.com` | Tailscale `100.69.178.99:9442` | alias `qwen3-embedding-0.6b-remote`; 2,048 context; 1 thread; 1 slot; last-token pooling | `loadcoach-remote-cpu.service`: 100% CPU, 1,500M high / 2,200M maximum RAM, no swap | HTTP 200, 1,024 dimensions, 10 prompt tokens; warm call 0.69 s; service memory about 352 MB |

Front's unit and settings live under `~/weightroom`; the remote worker uses
`~/suite-cpu`. Both user units are enabled and active. The remote user's
systemd linger is enabled so its unit survives logout. Front reaches the
remote worker over Tailscale, avoiding an unencrypted public-network route.
The temporary user API keys used for qualification were revoked afterward.
The benchmark queue had drained before activation and the Turnstile restarts.

## Validation, 2026-10-16

Run through front's turnstile with the owner's key on the throwaway installation.

- **Sustained, `front_cpu`:** 300 requests (about 125 tokens each, 4 concurrent) all returned
  HTTP 200 with 1,024 dimensions in 520 s. Latency was flat (p50 6.9 s, p95 7.0 s, max 8.6 s),
  which is about 1.7 s per request through the one slot, slower than the 0.34 s single short
  call. Service memory rose from 45 MB (model unloaded) to a 1.23 GB peak, under the 4G high
  limit; no restarts.
- **Outage, `front_cpu`:** with the service stopped, a request three seconds later returned
  **404** `The model ... does not exist`, in 0.35 s, while System → Machines still showed the
  machine ready. It did not hang or duplicate output, but 404 is the wrong refusal: a known
  worker that is down should give 503 with `Retry-After`. After restart the machine and model
  recovered and requests returned 200 within 25 s.
- **Same host, GPU LLM plus CPU embedder (compute):** `qwen3.6-35b-a3b-q4` resident on both
  cards, a temporary `gpus = 0`, `gpu-layers = 0`, 2-thread, 1-slot embedder of the same weights
  added then removed. A 1,500-token generation ran at 96.4 tok/s while 40 embedding requests
  completed (all 200, p50 0.55 s); the same generation alone ran at 83.4 tok/s. No measurable
  slowdown of the LLM. GPU memory did not change.

These checks establish routing, real embedding output on both CPU hosts and
simultaneous GPU LLM/CPU embedding traffic on compute. Remote-worker outage
and sustained memory qualification are complete in
[matching delivery and live checks](#matching-delivery-and-live-checks-2026-10-16).
Collections should remain pinned to their embedding fingerprint;
changing the model or its embedding configuration requires an explicit rebuild.

## RAM admission and idle eviction (deployed, 2026-10-16)

`host_ram_headroom_mib` defaults to **512 MiB** and is editable per LoadCoach under System → Compute nodes.
`host_ram_limit_mib` is also editable per node: `0` leaves the CPU-model-process
budget unset; a positive MiB value adds that budget. The process-RSS estimate
is compared with host `MemAvailable` minus headroom and the configured process
budget. Cgroup-v2 `memory.high`/`memory.max` room across ancestors is a separate
live gate: exhausted room waits, and actual charge is watched during loading and
requests. Process RSS cannot be numerically compared with cgroup charge.
Existing systemd CPU, RAM and swap limits remain authoritative and unchanged.
Host availability includes other programs and GPU model processes on that host.
Dry Fit selects one compute node at a time and draws CPU models against that node's
process-RSS planning limit, using measured resident peaks when available and the
conservative first-load bound otherwise; it shows the cgroup service ceiling separately.
Live verification on 2026-10-17 showed the configured three-node pills, valid CUDA0/CUDA1
capacity on `compute`, and effective 4,096 MiB / 1,500 MiB service ceilings on `front_cpu` /
`remote_cpu`; System → Compute nodes exposes each node's independent `host_ram_limit_mib` control.

Each model process and its children report current resident RAM in MiB; the
highest observation is retained in the existing model measurement record and
invalidated when model files or startup settings change. A later load reserves
that peak plus 10% and 64 MiB. First loads use model/projector file bytes plus
256 MiB as a provisional bound, serialize against other CPU loads, and check
room after replaced children exit, then actual host/service headroom before
serving. Loading reservations prevent a
second CPU load from spending the same observed room. Process RSS includes shared
mapped pages and can exceed unit-cgroup charged memory; `memory.high` is a soft
throttle and `memory.max` is the hard cap ([G18 unit pressure observation](../data/qualification/gates-2026-10-16/g18-unit-pressure-observations.json)).
Sampling and a first-load estimate cannot prove a hard future peak. Systemd
ceilings provide the final enforcement.

Insufficient room waits in the existing queue and returns its retryable 503 on
timeout. Admission can replace the oldest idle CPU residents; active requests,
holds, model locks, global locks and GPU residents stay protected. Independently,
`cpu_idle_minutes` defaults to **15 minutes** and automatically unloads idle CPU
residents with those protections; **0** disables that timer. GPU demand-driven
residency and `model_idle_minutes` retain their existing semantics. Neither setting
changes Spotter's sleep policy.

`/loadcoach/status` adds `system_memory` (host and service availability, headroom,
loading reservations and admissible free MiB), `cpu` (logical processors and CPU
model slot concurrency), and per-model `rss_mib`/`rss_peak_mib`. Signed machine
inventory preserves these fields. Unknown readings remain null; unavailable RAM
never becomes guessed capacity. Known inventory aliases whose worker is down
return **503** with **Retry-After**, while caller-forbidden aliases are excluded
from that unavailable list.

Focused isolated tests cover measured admission, ancestor service ceilings,
concurrent load reservations, measurement persistence/invalidation, protected
eviction, real child RSS, and cached-worker outage/refusal ownership.

## Matching delivery and live checks (2026-10-16)

Compute, `front_cpu` and `remote_cpu` received the reviewed CPU worker modules,
defaults and the two-field runtime settings-report correction. The remote keeps
its existing DB1 application baseline with that reporting correction applied in
place. Restarts followed an empty front task queue and zero worker requests,
holds and locks. Private settings hashes and unit ceilings were preserved.

All three signed front routes passed a headroom **512 → 513 → 512 MiB** write,
runtime read and original-override restore; benign Spotter writes were restored.
Sleep policies remained **60 minutes / disabled / disabled**. Both CPU workers
returned 1,024 finite embedding dimensions through front. After stopping only
`loadcoach-remote-cpu.service`, its known alias returned **503**, `Retry-After: 5`,
in **0.155 s**; its host settings also returned retryable 503. The front CPU
worker continued returning 200, and the remote recovered to 200 within **7.53 s**
after restart. The temporary qualification key was revoked.

Observed live service RAM was about **1,088 MiB** on front and **334 MiB** remotely;
resident-process RSS was **1,584 MiB / 912 MiB**, illustrating shared mmap page
accounting. Signed inventory reported RAM, CPU slot concurrency and per-model RSS
on both hosts. Full receipts live in front's private
`~/weightroom/g11-worker-backup-20261016/live-qualification.jsonl`; scoped source
backups and settings hashes are beside them.

The isolated native trial ran **01:49:01–03:49:10 UTC, 2026-10-17** in
`suite-g11-cpu-soak.service` (invocation `06528b6d6bda46ed8af696f0e423ddd3`).
All **803** requests returned 200 with finite 1,024-dimensional vectors across
**7,208 seconds**; failures and restarts were zero. Systemd exited successfully,
with a **646 MiB** cgroup peak below its **1,500M high / 2,200M maximum** limits;
resident RSS peaked at **1,020 MiB**. The unit journal showed no OOM event.
Its final `error: SystemExit(False)` row is the harness catching its own successful
exit; the summary has no failures and systemd exit status is zero. Private
`~/suite-cpu/g11-isolated-20261016/samples.jsonl` and `summary.json` retain the
machine-readable receipts. The trial overlay, token and DB were removed after exit;
the installed workers, unit ceilings, SQLite bindings and sleep policy were preserved.

## Native CPU resource benchmarks (G18)

An eligible `gpus = 0`, `embedding = true` model can run generic Load and
embedding Context, Load and Speed through the existing signed task token, probe
lock and native child lifecycle. CPU text models also support Load, Context, Speed
and native generation/quality probes through that path, with the same RAM admission,
measurement and interruption checks. Embedding requests retain complete native input counts, a finite
pooled vector and its receipt; load-only probes do not claim a vector. Each CPU
sample reports `device = cpu`, final process-and-children `rss_mib`, sampled
`peak_ram_mib`, empty `cards` and null `peak_gpu_mib`. The RAM values also appear
in `evidence.resource`.

The probe checks estimated room before starting, then checks observed RSS after
readiness and during the sample. The same post-readiness check now covers ordinary
CPU loads. It compares the combined observed CPU residents with any configured
`host_ram_limit_mib` and checks live host headroom and cgroup service room. A
measured breach of the configured RAM budget stops its child and records a
failed capacity sample with RSS, peak RAM and native log. When an isolated probe's
native child exhausts this worker unit's cgroup RAM ceiling, it records a separate
`measured_service_ram` capacity sample with process RSS and the unit's observed
used/limit MiB; those are distinct measurements. A conservative admission
bound above a fixed configured process or host ceiling records a separate failed
sample with `native_attempted = false` and null current RSS. Context searches
smaller sizes; Load retains failed attempts and reports the largest passing
configured size. Preflight pressure and host or ancestor-cgroup pressure remain
retryable 503, as do ordinary CPU loads.
Each native embedding request allows 1,700 seconds on CPU through 8,192 input
tokens and 3,300 seconds above that, or 600 seconds on GPU. Speed's two smaller
CPU calls still fit inside the 3,900-second benchmark request budget; a single
large Context call has more room. The input, vector and timing checks are unchanged.
Existing systemd CPU, memory and swap ceilings remain in force. All three
workers and WeightRoom Dry Fit received the reviewed RSS-versus-cgroup-charge
correction on 2026-10-17 at 21:08 UTC; installed hashes and authenticated idle
status passed. The 78 focused CPU and Dry Fit checks passed locally.
Native qualification completed on 2026-10-17 with `qwen3-embedding-0.6b-q8`.
All twelve per-node runs and 88 retained resource samples pass the independent
identity, protocol, native-receipt and search/series audit. Six Context failures
are measured service-capacity bounds; successful vectors have 1,024 dimensions.

| Node | Proven context / native input tokens | Context probes | Generic Load / Embed Load / Speed samples |
|---|---:|---:|---:|
| `compute` | 32,768 / 32,736 | 1 | 9 / 9 / 12 |
| `front_cpu` | 29,696 / 29,664 | 6 | 9 / 9 / 12 |
| `remote_cpu` | 6,144 / 6,112 | 6 | 3 / 3 / 9 |

The CPU Context searches stop at 1,024-token resolution. Model settings and
service ceilings are unchanged. Historical runs superseded by the RSS/charge
correction remain diagnostics; no saved sample was rewritten.
[Native audit](../data/qualification/gates-2026-10-16/g18-retained-audit.json).

The live **Queue outstanding benchmarks** check then created zero runs and
recognized all twelve as current, with zero new active jobs; its temporary key
was revoked. Eight focused local identity/queue checks passed, including stale
model/protocol/server/engine/machine and per-location evidence; these fixture
checks are separate from the live fresh-result check.
[Deployment and queue receipts](../data/qualification/gates-2026-10-16/g18-runtime-deployment.json).
G18 and milestones 130/135 are complete for the eligible three-node workload.

During qualification, front-owned queued benchmarks did not appear in the compute
Spotter's direct queue counts. Preserve its configured sleep policy: interleave
needed GPU runs with CPU work by pausing individual CPU runs at durable sample
boundaries, then resume them. A front queue alone is not evidence that compute
will stay awake; do not add artificial holds or wake the host for this loop.
