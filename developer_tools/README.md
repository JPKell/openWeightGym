# Developer tools

The code and documentation MCP tools live together in `developer_tools/`. They help developers
and agents find current implementation details, inspect possible change impact and answer
questions with source references. ToolLocker supplies authentication and permissions; the
existing `toollocker.tools.code` command and `toollocker.docs` imports remain compatible.

## Connect and inspect

Connect an MCP client to the configured control-node listener, normally
`http://CONTROL:8443/mcp`, with its own OWG account key. Use a trusted LAN/VPN or HTTPS proxy.
The account needs the relevant tool patterns and sensitivity level; model-driven calls also
need `use-tools`. For console checks, open **Settings → Tools → Try a tool → Connect**.
[ToolLocker setup](/docs/modules/toollocker) covers installation and access control.

| Tools | Purpose |
| --- | --- |
| `docs__search`, `docs__read`, `docs__list_topics` | Search and read caller-visible guides with canonical citation URLs. |
| `docs__find_endpoint`, `docs__lookup_setting`, `docs__explain_ability` | Generated API, shipped-default and permission references. |
| `docs__code_outline`, `docs__code_symbol` | Read allowlisted source documentation; administrator access required. |
| `code__repositories`, `code__status`, `code__log`, `code__diff`, `code__search` | Inspect explicitly allowed Git repositories and tracked working-tree files. |
| `code__symbols`, `code__read_symbol` | Discover Python definitions, then read their exact source and docstrings. |
| `code__related` | Follow containment, call, import and inheritance relationships; use `direction="incoming"` to investigate possible impact. |
| `code__docstring_search` | Search names/docstrings lexically, or use configured semantic vectors. |
| `code__index_status` | Inspect refresh time, excluded files, vector progress and retry errors. |

Start with `code__repositories`. Call `code__symbols` with a name/path fragment or
`code__docstring_search` with a question. Pass a returned `id` as `symbol` to
`code__read_symbol` or `code__related`. Follow `next_offset` on paged results. Source results
include repository-relative paths, line ranges and SHA-256 hashes. Read the source before
editing; incoming relationships are leads to investigate, not proof of all affected callers.

[Docs Ask](/docs/web-app/help) uses the existing Responses tool loop and canonical help prompt.
It still needs a configured `docs-help` role and an available model; these code tools do not
install or choose one. Administrators may use allowed `code__*` tools alongside docs tools.

## Automatic source index

The bundled code server builds a SQLite database for each allowed repository automatically.
There is no separate database service, GPU requirement or manual import step. Queries refresh
the source index before returning; a background cycle also refreshes it every 30 seconds.
Changed content is reparsed, new tracked files are added, and deleted or invalid files lose
their stale symbols, relationships and vectors in the same transaction. Restarting resumes
from the durable cache. A failed refresh makes normal queries fail rather than silently return
old source; `index_status` reports the failure and the last successful update.

Only Git-tracked regular `.py` files are indexed, including staged additions and uncommitted
edits. Untracked files and symlinks are excluded. **Tracked files are not necessarily public:
never grant access to a repository containing secrets.** Parsing uses Python's AST and never
imports the repository's code. Bounds are 5,000 files, 1 MiB per file, 64 MiB total source and
50,000 symbols. Invalid/unreadable files are reported in `excluded` (first 50 entries).

The graph stores `files`, `symbols` and `edges`; `docs` is an SQLite FTS5 table, and `vectors`
stores normalized embedding arrays with source hashes and an embedding identity. Containment
edges are exact. Calls, imports and bases retain their source spelling and are marked
`syntactic_candidate`; aliases, dynamic dispatch and external targets are unresolved.
Incoming name matches can include unrelated symbols. This is a Python source graph, not a
complete cross-language dependency or runtime call graph.

Caches live under the service account's `~/.cache/openweightgym/code/`, in owner-readable
SQLite files named from the repository path and embedding configuration. Different embedding
identities use separate caches so concurrent hosts cannot mix vectors. They contain source-derived private information,
so keep them outside shared folders and repositories. They are disposable: stop the tool host,
remove its database, then restart to rebuild. Source, settings and models remain authoritative.

## Configure semantic docstring search

Lexical search works immediately. Semantic search is opt-in and requires an existing
OpenAI-compatible embedding endpoint. Prefer the normal OWG `/v1/embeddings` service with
an allowed local embedding model; this uses the existing scheduler and account limits.
No model is downloaded and no separate inference engine is started by the code server.

Create an operator-owned TOML file, for example `settings/developer-tools.toml`:

```toml
[code]
repositories = ["/home/you/openWeightGym"]
index_directory = "/home/you/.cache/openweightgym/code"
index_interval = 30
embedding_url = "http://127.0.0.1:8442/v1/embeddings"
embedding_model = "YOUR_EMBEDDING_MODEL"
embedding_revision = "YOUR_IMMUTABLE_MODEL_REVISION_OR_WEIGHT_HASH"
embedding_key_file = "/home/you/openWeightGym/keys/code-embedding.key"
```

