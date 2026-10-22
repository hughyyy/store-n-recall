# store-n-recall

A lightweight [MCP](https://modelcontextprotocol.io) server that gives agents a
persistent knowledgebase: **store** and **recall** facts about projects and repos,
so knowledge survives between agents and sessions. Third in the collection —
[look-n-see](../look-n-see) (vision/OCR), [scrape-n-search](../scrape-n-search)
(web), store-n-recall (project memory).

> **Status: planned** — this README is the target surface. Implementation has not
> started; tool names and env vars below are the design the first implementation
> should follow.

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

## Planned tools

| Tool | Params | Returns |
| --- | --- | --- |
| `store_fact` | `project`?, `fact` (required), `kind`?, `tags`?, `supersedes`? | the stored fact as JSON |
| `recall_facts` | `project`?, `query`?, `kind`?, `tags`?, `limit`? | `{"facts": [...]}` — relevance-ranked when `query` is given, newest-first otherwise |
| `forget_fact` | `id` (required) | `{"forgotten": id}` |
| `list_projects` | — | projects with fact counts and latest activity |

**Project resolution**: the `project` param wins; else the `STORE_N_RECALL_PROJECT`
env var; else a clear error. There is deliberately no cwd-derived default — the
MCP server process's cwd is wherever the client launched it, not the agent's repo.

**Kinds**: `decision`, `gotcha`, `convention`, `fact` (default when omitted).

## Fact model

| Field | Notes |
| --- | --- |
| `id` | server-assigned, stable across recall calls |
| `project` | the scope (see resolution above) |
| `kind` | one of the four kinds |
| `text` | the fact itself — one self-contained statement |
| `tags` | zero or more free-form labels |
| `agent` | storing agent's name, for provenance |
| `created_at` | UTC timestamp |

Facts are immutable. A wrong fact is corrected by storing a new one with
`supersedes=<id>` (old fact retired in the same act) or removed with `forget_fact`.
Facts are never edited in place and history is never rewritten — the store stays
append-mostly and auditable.

## Storage

One SQLite database, default `~/.local/share/store-n-recall/facts.db`
(`STORE_N_RECALL_DB` overrides the path). WAL mode plus a busy timeout, so the
parallel server processes that separate agent sessions spawn can share one file
safely. Full-text search via FTS5 where the platform's sqlite3 has it, with a
LIKE fallback where it doesn't — recall quality targets "a few hundred facts per
project", not corpus search.

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

## Non-goals

- No embeddings or similarity ranking — facts are short and exact-match/FTS is
  enough at this scale.
- No automatic capture, transcript dumping, or TTL garbage collection — agents
  store and supersede deliberately.
- No per-session scoping — provenance is metadata; recall always crosses sessions.
- No server-side aggregation beyond `list_projects` counts.

## Development

(once implemented)

```bash
.venv/bin/pip install -e .
.venv/bin/python -m unittest discover -s tests -q   # offline suite
```

The server must start and register tools with no store file present (created on
demand) and never require network. See `CONTEXT.md` for the vocabulary — fact,
project, store, recall, supersede — and `AGENTS.md` for project conventions.
