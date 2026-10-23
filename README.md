# store-n-recall

A lightweight [MCP](https://modelcontextprotocol.io) server that gives agents a
persistent knowledgebase: **store** and **recall** facts about projects and repos,
so knowledge survives between agents and sessions. Third in the collection —
[look-n-see](../look-n-see) (vision/OCR), [scrape-n-search](../scrape-n-search)
(web), store-n-recall (project memory).

> **Status: implemented** — tools, env vars, and behavior below match the
> server in `src/`. See `BRIEF.md` (requirements) and `IMPLEMENTATION.md`
> (design).

## The idea

Every session, agents re-derive the same facts about a repo: build commands,
gotchas, decisions already made, conventions to follow. store-n-recall keeps one
small store per machine where any agent can persist those facts under a project
name and recall them in one call.

- Facts are short, durable statements — decisions, gotchas, conventions — not chat
  logs or transcripts. Agents store deliberately; provenance is recorded.
- Scoped by **project**: one slug per codebase, and every session sees the same
  store.
- Fully offline, stdlib-only storage: one SQLite file, no embeddings, no vector
  DB, no sync. Recall is full-text match.
- Nothing is ever hard-deleted: facts are archived — hidden from recall, kept
  forever — and retrievable via `scour_archives`.
- Tagged with a small default vocabulary (`repo`, `project`, `feature`, ...) plus
  free-form tags, combinable for granular or broad search.

## Tools

| Tool | Params | Returns |
| --- | --- | --- |
| `store_fact` | `project`?, `fact` (required), `kind`?, `tags`?, `confidence`?, `source`?, `supersedes`? | `{"fact": {...}}` — the stored fact, provenance included |
| `recall_facts` | `project`?, `id`?, `query`?, `kind`?, `tags`?, `min_confidence`?, `limit`? | `{"facts": [...]}` of active facts — relevance-ranked when `query` is given, newest-first otherwise; with `id`, exactly one full record (sources included) |
| `archive_fact` | `id` (required), `note`? | `{"fact": {...}}` — the archived fact, `archived_at` set, hidden from recall from then on |
| `scour_archives` | same filters as `recall_facts` | `{"facts": [...]}` — archived facts only; with `id`, exactly one full record |
| `list_projects` | — | `{"projects": [...]}` with active/archived fact counts, top tags, latest activity |

Every tool advertises an **output schema** and returns the same JSON as
`structuredContent` and as text. Invalid input (unknown id, bad kind, missing
project, …) is a **tool error** (`isError: true`) whose message states what to
fix — not a prose string inside a successful result.

**Filters and `id`**: filters compose with every lookup, including `id`. A
valid id that no filter matches yields `{"facts": []}`; an id that is unknown
or on the wrong side of the archive flag is an error. `recall_facts(id=...)`
returns the full record (complete source list); list calls return summary rows.

Summary rows carry `sources: null`; only an `id` lookup (or the store/archive
return value) includes the full source list. `list_projects` reports the five
most frequent tags per project (counted across active and archived facts, ties
by name) and latest activity = the newest `created_at`/`archived_at` there.

**Project resolution**: the `project` param wins; else the `STORE_N_RECALL_PROJECT`
env var; else a clear error. There is deliberately no cwd-derived default — the
MCP server process's cwd is wherever the client launched it, not the agent's repo.

**Kinds**: `decision`, `gotcha`, `convention`, `fact` (default when omitted).

**Default tags**: `repo`, `project`, `feature`, `build`, `test`, `deps`, `docs`,
`infra` — a convention, not a whitelist; free-form tags are welcome. Tags combine
with AND: several tags (`repo` + `build`) give a granular slice, one tag
(`feature`) stays broad. Pair with `kind` and `min_confidence` for finer cuts.

**Query semantics**: a `query` is split into word tokens, and every token must
match (implicit AND). With FTS5, matching is **whole-token**, case- and
diacritic-insensitive: `deploy` does not match "deployment", `cafe` matches
"café", and CJK runs match only as whole runs. FTS syntax (`AND`, `NEAR(...)`,
quotes) is inert — it is data, not operators. On a sqlite3 without FTS5 the
fallback is a literal `LIKE` substring match (ASCII case-insensitive): `deploy`
does match "deployment", `cafe` does not match "café". Recall is ranked by
relevance under FTS5 and newest-first under the fallback; scour always orders by
archive time.

## Fact model

| Field | Notes |
| --- | --- |
| `id` | server-assigned, stable across recall calls |
| `project` | the scope (see resolution above) |
| `kind` | one of the four kinds |
| `text` | the fact itself — one self-contained statement |
| `tags` | zero or more labels (see default tags above) |
| `confidence` | 0.0–1.0 stated by the storing agent, default 0.5 |
| `sources` | append-only provenance list — `{agent, session?, at, action, note?, pointer?}` per event: `stored` at creation, `archived` on archive; `source?` param on store adds a pointer (commit, file, URL); supersede records the link as a note on both events |
| `created_at` | UTC timestamp of the store event |
| `archived_at` | UTC timestamp, present once archived |
| `supersedes` | id of the fact this one replaced, present once used |

Facts are immutable and never hard-deleted (the database enforces both: a
trigger rejects `DELETE` and any content `UPDATE`; only `archived_at` changes).
A wrong fact is corrected by storing a new one with `supersedes=<id>` — the old
fact is archived in the same act, the new record carries `supersedes`, and the
old fact's `archived` source entry notes `superseded by fact <new id>` — or
retired with `archive_fact`. History is never rewritten, and archived facts stay
forever, reachable via `scour_archives`.

List a fact's sources with `recall_facts(id=...)`: the full record includes the
complete source list — every store/archive event with its agent, session,
timestamp, and optional note/pointer.

## Storage

One SQLite database, default `~/.local/share/store-n-recall/facts.db`
(`STORE_N_RECALL_DB` overrides the path). WAL mode plus a busy timeout, so the
parallel server processes that separate agent sessions spawn can share one file
safely. Full-text search via FTS5 where the platform's sqlite3 has it, with a
LIKE fallback where it doesn't — recall quality targets "a few hundred facts per
project", not corpus search. Facts are capped at 16,384 characters so an agent
cannot turn the store into a transcript dump; `text`, `source`, `note`, `agent`,
and `session` are whitespace-normalized on the way in, and tags are stored
sorted and deduped.
The schema carries a `user_version` and upgrades itself in place (an older store
gets the `supersedes` column on first open). Archived facts remain in the file
forever — the server never deletes rows; the archive is a visibility flag, not a
trash bin.

## Install

```bash
python3 -m venv .venv
.venv/bin/pip install -e .

# run as a stdio MCP server
.venv/bin/store-n-recall
```

Python 3.10+. No API keys — the server is fully offline.

### Register with Oh My Pi

Add to `~/.omp/agent/mcp.json`. Give a shared agent a project default via env, or
omit `STORE_N_RECALL_PROJECT` and pass `project` per call:

```json
{
  "mcpServers": {
    "store-n-recall": {
      "type": "stdio",
      "command": "/absolute/path/to/store-n-recall/.venv/bin/store-n-recall"
    }
  }
}
```

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `STORE_N_RECALL_DB` | `~/.local/share/store-n-recall/facts.db` | store file path (honors `XDG_DATA_HOME`) |
| `STORE_N_RECALL_PROJECT` | — | default project when a tool call omits `project` |
| `STORE_N_RECALL_AGENT` | `unknown` | provenance agent name when the MCP client supplies none |

## Non-goals

- No embeddings or similarity ranking — facts are short and exact-match/FTS is
  enough at this scale.
- No automatic capture, transcript dumping, or TTL garbage collection — agents
  store and supersede deliberately.
- No per-session scoping — provenance is metadata; recall always crosses sessions.
- No server-side aggregation beyond `list_projects` counts and tag summaries.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/python -m unittest discover -s tests -q   # offline suite
```

The server must start and register tools with no store file present (created on
demand) and never require network. See `CONTEXT.md` for the vocabulary — fact,
project, store, recall, supersede, archive, source — and `AGENTS.md` for project
conventions.