The key file contains a narrowly permitted OWG account key, not the machine signing key.
Use absolute paths; keep the key owner-readable. Configuring an endpoint authorizes sending
indexed symbol names/docstrings and semantic search queries to it. An external provider
therefore receives source-derived text. Credentials are never arguments to MCP tools.
Redirects are refused, responses are bounded, and OWG requests include the no-wake header.

Add `"--settings", "/home/you/openWeightGym/settings/developer-tools.toml"` to the existing
`servers.code.command` in `settings/toollocker.toml`, keeping its Python and module arguments.
Repository positional arguments, if present, override the TOML repository list. Alternatively,
run `python -m developer_tools.code --settings /absolute/path/developer-tools.toml`.
Tool-folder hosts accept the same `[code]` section in their existing `--settings` file.
Use `owg restart toollocker` after changing operator configuration; ordinary source edits need
no restart. Apply software updates through `owg update` followed by `owg restart`.

Each background cycle embeds up to 16 pending docstrings (at most 8,000 characters each).
Failed batches remain pending and rotate behind unattempted work before retrying, so one bad
batch cannot block the entire queue. `failed_docstrings` reports unresolved failures. Lexical search and the graph remain
usable. Responses are published only if the indexed source hash still matches. Set
`semantic=true` on `code__docstring_search`; its status includes pending work so callers can
recognize partial results. Scores are cosine similarity, not calibrated confidence. A query
embedding failure is explicit; it never silently becomes lexical search.

Changing the endpoint, model or revision selects a fresh cache on restart; unused old databases
can be removed while their host is stopped. Set a new
`embedding_revision` whenever weights change behind a stable model name: same-dimension
weight replacements cannot be detected from vectors alone. Dimension changes clear stored
vectors during indexing or refuse a mismatched search. No embedding work occurs without
`embedding_url`; omit all embedding fields to keep source entirely local.

## External developer integrations

Superusers can add these official HTTP servers through **Tools → Remote MCP servers**.
Use the existing remote editor and review the discovered tools and group access. Public mode
is only for intentionally unauthenticated HTTPS servers. See
[remote server configuration](/docs/modules/toollocker#add-other-mcp-servers) for credentials,
removal and fixed stdio commands.

| Server | MCP URL | Use and authentication |
| --- | --- | --- |
| [Context7](https://github.com/upstash/context7) | `https://mcp.context7.com/mcp` | Version-specific library docs through `resolve-library-id` and `query-docs`; optional bearer key raises service limits. |
| [Microsoft Learn](https://learn.microsoft.com/en-us/training/support/mcp) | `https://learn.microsoft.com/api/mcp` | Public Microsoft documentation and code samples; public HTTPS mode, no key. |
| [GitHub](https://github.com/github/github-mcp-server/blob/main/docs/server-configuration.md) | `https://api.githubcopilot.com/mcp/x/all/readonly` | Read-only repository/issue tools using a suitable GitHub bearer token. |

These are upstream services, not bundled dependencies. Availability, quotas and permissions
belong to their operators. The OWG remote editor supports bearer credentials; it does not run
an interactive OAuth login or configure arbitrary request headers. Use compatible URL options
(such as GitHub's read-only endpoint) or an operator-managed stdio server when necessary.

## Extend and verify

| File | Responsibility |
| --- | --- |
| `developer_tools/code.py` | Stable MCP tools, argument contracts, repository allowlist and CLI. |
| `developer_tools/code_index.py` | Static extraction, incremental SQLite transactions, background refresh and embeddings. |
| `developer_tools/docs.py` | Signed caller-bound docs adapter; visibility remains enforced by WeightRoom. |
| `toollocker/tools/code.py`, `toollocker/docs.py` | Compatibility entry points; keep existing installations working. |
| `tests/test_developer_tools.py`, `tests/test_docs_tools.py` | Index lifecycle, boundaries, MCP protocol and caller permissions. |

For an additional code tool, add a bounded `@tool` function in `code.py` and include it in
`TOOLS`. Its signature generates the MCP schema; its docstring explains purpose, inputs,
limits and result. Use the existing index and repository selector. Never accept shell commands,
arbitrary database SQL or unrestricted file paths. Tool hosts support an explicit `TOOLS`
export list for compatibility adapters; ordinary custom modules still discover their own
`@tool` functions. [Tool authoring](/docs/modules/toollocker#add-a-python-tool) covers independent
custom tools and deployment.

For a new parser, extend extraction while keeping source hashes, transactional stale-row
removal and explicit unresolved edges. Changing the database schema requires a version bump
and a documented migration or disposable-cache rebuild. Test source edits, deletion, rename,
invalid input, interrupted providers, permissions and the actual MCP transport. Keep generated
reports, browser sessions and credentials out of Git; regression test source belongs in Git.

Run from the repository root:

```bash
.venv/bin/pytest tests/test_developer_tools.py tests/test_toollocker.py tests/test_docs_tools.py
.venv/bin/python scripts/docs-coverage.py
```

The documentation maintenance checks in `docs/agents.md` cover the guide catalogue and source
references. Generated source documentation and MCP schemas come from the implementation;
update this guide when behavior or operator setup changes. Changed tool implementations/schema
apply after the normal host restart; repository content updates automatically without one.
