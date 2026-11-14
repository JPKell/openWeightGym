# Advisory spending allowances

C07 / milestone 107 implements the owner's **soft enforcement** decision of 2026-10-15.
**Settings → Spending allowances** reports rolling 24-hour USD allowances for individual
users, pooled groups and persistent API keys. All are disabled initially; setting zero disables
one. Exceeding an allowance does not reject, redirect or reserve a request. Existing hosted
user/service spending ceilings and all normal authorization/admission rules still apply.

## Meaning of remaining

Each row shows its allowance, recorded cost, nonnegative recorded remainder, excess, unknown-cost
endings and requests without a retained ending. The same request contributes to its user, key
and recorded group, so these scopes must not be added together. Groups pool their members'
recorded costs; these are separate from the existing per-user `max_hosted_spend` setting.

The ordinary event log is the only accounting authority. The newest retained ending per request
in the rolling window is counted once, whether completed, failed, refused, stopped or hung up.
Separate retries keep separate charges. Known numeric zero counts as known; invalid, negative,
nonfinite and absent costs remain unknown. Recorded costs can include estimates saved at request
time. Current pricing and the Usage page's model-rate/mock estimates do not fill missing costs.
These allowances do not claim a hard cap, provider billing reconciliation or complete accounting.

Window boundaries use absolute seconds and ending timestamps, independent of Usage calendar
filters or daylight saving. “Without ending” covers retained starts inside the window without
an ending at the report instant: it includes lost endings, not only currently running work.
Missing costs, unrecorded requests, log retention and delayed compute delivery may overstate the
remainder. Isolated benchmark probes, unlogged local requests and plugin-adaptor estimates are
outside this first increment. Retained records shorter than one day cannot cover the full window.
The page refreshes manually. If compute cannot flush its existing outbox without waking, the
page warns that the report may be stale; the control node's retained history remains available.

## Attribution and access

New ordinary compute requests snapshot immutable `account_id`, opaque `key_id` and their group
on both start and ending. Turnstile derives the key attribution ID from the already hashed random
key using a domain-separated SHA-256; neither the credential nor its stored verifier is exposed.
The ID travels inside the existing signed caller statement, not an independently trusted caller
header. Account renames preserve user/key attribution. Deletion and recreation do not reattach
old user/key spend or policies. Removing a persistent key or account removes its allowance;
removing a group clears its named policy. Group history remains its recorded name, without
inferring later membership changes.

Older records without immutable account attribution are excluded from user/key amounts and
reported as legacy coverage. Background work that runs without a particular key still contributes
to its account/group where those snapshots exist; missing key attribution is separately counted.
Session keys can carry attribution but do not have editable session-specific policies here.
User/group rows include their ordinary recorded session work.

Members see their own user/key rows and their current group's pooled allowance, without other
members' request or key details. `see-log` permits all rows. Editing requires `see-log` plus
`manage-users` for user/key targets or `manage-groups` for groups. These soft policies grant no
inference/account authority. API responses and console pages use existing authentication and
no-store responses; successful policy changes are recorded in the existing audit log.

## Storage, API and deployment

`loadcoach.users` migration 4 adds `spending_allowances(scope, subject, amount)` to the existing
control node accounts store. There is no new database, credential, reservation ledger or scheduler.
Verified full-store backups/transfers preserve the policies and attribution-bearing log. The
legacy `accounts-from` first-move command refuses populated new policies rather than silently
omit them; use the verified full-store transfer workflow for that case.

`GET /loadcoach/allowances` returns authorized rows, fixed window/currency/mode, coverage and
compute synchronization status. `POST` accepts only `scope`, `subject`, `amount`; the subject
must be a current target returned to an authorized manager. Amount must be finite, nonnegative
and at most one billion USD; zero removes the policy. There is no hard-mode flag.
The console's `/allowances` page uses those existing control node account APIs and the caller's key.

Matching control node code/templates, accounts migration 4 and compute attribution code were present
on this installation by 2026-10-16. A restricted live account saw only its user/group/key rows,
all uneditable, through the 24-hour soft-USD API without waking compute. On 2026-10-17, a
temporary manager key and restricted fixture account exercised live API saves for user, key and
group allowances, scoped reads, permission and input errors, and zero-value removal. The fixtures
were removed and the policy table remained empty. Retained ordinary records included attributed,
legacy, unknown-cost and missing-ending cases; the live report displayed the coverage gaps. Exact
counts, responses and test limits are in the [private qualification receipt](../data/qualification/delivery-2026-10-16.json).
This qualifies the current soft policy workflow and its coverage display, not complete cost
accounting or provider billing.
Old compute/front statements remain readable, but cannot supply the new attribution;
coverage must not be called complete until both sides are updated. Keep backups before upgrade.
Rolling back code leaves the additive policy table and retained audit data intact; older code
has no allowance UI. Local verification did not itself change the production store.

Hard reservations remain outside this increment. The owner chose that, if separately authorized
later, a reservation without final usage/cost retains its estimate until reconciliation. That
conditional decision does not enable reservations now. App/session-specific policies, token/
request-rate allowances and richer provider pricing require separately selected scope.
