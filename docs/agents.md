# Agent reference

Read this first for repository work, then the [human guide](README.md),
[status/backlog](agent-status.md) and the relevant reference below. This consolidates the former
root agent instructions, documentation contract and durable design decisions.

## Working rules

- Follow the owner's current instructions; keep replies under 100 words unless detail is needed.
  State decisions in chat. Use `rtk` for shell commands, as required by `/home/jpk/.codex/RTK.md`.
- Start with `git status`; preserve unrelated work. Keep patches small and code readable enough
  for the owner to explain. Prefer existing components over abstractions, scaffolding or dependencies.
- Finish authorized work without intermediate review gates. Proposals are not authorization.
  Never push Git; keep any local commit limited to the intended files.
- Distinguish implemented, locally tested, deployed, live-verified and owner-accepted claims.
  Date deployment/acceptance records; a passing fixture does not establish live behavior.
- Use the existing routes, schedulers, stores and service lifecycle. Do not add a second model
  scheduler, inference path, identity authority, private-prompt store or credentials without scope.

## Standing permissions and execution constraints

Owner decisions D1–D7, recorded 2026-10-15/16, apply to the approved disposable installation:

| ID | Authority and boundary |
|---|---|
| D1 | Advance approved gates, including DB1 retirement, without intermediate review or minimum backup retention. Preserve data and resolve differences through evidence; technical conflicts block dependent work. |
| D2 | Diagnose capped extraction/retrieval with up to 32,768 outputs where context fits. Headline check accuracy plus whole-case accuracy; report caps separately. Version harder revisions and preserve old evidence. |
| D3 | Live bindings remain SQLite. PostgreSQL 18 qualification is isolated; live engine cutover needs separate authorization. |
| D4 | Direct voice retains its least-privilege identity, private server-side key, private HTTPS and revocation contract. Never expose the key to chat/browser. |
| D5 | Physical/subjective acceptance belongs to the owner. Completed acceptance must not reappear as a pending owner session. |
| D6 | Preserve measured CPU resident-RAM admission, host/service headroom, idle-only eviction of unheld/unlocked models and each host's sleep policy. |
| D7 | Complete approved gates autonomously; follow current agent instructions on delegation. Use a small representative model set, expanding only when needed. Correct broken scoring through versioned contracts, retaining old evidence; no mandatory four-model rerun. |

- Deployment/restarts are permitted for approved work. Before affected restarts or new model
  loads, verify no benchmark is queued/running; freeze submitted implementations and identities
  until drained. GPUs may run unattended. Use a reviewed immutable release with its dependencies.
- Inspect deployer scope and every installed copy: front, workers, standalone adapters/tool hosts
  and shared-checkout services can need separate updates. Report exactly what was activated.
- Do not wake/suspend/reboot/power off hosts for qualification, change live DB engines, execute
  generated code or spend on new services without separate authorization. Controlled service
  outages remain permitted after affected work drains.
- The same-host two-LoadCoach rig may borrow one GPU briefly; it does not qualify another physical
  GPU host. Reference spread: SmolLM2 135M, Qwen2.5 1.5B, Qwen3.5 9B, GPT-OSS 20B Q4; D7 allows fewer.
