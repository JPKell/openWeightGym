# Telephone voice and Voice status plan

Drafted **2026-11-14**; decisions settled with the owner on 2026-11-14 (see
[Decisions](#decisions)). Status: in progress; P1–P8 complete.

## Goal

Make Twilio telephone calls a supported part of the suite: an owner with a Twilio account can
turn phone calls on, connect one or more numbers, decide who may call, test it and watch it
working **from the web panel**, without editing TOML, creating key files or accounts by hand,
or knowing the internals. Users may register their own phone numbers and call in as themselves.
At the same time, rebuild **System → Voice status** so it answers "can a call start right now,
and if not, why?" for every way into a call, and finish with a professional design across the
whole Voice area.

## Where things stand

What exists and stays:

- The Twilio adapter in [voice/gateway.py](../voice/gateway.py) and
  [voice/telephone.py](../voice/telephone.py): signed webhook and stream checks, `AccountSid`
  check, 8 kHz μ-law conversion, control-node-side echo cancellation, `media`/`mark`/`clear`
  bookkeeping, a public listener (`public_port`, default 8081) that serves only the Twilio paths.
- [scripts/voice-phone-check.py](../scripts/voice-phone-check.py), which simulates a signed call
  end to end without dialling.
- Settings changed on the site are kept in LoadCoach and laid over each program's file
  ([loadcoach/settings.py](../loadcoach/settings.py)); the gateway already takes `NOW` settings
  while running.
- Cloud API credentials are written to owner-only key files, atomically, and never shown back
  (`set_key` in [loadcoach/outside.py](../loadcoach/outside.py)). Telephone credentials follow
  the same pattern.
- The `voice-gateway` identity already holds `app-sign-in`, which lets it obtain limited app
  sessions for accounts (`limited()` in [loadcoach/groups.py](../loadcoach/groups.py)).

What is missing:

| Gap | Where |
|---|---|
| `[twilio]` and `public_port` are machine-file settings, commented out in the example; one number only | [settings/examples/voice-gateway.toml](../settings/examples/voice-gateway.toml) |
| No web form for any telephone setting; nothing in `CHANGEABLE["voice-gateway"]` | [loadcoach/settings.py](../loadcoach/settings.py), [voice_settings.py](../weightroom/voice_settings.py) |
| The installer creates `voice-gateway` and `voice-browser` identities, but no phone account; `[twilio] key_file` must be made by hand | [install-suite-support.py:700-705](../scripts/install-suite-support.py#L700-L705) |
| Anyone who dials the number gets a call on the phone account: there is no caller allowlist or PIN | `inbound` in [gateway.py](../voice/gateway.py) |
| Every phone call runs as one shared account; a user cannot call in as themselves | `telephone` in [gateway.py](../voice/gateway.py) |
| No supported public route: the README hard-codes the owner's VPS and domain | [voice/README.md](../voice/README.md) |
| Twilio's number must be pointed at the webhook in Twilio's console by hand | — |
| The status page lists two rows that are always "Available", has no telephone row, shows active calls only as a count, has no targets on latency, and refreshes only by hand | [voice_status.html](../weightroom/pages/voice_status.html), `admin` in [gateway.py](../voice/gateway.py) |
| Phone rejections are only in the journal (`voice.phone.rejected`) | [gateway.py](../voice/gateway.py) |
| The Voice settings page is a plain list of setting rows | [voice_admin.html](../weightroom/pages/voice_admin.html) |

## Ground rules

- **One gateway, one adapter.** The existing Twilio adapter, signature checks and public-port
  allowlist stay the only telephone path. No second listener, no proxy-trusted headers.
- **Secrets are write-only.** Twilio auth tokens, API key secrets and PIN hashes are entered once,
  stored as owner-only files under `keys/` (or hashed in the accounts store for PINs and SMS
  codes), read when needed (so a new token needs no restart), and never rendered, logged,
  exported or returned by any API. The page shows only "Saved · last changed …".
- **Every call runs as an account.** A call runs either as the matched user's own account or as
  the number's phone account (`voice-phone` or `voice-phone-<label>`), so models, limits, tools
  and usage stay under the normal Users/Groups rules. The panel creates phone accounts; there is no
  separate permission path.
- **Nothing on the public port changes.** The public listener still serves only
  `/voice/inbound`, `/twilio/voice`, `/voice/stream`, `/twilio/media`. No admin, status or
  registration route is ever reachable from it.
- **Refuse before cost.** Every caller-access decision happens in the webhook, before compute is
  woken, a model is held or a session is reserved.
- **Status reads never wake compute or load models**, as today.
- **Privacy:** no utterance text or audio in status, events or exports. Phone numbers are personal
  data: stored only where needed (allowlist entries, users' registered numbers), shown masked
  (`+1 ••• ••• 0123`) except to their owner and `manage-settings`, never in events or logs (a keyed
  hash is used for correlation), and removed when the entry or account is removed.
- **Permissions:** Voice settings, Telephone and Voice status require `manage-settings`, re-checked
  by the gateway's private admin route. Registering one's own number requires the new
  `front-phone` ability (below).
- **Deployment** only on the owner's request, through `owg update; owg restart`, then verified live
  (effective service source and a real or simulated call).
- **UI rules from [AGENTS.md](../AGENTS.md#web-ui):** reuse `parts.html`, `kpi_parts.html`,
  `setting_row.html` and the polling-fragment pattern (`data-hold-refresh` for unsent drafts);
  add new shared parts to the template index.

## Decisions

Settled with the owner on 2026-11-14.

| # | Question | Decision |
|---|---|---|
| D1 | Public route for Twilio | Support and document all three: **own reverse proxy**, **Tailscale Funnel**, **Cloudflare Tunnel** (P7). |
| D2 | Twilio API credential | **Accept either.** The auth token is always required (signatures); an optional API key SID/secret is used for API calls when given, and the form recommends it. |
| D3 | Outbound "Call me" test | **Simulation only** in this plan. |
| D4 | Caller access | **Switchable per number** between *Anyone*, *Allowlist* and *PIN*. Allowlist entries are added by an admin (who picks the account the calls run as, defaulting to the number's phone account) or by a user for their own number. New numbers start in **Allowlist**; the owner's existing deployment stays *Anyone* until changed. |
| D4a | A user's own number | Calls from it run as **the user's own account**. |
| D4b | Proving ownership | **SMS code** sent through Twilio to the number being registered. |
| D5 | Numbers | **Several numbers now**, each with its own mode, profile, voice, greeting, caller access and phone account. |
| D6 | Design scope and flow | **One design for all three Voice pages** (settings, Telephone, status) plus the My account phone-number section, **applied straight to live** (P9). No standalone proposal round. |

## Milestones

| # | Milestone | Size | Depends on |
|---|---|---|---|
| P1 | Complete — site settings and signed multi-number routing; 57 focused/settings/UI checks pass, including Chromium desktop/narrow | L | — |
| P2 | Complete — write-only credentials, phone accounts/rotation and front-phone; 98 unit/browser/UI checks pass | M | P1 |
| P3 | Complete — setup/number editors, Twilio discovery/connect and shared signed simulation; 120 checks pass, including real sockets and full UI audit (activity follows P6) | L | P1, P2 |
| P4 | Complete — caller policy, hashed PINs, attestation, limits and private rejection evidence; 87 focused unit/browser checks pass | M | P1 |
| P5 | Complete — SMS ownership, private numbers, limited personal sessions and revocation; 132 clean-snapshot unit/browser/UI checks pass, including signed real-socket model attribution | L | P2, P4 |
| P6 | Complete — bound listeners, private call controls, scoped readiness reasons and bounded masked activity; 135 clean-snapshot unit/browser checks pass | M | — |
| P7 | Complete — polling readiness, active call controls, profile reasons, latency targets and activity; selection/disclosure/scroll and narrow browser checks pass | L | P6 |
| P8 | Complete — signed public probes, three route guides, lossless legacy import and installer/docs; 143 clean-snapshot unit/browser/UI checks pass | M | P3 |
| P9 | Professional design for the Voice area | L | P3, P5, P7 |

P6–P7 can run in parallel with P1–P5. P9 comes last because it restyles what P3, P5 and P7 build;
P3/P5/P7 should still use shared parts so P9 changes templates, not logic.

### P1 — Telephone settings become site settings, with several numbers

- Add to `CHANGEABLE["voice-gateway"]`:
  - `telephone` (table, `NOW`):
    - `enabled`, `account_sid`, `public_url` (one base URL; webhook and stream URLs are derived
      from it as `…/voice/inbound` and `wss://…/voice/stream`).
    - `numbers`: a list, one entry per Twilio number: `label`, `number` (E.164), `enabled`, `mode`,
      `profile`, `voice`, `greeting`, `account` (phone account name), `callers` (P4),
      `max_calls_per_hour`.
  - `public_port` and `public_host` (`RESTART`). The page says "applies after the voice gateway
    restarts" through the existing pending-restart display.
- **Routing several numbers:** all numbers share one webhook and stream URL. The gateway picks the
  entry by the signed `To` parameter of the webhook; an unknown or disabled `To` answers with a
  short "not in service" TwiML and is counted as a rejection. The chosen entry is stored on the
  call item, so the stream uses that number's account, mode, profile and voice.
- **Legacy table:** if `[twilio]` is present and `telephone` is not, the gateway uses it as a
  single number that accepts any `To`, and the page shows "Set in the machine file" with an
  **Import to site settings** action (P8). Explicit `webhook_url`/`stream_url` in the old table
  still win, so the owner's current deployment keeps its exact signed URLs.
- `enabled = false` (global) makes `inbound` answer 404 exactly as an unconfigured gateway does.
- Validation (server side, in `voice_settings`): `account_sid` matches `AC` + 32 hex; `public_url`
  is `https://`, has no query or fragment, is not a private/loopback address; numbers are valid
  E.164 and unique; labels unique; `mode` within the profile's `modes`; `voice` one of the
  number's account's SoundRoom voices or empty.
- Tests: settings round trip, legacy precedence, `To` routing including unknown/disabled numbers,
  disabled answers 404, every validation error, signature checks still use the exact URLs.

### P2 — Phone accounts and credentials managed from the panel

- **Twilio credentials:** a `TelephoneKeys` helper following `loadcoach/outside.py`'s pattern
  writes `keys/voice-twilio.key` (auth token) and optionally `keys/voice-twilio-api.key`
  (API key SID and secret), 0600 and atomic. Password inputs are never prefilled; an empty
  submission keeps the saved value. **Remove** deletes it; removing the auth token disables
  telephone.
- **Phone accounts:** each number names a phone account. **Create phone account** makes a
  `voice-phone` (or `voice-phone-<label>`) app identity with `front-voice` through the same helper
  the installer uses for `voice-browser`, writes its key file and links to `/users/<name>` for
  models and limits. Several numbers may share one account. **Rotate key** replaces the key file
  in place.
- **New ability `front-phone`** ("call in by telephone as yourself"), listed with the other control node
  abilities in [app.py](../weightroom/app.py) and grantable per group/user. It gates P5.
- Tests: file mode and atomicity, secrets never in HTML/JSON/logs (assert by searching rendered
  output and captured logs), account created once, rotation without restart, ability shown on
  Users/Groups.

### P3 — Telephone page: setup, connect numbers, test

A new **System → Voice → Telephone** view (sibling of Voice settings and Voice status).

1. **Status strip** (shared with P7): Off / Needs setup / Ready / Receiving calls, with the first
   unmet step named.
2. **Setup checklist**, each card showing done/not done and its own form:
   - Twilio account: Account SID, auth token, optional API key (write-only); **Check
     credentials** calls Twilio's account endpoint once and shows the friendly name or the exact
     error.
   - Public address: `public_url`, public port; **Check from outside** (P8).
3. **Numbers:** **Find my numbers** lists the Twilio account's numbers (via the
   IncomingPhoneNumbers API) with their current Voice URL; **Add** creates a `numbers` entry and
   **Connect** sets that number's Voice URL to the derived webhook (`POST`). Each number has its
   own editor: label, enabled, account (create/link, P2), mode, profile, voice, greeting, caller
   access (P4), rate limit. A **Manual** disclosure shows the exact URL and method to paste into
   Twilio's console. A number whose Voice URL points elsewhere is flagged "Not connected here".
   Drafts survive switching between numbers, as on the Cloud APIs page.
4. **Test:** **Simulate a call** to a chosen number runs the logic of `voice-phone-check.py` from
   control node against the public URL (signed webhook with that `To` → TwiML → signed stream → spoken
   reply → marks) and reports each stage with timings. Its core moves into
   `voice/telephone_check.py`, shared by the script and the page. No outbound calls (D3).
5. **Recent phone activity:** last 20 inbound attempts per number from the ring (P6) — time,
   masked caller, account used, accepted/rejected and reason, duration.

The Twilio API client lives in `voice/twilio_api.py`: `httpx`, bounded timeouts, no SDK
dependency, only the endpoints used (account, incoming numbers list/update, messages for P5),
every error mapped to a plain message; uses the API key when saved, else the auth token (D2).

Tests: page renders for each checklist state; drafts survive polling (`data-hold-refresh`) and
number switches; Twilio API mocked, including 401, 404, rate limit and timeout; Connect writes
exactly the derived URL; simulated call passes against a test gateway for two numbers.

### P4 — Caller access: Anyone / Allowlist / PIN, per number

Each number's `callers` table: `mode` = `anyone` | `allowlist` | `pin`, plus `entries` and the
PIN hash. New numbers default to `allowlist`; an imported legacy number keeps `anyone` (D4).

- **Anyone** — today's behaviour, shown with a warning about cost and abuse.
- **Allowlist** — the caller's `From` must match an entry:
  - *Admin entries* on the number's editor: number, note, and **account the call runs as**
    (default: the number's phone account; any user may be chosen).
  - *User entries* from P5, shown read-only with the owning user and a Revoke action.
  - Others hear the number's refusal greeting and are hung up.
- **PIN** — any caller is asked for a keypad PIN (TwiML `<Gather>` before `<Connect>`); PIN stored
  hashed; three wrong attempts end the call and count toward the rate limit. A caller whose number
  matches an allowlist or user entry skips the PIN and runs as that entry's account.
- **Caller ID trust:** `From` can be spoofed. Each number gets `require_attested_caller_id`
  (default on for entries that run as a user's own account): accept only when Twilio's
  `StirVerstat` reports full attestation (`TN-Validation-Passed-A`); otherwise fall back to the
  PIN, or refuse when no PIN is set.
- **Rate limits** per caller and per number (`max_calls_per_hour`), enforced in the webhook.
- Every refusal emits `voice.phone.rejected` with a reason code (`not_listed`, `bad_pin`,
  `unattested`, `rate_limited`, `unknown_number`, `invalid_signature`) and a keyed hash of the
  caller, and is counted in the ring (P6).

Tests: each mode, entry precedence, admin-chosen account, PIN hashing and attempts, attestation
fallbacks, rate limits, compute never contacted for refused callers.

### P5 — Users register their own numbers and call as themselves

- **My account → Phone numbers** section (shared parts from `user_parts.html`), visible with
  `front-phone`: add a number, receive an SMS code, enter it, done. Up to 3 numbers per user;
  remove at any time.
- **Verification:** control node sends a 6-digit code through Twilio Messages from an SMS-capable
  connected number (the page names which; if none is SMS-capable, registration is unavailable
  and says so). Codes are hashed, expire after 10 minutes, allow 5 attempts, and sending is
  rate-limited per user and per number. A number already registered to another user is refused
  without saying whose.
- **Which numbers accept user calls:** each Twilio number has `accept_user_numbers` (on by
  default for allowlist/PIN numbers). A registered user calling such a number is admitted as an
  allowlist match.
- **Running as the user:** the gateway asks Turnstile, through the existing `app-sign-in` path,
  for a short-lived limited session for that user, scoped to `front-voice`/`front-phone` and the
  user's own models and limits. The call's usage, logs and (with a SoundRoom voice) conversation
  belong to the user. Losing `front-phone` or the account being disabled refuses future calls.
- Admins see users' numbers (masked) on the number's allowlist and on `/users/<name>`, and can
  revoke them.
- Tests: SMS flow with mocked Twilio (send, expiry, attempts, rate limits, duplicates), session
  scoping, usage attributed to the user, revocation, ability removal, privacy of numbers in
  rendered pages and logs.

### P6 — Gateway status payload v2

Extend `GET /admin/status` (private listener only), keeping existing keys for compatibility:

- `version`: gateway commit and start time (uptime), for post-deploy verification.
- `entries`: one object per way in — `browser`, `desktop`, `telephone`, `hermes` — each with
  `configured`, `listening` (socket actually bound), `can_start` and `reason`, `last_success_at`,
  `last_rejection` `{at, reason}`.
- `telephone`: `enabled`, derived URLs, public port bound, credentials saved yes/no (never the
  values), and per number: label, masked number, caller-access mode, accepted/rejected counts for
  the last hour and 24 h, and a bounded ring of the last 50 attempts (masked caller, account,
  outcome, reason).
- `calls`: active calls — source, number label, account, mode, profile, model, started, turns,
  last state — with an opaque id; no text and no full numbers.
- `readiness.profiles` gains `reason` for each `No` (missing model, compute asleep, synthesis not
  installed …), from the compute voice service's `/voice/ready`.
- Drop the hard-coded "Available" rows: the gateway reports its own facts, and the control node reports
  Turnstile reachability itself.
- Add `POST /admin/calls/{id}/end` (manage-settings) that ends a call the same way hang-up does.

Tests: payload shape, no secrets or full numbers in it, public port returns 404 for every admin
path (existing check extended), end-call cleanup.

### P7 — Voice status page rebuild

Rebuild [voice_status.html](../weightroom/pages/voice_status.html) as a polling fragment
(`voice_status_content.html`, refreshed every 5 s, holding while text is selected):

1. **Verdict band** (`kpi_row`): Calls can start — Browser / Desktop / Phone / Hermes, each ✓ or the
   reason with a link to its fix (Voice settings, Telephone, Users, Components).
2. **Active calls** table with End call (shared two-click confirmation) and duration.
3. **Ways in** table from `entries` (replaces Services), with one row per telephone number.
4. **Profiles** readiness with reasons.
5. **Latency** compared with the documented targets in [voice.md](voice.md#counters-and-targets):
   p50/p95 cells marked within/over target, one unit (ms) throughout, caveats folded into a
   disclosure.
6. **Telephone activity** summary per number, linking to the Telephone page.
7. **Facts:** gateway version and uptime, greeting recordings source/revision, links to Event log
   and System logs pre-filtered to voice events.

Tests: [test_ui_consistency.py](../tests/test_ui_consistency.py) audit, feature tests for each
state (gateway down, compute asleep, phone off, phone ready, active calls, no timings), polling
keeps scroll and open disclosures, narrow layout.

### P8 — Public routes, installer, upgrade and documentation

**Public routes (D1).** Twilio must reach `public_port` over public HTTPS/WSS. The Telephone page
offers a choice with tailored instructions:

- **Own reverse proxy** (Caddy/nginx on a VPS or the control node): generic example configs with WebSocket
  upgrade and long-lived timeouts, domain as a placeholder.
- **Tailscale Funnel:** detects `tailscale`, shows the one command that funnels the public port and
  the resulting `https://<node>.<tailnet>.ts.net` URL to use as `public_url`.
- **Cloudflare Tunnel:** detects `cloudflared`, shows the tunnel config for the public port.

The suite shows commands but does not run them with elevated rights. **Check from outside** sends a
signed probe to the configured `public_url` (a new `HEAD /voice/inbound` that answers 204 only for
a valid probe signature, served on the public port), so the owner learns whether the route
forwards before a real call.

**Installer and upgrade.**
- `install.sh` on a control node with voice: stop telling owners to edit `[twilio]`; print "Phone calls:
  set up under System → Voice → Telephone".
- `owg update` keeps any existing `[twilio]` table working untouched (P1); the panel offers Import,
  which copies non-secret values into site settings as one number with `callers.mode = anyone` and
  points the token file at the existing file without copying it.
- Uninstall removes the telephone key files only with the rest of `keys/`, as today.

**Documentation.** Replace the owner-specific domain in [voice/README.md](../voice/README.md) with
placeholders (the owner's route stays only in their machine settings). Write `docs/telephone.md`:
costs (numbers, minutes, SMS codes), setup in the panel, each public route, caller access and
caller-ID trust, users' own numbers, troubleshooting by verdict reason.

### P9 — Professional design for the Voice area

One coherent design for **Voice settings** (`/system/voice`), **Telephone** and **Voice status**,
plus the **My account → Phone numbers** section, applied directly to the live pages (D6).

- **Shared Voice shell:** one page heading with Settings / Telephone / Status pills
  (`navigation_parts.html`), the P3/P7 status strip on every Voice page, and a consistent
  two-column layout on wide screens: a navigator on the left (profiles on Settings, numbers on
  Telephone, ways in on Status) and the selected item's editor or detail on the right, collapsing
  to one column on narrow screens. Drafts stay in page memory across selections, as in the Cloud
  APIs and Users directories.
- **Voice settings, regrouped** from the flat list of rows into task cards: *Conversation* (direct
  model, system prompt, token limits, modes), *Voice and greetings* (speaker with a preview
  button, greeting texts with regenerate), *Hermes* (model, prompt, steps, reasoning), *Capacity*
  (active/starting sessions, startup time). Each card saves on its own and shows in-force /
  pending-restart / changed-from-default state from `setting_row.html`.
- **Telephone:** numbers as identity rows (label, masked number, caller-access badge, connected
  state, today's calls); the editor in grouped sections; checklist cards collapse once done.
- **Status:** KPI band from `kpi_parts.html`, tables with tabular numerals, clear ok/warn/error
  badges using the palette tokens, latency targets drawn as reference marks.
- **Visual standard:** existing CSS tokens and Bootstrap primitives only, a new
  `weightroom/static/voice-admin.css` for layout and `voice-admin.js` for selection/drafts; light
  and dark palettes, keyboard navigation and visible focus, accessible names on every control,
  no horizontal scroll at phone width, empty and error states designed rather than default text.
- **Template index:** add the Voice area row to [AGENTS.md](../AGENTS.md#template-index) with its
  shared parts (`voice_admin_parts.html`).
- Tests: UI consistency audit and all-page browser checks (desktop and narrow, light and dark,
  empty/error states, polling replacements), existing voice settings save/reset tests unchanged,
  screenshots attached to the review.

## Out of scope

Outbound calling from conversations, SMS beyond verification codes, call recording on Twilio's
side, other telephone providers (SIP, Vonage, Telnyx), billing and usage import from Twilio, and
WebRTC for browser calls.
