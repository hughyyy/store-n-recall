# store-n-recall

[![tests](https://github.com/hughyyy/store-n-recall/actions/workflows/tests.yml/badge.svg)](https://github.com/hughyyy/store-n-recall/actions/workflows/tests.yml)
[![python](https://img.shields.io/badge/python-3.10%2B-blue)](pyproject.toml)

A lightweight [MCP](https://modelcontextprotocol.io) server that gives agents a
persistent knowledgebase. Agents **store** durable facts about a project and
**recall** them in one call, so knowledge survives sessions, agents, and machine
restarts.

Third in the collection — [look-n-see](https://github.com/hughyyy/look-n-see)
(vision/OCR), [scrape-n-search](https://github.com/hughyyy/scrape-n-search) (web),
store-n-recall (project knowledge).

> **Status: implemented.** The tools, env vars, and behaviour documented here
> match `src/`. Requirements live in [`BRIEF.md`](BRIEF.md), the design and
> hardening rationale in [`IMPLEMENTATION.md`](IMPLEMENTATION.md), and the
> vocabulary in [`CONTEXT.md`](CONTEXT.md).

## Why

Every session an agent re-derives the same things about a repo: the build
command, the test that flakes, the decision to avoid a dependency, the
convention nobody wrote down. Transcripts are the wrong place for that — they
are long, per-session, and gone. store-n-recall keeps one small store per
machine where any agent persists a short fact under a project name and any
agent recalls it in one call.

What it deliberately is **not**: embeddings or vector search, automatic capture,
transcript dumping, TTL garbage collection, or per-session scoping. One SQLite
file, stdlib only, fully offline.

## Quick start

```bash
git clone https://github.com/hughyyy/store-n-recall
cd store-n-recall
python3 -m venv .venv
.venv/bin/pip install -e .

.venv/bin/python -m unittest discover -s tests -q   # offline suite
.venv/bin/store-n-recall                            # stdio MCP server
```

Python 3.10+. The only dependency is `mcp>=2,<3`; the server starts with no store
file present and creates it on the first tool call.

### Register with a client

Any stdio MCP client works. For Oh My Pi, put this in `~/.omp/agent/mcp.json`:

```json
{
  "mcpServers": {
    "store-n-recall": {
      "type": "stdio",
      "command": "/absolute/path/to/store-n-recall/.venv/bin/store-n-recall",
      "env": { "STORE_N_RECALL_PROJECT": "my-repo" }
    }
  }
}
```

`STORE_N_RECALL_PROJECT` is optional — without it, every tool call must pass
`project` explicitly, which is what you want when one client serves many repos.

### A session, end to end

```text
store_fact(fact="Build with `python -m build`; the wheel ships py.typed.",
           kind="convention", tags=["repo", "build"], source="pyproject.toml")
→ {"fact": {"id": 1, "project": "my-repo", "kind": "convention",
           "text": "Build with `python -m build`; the wheel ships py.typed.",
           "tags": ["build", "repo"], "confidence": 0.5,
           "created_at": "2026-10-23T02:59:55.203+00:00",
           "archived_at": null, "supersedes": null,
           "sources": [{"action": "stored", "agent": "omp",
                        "at": "2026-10-23T02:59:55.203+00:00",
                        "session": null, "note": null,
                        "pointer": "pyproject.toml"}]}}

recall_facts(query="wheel")
→ {"facts": [{"id": 1, ..., "sources": null}]}      # summary rows

recall_facts(id=1)
→ {"facts": [{"id": 1, ..., "sources": [...full provenance...]}]}
```

Every tool returns the same JSON both as `structuredContent` and as text, and
advertises an `outputSchema`. Bad input — unknown id, unknown kind, missing
project — is a **tool error** (`isError: true`) whose message states the fix.

## Tools

| Tool | Params | Returns |
| --- | --- | --- |
| `store_fact` | `fact` (required), `project`?, `kind`?, `tags`?, `confidence`?, `source`?, `supersedes`? | `{"fact": {...}}` — the stored fact, provenance included |
| `recall_facts` | `project`?, `id`?, `query`?, `kind`?, `tags`?, `min_confidence`?, `limit`? | `{"facts": [...]}` of active facts — relevance-ranked with `query`, newest-first otherwise; with `id`, exactly one full record |
| `archive_fact` | `id` (required), `note`? | `{"fact": {...}}` — the archived fact, `archived_at` set, hidden from recall from then on |
| `scour_archives` | same filters as `recall_facts` | `{"facts": [...]}` — archived facts only, most recently archived first |
| `list_projects` | — | `{"projects": [...]}` with active/archived counts, top tags, latest activity |

Five tools, no more: new recall needs are expressed as filters, not new tools.
Annotations are declared too — `recall_facts`, `scour_archives`, and
`list_projects` are read-only; `store_fact` is a non-destructive write;
`archive_fact` is marked destructive because it retires a fact.

**Summary vs. full records.** List results are summary rows with `sources: null`.
An `id` lookup (in `recall_facts` or `scour_archives`) returns the full record,
complete source list included.

`list_projects` reports per project the active/archived counts, the five most
frequent tags (counted across both, ties by name), and the newest
`created_at`/`archived_at` as `latest_activity`.

**Filters compose with `id`.** A valid id that no filter matches yields
`{"facts": []}`; an id that is unknown, or on the wrong side of the archive
flag, is an error. `limit` defaults to 50 and is applied after tag filtering.

## Fact model

| Field | Notes |
| --- | --- |
| `id` | server-assigned, stable |
| `project` | the scope — one codebase per project; facts never cross projects |
| `kind` | `decision`, `gotcha`, `convention`, or `fact` (default) |
| `text` | the fact itself — one self-contained statement, capped at 16,384 chars |
| `tags` | zero or more labels, stored sorted and deduped |
| `confidence` | 0.0–1.0 stated by the storing agent, default 0.5 |
| `sources` | append-only provenance: one entry per event, `stored` or `archived`, each with `agent`, `session`, `at`, optional `note` and `pointer` (commit, file, URL) |
| `created_at` | UTC timestamp of the store event (ISO-8601, millisecond precision) |
| `archived_at` | UTC timestamp, present once archived |
| `supersedes` | id of the fact this one replaced, present once used |

**Kinds** are a closed taxonomy — `decision`, `gotcha`, `convention`, `fact`.
**Tags** are open: `repo`, `project`, `feature`, `build`, `test`, `deps`,
`docs`, `infra` are the starter set agents reach for first, not a whitelist.
Several tags combine with **AND** (`repo` + `build` = granular; `feature`
alone = broad); pair them with `kind` and `min_confidence` for finer cuts.

**Project resolution:** the `project` param wins, then
`STORE_N_RECALL_PROJECT`, else a clear error. There is deliberately no
cwd-derived default — the MCP process's cwd is wherever the client launched it,
not the agent's repo.

**Provenance agent:** MCP `clientInfo.name` when a request context exists, then
`STORE_N_RECALL_AGENT`, then `unknown`. Session ids are recorded when the
transport exposes one and reported as absent otherwise — never fabricated.

## Lifecycle

Facts are **immutable**. The only two changes in the schema are "insert a fact"
and "set `archived_at`". A stale fact is retired one of two ways:

- **Supersede** — store a correction with `supersedes=<old id>`. In one write
  transaction the new fact is stored (carrying `supersedes`), the old fact is
  archived, and each provenance trail gets a note linking the other
  (`supersedes fact 1` / `superseded by fact 2`). The target must be active and
  in the same project, or nothing is written.
- **Archive** — `archive_fact(id, note=...)` retires a fact on its own.

Archived facts are invisible to `recall_facts`, visible only through
`scour_archives`, and kept forever. The archive is a visibility flag, not a
trash bin: the database rejects `DELETE` and any content `UPDATE` with a
trigger, so no code path — including an external writer — can remove a row or
rewrite history.

## Recall semantics

**Query matching.** A `query` is split into word tokens and **every** token must
match (implicit AND).

- With FTS5 (default on every mainstream build): whole-token, case- and
  diacritic-insensitive. `deploy` does not match "deployment"; `cafe` matches
  "café"; CJK text matches as whole runs. User input is quoted as phrase
  tokens, so FTS syntax (`AND`, `NEAR(...)`, `"..."`) is inert data, and an
  all-punctuation query matches nothing rather than everything.
- Without FTS5: a literal `LIKE` substring match, ASCII case-insensitive —
  `deploy` matches "deployment", `cafe` misses "café" — with `%`, `_`, and `\`
  escaped so punctuation in facts stays literal.

**Ordering.** Recall without a query is newest-first; recall with a query under
FTS5 is relevance-ranked (ties newest first); under the LIKE fallback it is
newest-first. `scour_archives` is always ordered by archive time, query or not —
there a query filters, it does not rank.

**Confidence** defaults to 0.5 and is bounded to `[0.0, 1.0]` (NaN, infinity, and
overflow rejected). `min_confidence` is an inclusive floor, and `0.0` is a real
floor rather than "unset".

Recall quality targets a few hundred facts per project, not corpus search.

## Storage and architecture

```
src/store_n_recall/
  server.py   MCPServer, five tool wrappers, typed result models, stdio main()
  store.py    SQLite storage: schema, guards, FTS mirror, validation, lifecycle
  config.py   env parsing, defaults, project resolution
  __main__.py python -m store_n_recall
```

One SQLite file, default `~/.local/share/store-n-recall/facts.db` (`XDG_DATA_HOME`
honored, `STORE_N_RECALL_DB` overrides). Two tables:

- `facts(id, project, kind, text, tags JSON, confidence, created_at,
  archived_at, supersedes → facts)`, indexed on
  `(project, archived_at, created_at DESC)`.
- `sources(id, fact_id → facts, action, agent, session, at, note, pointer)` —
  the append-only audit trail, in insertion order.

Plus an external-content FTS5 mirror `facts_fts` fed by an insert-only trigger,
and database-level guards installed as triggers so they also protect stores
written by older builds: no `DELETE` on facts or sources, no content `UPDATE` on
facts, no `UPDATE`/`DELETE` on sources at all, and inserts rejected for an
unknown kind, out-of-range confidence, or unknown source action.

**Connections.** Each operation opens a short-lived connection
(`journal_mode=WAL`, `synchronous=NORMAL`, `busy_timeout=5000`,
`foreign_keys=ON`); writes run `BEGIN IMMEDIATE` so concurrent writers queue at
the lock instead of failing with `SQLITE_BUSY`, and a lock that outlasts the
timeout surfaces as an error rather than a silently dropped write. Schema setup
(DDL, migration, guards, FTS mirror) runs once per `Store` and is cached — it is
idempotent, but repeating it per operation would cost a write transaction for
nothing.

**Concurrency.** WAL gives one writer at a time and many readers. Parallel MCP
server processes, one per agent session, can therefore share a single file
safely; a real multi-process test covers it. Nothing shares a sqlite3 connection
across threads, so the sync tool wrappers the SDK runs on worker threads are
safe.

**Migration.** The schema carries `PRAGMA user_version` (currently 2) and
upgrades in place on first open — a pre-`supersedes` store gains the column and
its guards in one write transaction. Facts stored while FTS5 was unavailable (or
by a build whose index was partially built) are detected by comparing
`facts_fts_docsize` against the fact count and reindexed in a single write
transaction; an unusable mirror is dropped and rebuilt rather than left broken.

**Startup.** No file is touched until the first call, so the server registers its
tools with no store present; a configuration error (for example no locatable
data directory) prints an actionable line to stderr and exits non-zero instead
of dying in a traceback inside the client.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `STORE_N_RECALL_DB` | `~/.local/share/store-n-recall/facts.db` | store file path (`~` expanded; `XDG_DATA_HOME` honored) |
| `STORE_N_RECALL_PROJECT` | — | default project when a tool call omits `project` |
| `STORE_N_RECALL_AGENT` | `unknown` | provenance agent when the MCP client supplies no name |

No network, no secrets, no API keys.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/python -m compileall -q src
.venv/bin/python -m unittest discover -s tests -q    # 288 tests, offline
```

The suite covers storage semantics and edge cases (both FTS5 and LIKE
backends), model-based randomized scenarios, real multi-process concurrency,
and the full tool surface end to end over a real stdio transport. CI runs it on
Python 3.10, 3.12, and 3.14.

## Non-goals

- No embeddings or similarity ranking.
- No automatic capture, transcript dumping, or TTL garbage collection — agents
  store, supersede, and archive deliberately.
- No per-session scoping: provenance is metadata, and recall always crosses
  sessions.
- No server-side aggregation beyond `list_projects` counts and tag summaries.
- Still no hard delete, ever.

## Docs

| File | Contents |
| --- | --- |
| [`BRIEF.md`](BRIEF.md) | requirements |
| [`IMPLEMENTATION.md`](IMPLEMENTATION.md) | design, invariants, release plan |
| [`CONTEXT.md`](CONTEXT.md) | vocabulary — fact, project, store, recall, supersede, archive, source |
| [`AGENTS.md`](AGENTS.md) | conventions for working in this repo |