- Retain Qwen3.8 Q6's projector removal/minimum context 131,072, Gemma 31B context 32,768 and
  disabled estimated-wait admission. Preserve the [benchmark defaults](benchmarks/harder-versions-plan-2026-10-16.md#owner-decisions-approved-2026-10-16).
- Before destructive file cleanup, obtain usage from every configured node, protect the global
  usage union and refuse cleanup if any usage report is unavailable. Verify storage copies first.

## Design constraints

- **Ownership:** front owns identity, roles/reliability, hosted calls and retained history;
  LoadCoach owns final worker admission and model lifecycle. Applications use the APIs.
- **Authentication:** Turnstile validates current keys and sends request-bound, expiring signed
  statements; workers reject unsigned/replayed calls and prove their responses. Clock agreement
  matters. Machine-key rotation has ten-minute overlap; recovery/emergency procedures stay in
  [the implementation guide](loadcoach.md#a-new-machine-key-and-an-emergency-milestone-77).
- **Permissions:** fallback, tools, plugins, durable work and hosted use retain caller identity,
  model access and sticky sensitivity. Administrators cannot grant beyond their authority.
  Renames preserve stable account/model IDs; recreating a name must not inherit old work.
- **Scheduling:** measured GPU/CPU fit and configured headroom govern admission. Foreground work
  preempts benchmark probes. Active work, loading, holds and locks remain protected; default idle
  replacement delay is zero. Persistent model locks do not themselves keep a host awake.
- **Context:** prompt/history/tools/reasoning/output share the served per-request capacity.
  Trained context is not proof of allocated context. Keep service `MemoryHigh`/`MemoryMax` ceilings
  and `GGML_CUDA_DISABLE_GRAPHS=1` for adapter servers until independently requalified.
- **Model identity:** preserve stable IDs, selected-file revisions, fingerprints and engine/machine
  evidence. Display-name/order edits do not change inference identity. Historical results remain
  available; missing, stale, incompatible and zero measurements are distinct.
- **Adapters/projectors:** users choose configured model entries, not arbitrary per-request adapter
  overrides. Compatible kin share a server; preserve each entry's capabilities and answer identity.
- **Routing:** explicit model names remain supported; role choices explain quality/reliability,
  capabilities, residency and time estimates. Reassign a lost request only before output starts;
  fail cleanly afterward without replaying a partial answer.
- **Files:** install verified complete copies atomically; promotion retains storage originals.
  Delete only permitted unused files. Keep legacy explicit places and settings/service/command
  aliases where supported; relay→turnstile has no alias. Historical labels remain unchanged.
- **Audit/privacy:** preserve append-only chain serialization, forwarding deduplication and scoped
  capture access. A sensitive request that cannot be recorded fails. No-log work leaves no retained
  evidence; its in-memory Stop handle is neither a request record nor a text-inspection route.
- **Benchmarks:** freeze plans/protocols, retain raw answers/checks and completed samples, and
  separate resource measurements from quality. Graders are deterministic/data-only: no real tools,
  generated code, answer keys sent to models or model judges. Label subsets and unsupported cases.
  Only supported arithmetic evaluates scoring formulas; never execute formula code.
- **Durable work:** use stable IDs, current owner/model permissions and scoped revocable grants.
  Schedules do not wake hosts; missing readings do not resolve alerts. Preserve confirmed playback
  history and separate Direct/Hermes sessions. Broader tools/voice approval flows need explicit scope.
- **UI:** server-rendered Jinja/HTMX with small JS modules, vendored Tabler 1.4 CSS/ECharts; no Node
  build requirement. Responsive controls, escaped content and one detail view per object. Refreshes
  preserve selected text, focus and independent card state. Keep detail-page actions above metadata.
  Use the shared `card`, `page_header`, `section_title` and `form_section` macros in
  [parts.html](../weightroom/pages/parts.html), including inert templates for client-created cards.
  User-facing text uses plain words: say what the person can do, give a next step in empty states,
  and avoid metaphors or service names outside administrator diagnostics and source Docs.
  Use shared confirmation pieces for destructive actions, `timestamp` for recorded instants
  and `count` for whole totals. Preserve browser-local display and UTC recovery values.
  Shared size/spacing tokens live at the top of [style.css](../weightroom/static/style.css):
  forms use 40px controls, toolbars/filters 32px. Run [UI consistency checks](../tests/test_ui_consistency.py)
  when changing components or page composition; they require CPU-only Chromium/Playwright.

## Documentation maintenance

Record each fact once: human operation in [README](README.md) or its scoped guide, implementation
contracts in the owning subsystem, and approvals/status/proposals in [agent-status](agent-status.md).
Keep completed execution narratives out of the backlog; retain unique recovery steps and links to
machine-readable evidence. Older implementation-guide sections are chronological history: use later
ownership/protocol sections and current code when they conflict.

Update existing files in place. Add a plan/handoff/evidence document only when requested or required
by an explicit acceptance/recovery contract. Preserve the owner's walkthrough findings. Keep root
Markdown limited to `app-walkthrough.md` and the owner-provided `makeBetter.md`; its relative links start from the repository root.

| Reference | Read/update for |
|---|---|
| [Implementation](loadcoach.md) | Runtime/API behavior and existing recovery contracts. |
| [Installation](installation.md) | Host roles, dependencies, service units, settings, paths/ports, credentials and compatibility. |
| [Database storage](database-storage.md) | Store ownership, schemas, exports, backup/restore, health and engine migration. |
| [Benchmarks](../benchmarks/README.md) | openWeightGym protocols, scoring, retained evidence and worker operation. |
| [CPU workers](cpu-embedding-hosts.md) | RAM admission, native embedding probes and multi-node limits. |
| [Collections](collections.md), [workflows](workflows.md), [allowances](allowances.md) | Private data, inference, durable state and usage policy. |
| [Voice specification](voice.md), [voice brief](../voice/README.md), [desktop instructions](../desktop/README.txt) | Speech/client protocols, capacity and device acceptance. |
| [Plugin guide](../plugins/README.md), [specification](plugins/SPEC.md), [Hermes](../plugins/hermes/README.md) | Adapter runtime, permissions, authoring and delivery. |
| [Tool authoring](../toollocker/README.md) | Tool folders and remote hosts; shipped with each host. |
| [Application sweep](application-sweep-2026-10-20.md), [observability](observability-work-plan.md) | Retained delivery/acceptance evidence and observation limits. |
| [Agent walkthrough](agent-walkthrough.md) | Page-by-page automated testing methods, live boundaries and links to the owner's findings. |
| [Arithmetic audit](benchmarks/arithmetic-audit.md), [case contract](benchmarks/arithmetic-v3.md), [qualification](benchmarks/qualification-2026-10-15.md) | Versioned benchmark evidence; status remains in the ledger. |

Installation/deployment interfaces must change with their contracts:

- [openWeightGym installer](../scripts/install-suite.sh), [support](../scripts/install-suite-support.py), [installer tests](../tests/test_installer.py).
- Optional image runtime: [installer](../scripts/install-sdcpp.sh), [implementation](../scripts/install-sdcpp.py); operating instructions in [installation](installation.md#optional-image-generation-runtime).
- Mirror Wall: [native installer implementation](../mirrorwall/install.py); image engine, workflows, storage and UI live in the root `mirrorwall/` package.
- SoundRoom: [optional audio runtime installer](../scripts/install-audiocpp.sh), [implementation](../soundroom/install.py); audio engine, packages, storage and UI live in the root `soundroom/` package ([plan](soundroom-plan.md)).
- [Remote tool installer](../scripts/install-tool-host.sh), [tests](../tests/test_tool_host_installer.py).
- Model file placement: [implementation](../turnstile/model_install.py), [API/file checks](../tests/test_model_install.py), [browser checks](../tests/test_model_install_browser.py); contract in [Models](loadcoach.md#the-models-pages).
- [Front deployer](../scripts/deploy-front.sh), [full deployer](../scripts/deploy.sh), [shim retirement](../scripts/prune-frontdesk.py).
- [Plugin bundler](../plugins/_installer.py), [shipped installer README](../plugins/INSTALLER-README.md).
- [Desktop packager](../scripts/package-voice-desktop.py), [macOS/Linux launcher](../desktop/Voice.command), [Windows launcher](../desktop/Voice.cmd), [requirements](../desktop/requirements.txt).
- [Voice assets](../scripts/voice-models.py), `scripts/voice-*-check.py`, [engine updater](../scripts/update-llamacpp.sh), [observation qualification](../scripts/qualify-observability.py).
- [Service definitions](../services/), [power policy](../services/spotter.rules), [defaults](../settings/defaults/), [examples](../settings/examples/).

Exclude vendored IFEval, generated build/dist/cache/voice-test copies and personal notes from
canonical documentation maintenance. For docs-only work run
`rtk proxy .venv/bin/pytest tests/test_documentation.py`; run scoped tests for changed commands/configuration, then check
links and `rtk git diff --check`. Use `.venv/bin/pytest` for broader verification when warranted.
