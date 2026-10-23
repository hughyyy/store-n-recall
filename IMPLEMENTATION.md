# store-n-recall — Implementation Plan

## Goal

A stdio MCP server (`store-n-recall`) implementing the five-tool knowledgebase
surface from `README.md` with stdlib SQLite storage. See `BRIEF.md` for
requirements.

## Stack

- Python 3.10+ (workstation: 3.14), `mcp>=2` only (MCPServer).
- Stdlib `sqlite3` (WAL + busy timeout), `unittest` (zero dev deps).
- setuptools src layout; console script `store-n-recall` →
  `store_n_recall.server:main`.

## Layout

```
pyproject.toml
README.md            # user-facing surface (spec)
BRIEF.md             # requirements (this project)
IMPLEMENTATION.md    # this file: design + release plan
CONTEXT.md           # vocabulary (facts/projects/sources/...)
src/store_n_recall/
  __init__.py
  __main__.py        # python -m store_n_recall
  config.py          # env parsing, defaults, project resolution
  store.py           # SQLite storage: facts/sources schema, FTS5/LIKE, validation
  server.py          # MCPServer + tool registration + main()
tests/
  test_config.py     # env/defaults/project-resolution coverage
  test_store.py      # storage semantics, edge cases, immutability, concurrency
  test_server.py     # tool registration + end-to-end over MCPServer
```

## Design

### Schema (`facts`, `sources`)

- `facts(id PK, project, kind, text, tags JSON, confidence, created_at,
  archived_at NULL-able)`. `tags` stored as sorted, deduped JSON so serialized
  output is deterministic.
- `sources(id PK, fact_id → facts, action 'stored'|'archived', agent, session
  NULL-able, at, note NULL-able, pointer NULL-able)` — the append-only audit
  trail; one row per event, insertion order preserved.
- FTS5 mirror `facts_fts(content='facts', content_rowid='id')` with an
  **insert-only** trigger: rows are never deleted or content-updated, so the
  mirror mirrors the no-delete invariant. FTS presence is probed on an
  in-memory connection; missing FTS5 → LIKE fallback (`LIKE ... ESCAPE '\'`
  with `%`/`_`/`\` escaped so literal punctuation in facts and queries works).
  No FTS query syntax is honored: user input is split into quoted phrase
  tokens, so FTS operators in queries are inert. A query with no word
  characters matches nothing (not everything).
- DDL is `IF NOT EXISTS` on every open; connections are short-lived per
  operation, `PRAGMA journal_mode=WAL`, `busy_timeout=5000`,
  `synchronous=NORMAL`, `foreign_keys=ON`. Writes run `BEGIN IMMEDIATE` so
  concurrent writers queue on the lock instead of failing with SQLITE_BUSY.

### Invariants enforced by construction

- **No deletion**: the only mutations are `INSERT INTO facts/sources` and
  `UPDATE facts SET archived_at` — there is no other UPDATE and no DELETE in
  the module. Tests assert row counts never shrink.
- **Immutability**: fact content is never rewritten; archive is a visibility
  flag. Tests assert text/kind/tags are byte-identical after archive/supersede.
- **Scope**: `supersedes` validates the target exists, is active, and belongs
  to the same resolved project, all inside the same write transaction as the
  insert — a failed supersede stores nothing.
- **Archive is the only retirement**: `recall_facts` (`archived_at IS NULL`)
  and `scour_archives` (`archived_at IS NOT NULL`) are complementary; double
  archive is an error; `recall_facts(id=)` on an archived fact is an error.

### Semantics decisions (spec gaps closed for tests)

- Timestamps: `datetime.now(timezone.utc).isoformat(timespec="milliseconds")`
  — ISO-8601 UTC, lexicographic == chronological.
- Ordering: recall no-query → `created_at DESC, id DESC` (newest first);
  recall with query + FTS → `rank` (relevance), ties newest id first;
  recall with query + LIKE → newest first (no ranking possible); scour →
  `archived_at DESC, id DESC` (most recently archived first).
- `limit` default 50, must be a positive int; applied after tag filtering.
- `id`/`supersedes` accept int (or digit strings from untyped callers),
  positive only, bools rejected.
- `archive_fact(id=)` on an active fact is an error; `scour_archives(id=)` on a
  non-archived fact is an error — id lookups fail loudly on the wrong side of
  the archive flag (filters may still narrow a correctly-sided id to empty).
- Input normalization: project/kind/text/tags/query stripped; kind lowercased
  and validated against the closed taxonomy; tags deduped + sorted; confidence
  coerced to float and bounds-checked (NaN/inf rejected, boundaries
  inclusive); `min_confidence` filter is inclusive (`confidence >= ?`).
- Result shapes: `store_fact`/`archive_fact` → full record (sources included);
  `recall`/`scour` lists → summary rows (`archived_at` only present once set);
  `id=` → `{"fact": <full record>}`; `list_projects` →
  `{"projects": [{project, active, archived, top_tags, latest_activity}]}`
  sorted by project name; top tags = top 5 by count (desc), ties by name,
  counted across active + archived facts; latest activity = max of
  created_at/archived_at.
- Errors: `FactError` → `error: <reason>` text (model-actionable); unexpected
  exceptions propagate as MCP tool errors.
- Provenance agent: MCP `clientInfo.name` when a request context exists →
  `STORE_N_RECALL_AGENT` → `unknown`. Session: taken from the connection when
  the SDK exposes one; null on stdio (session is optional in the model, so
  this degrades gracefully and is recorded as absent, never fabricated).
- DB path: `STORE_N_RECALL_DB` (expanded) → `$XDG_DATA_HOME/store-n-recall/
  facts.db` → `~/.local/share/store-n-recall/facts.db`; empty XDG value = unset.

## Concurrency

WAL allows one writer at a time; `BEGIN IMMEDIATE` + 5 s busy timeout queue
waiting writers. Covered by a real multi-process test (subprocess workers,
each with its own `Store`, burst-storing into one file) — threads on one
connection are NOT supported (each thread/process opens its own Store).

## Release plan

1. Scaffold package + config + store + server.               [done]
2. Docs: BRIEF/IMPLEMENTATION, README status + config table. [done]
3. Tests: config, store (edge cases + invariants), concurrency (real
   processes), server (end-to-end over MCPServer).           [done]
4. Verify: full offline suite, compileall, import smoke,
   stdio server starts and registers tools with no store file. [done]
5. Commit: conventional, one logical change per commit.
