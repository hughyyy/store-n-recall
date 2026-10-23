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
  test_store.py      # storage semantics, edge cases, immutability, both backends,
                     # model-based randomized scenarios, failure modes
  test_server.py     # tool registration + end-to-end over MCPServer
```

## Design

### Schema (`facts`, `sources`)

- `facts(id PK, project, kind, text, tags JSON, confidence, created_at,
  archived_at NULL-able, supersedes → facts NULL-able)`. `tags` stored as
  sorted, deduped JSON so serialized output is deterministic. `supersedes`
  records which fact a correction replaced (set on the replacement).
- `sources(id PK, fact_id → facts, action 'stored'|'archived', agent, session
  NULL-able, at, note NULL-able, pointer NULL-able)` — the append-only audit
  trail; one row per event, insertion order preserved. A supersede writes a
  note on both events (`supersedes fact N` / `superseded by fact N`), so the
  trail answers both "what replaced this?" and "what did this replace?".
- Schema versioning: `PRAGMA user_version = SCHEMA_VERSION` (2). Older stores
  are upgraded in place on first open: `ALTER TABLE facts ADD COLUMN
  supersedes …` inside one write transaction, then the guards are installed.
  This is the migration hook for future columns.
- Database-level guards (triggers, so they apply to existing stores too):
  `DELETE` on `facts`/`sources` aborts, an `UPDATE` of any fact content column
  aborts (only `archived_at` may change), and inserts are rejected for an
  unknown kind, an out-of-range confidence, or an unknown source action. The
  code invariants are now enforced by the engine as well.
- FTS5 mirror `facts_fts(content='facts', content_rowid='id')` with an
  **insert-only** trigger: rows are never deleted or content-updated, so the
  mirror mirrors the no-delete invariant. FTS presence is probed on an
  in-memory connection; missing FTS5 → LIKE fallback (`LIKE ... ESCAPE '\'`
  with `%`/`_`/`\` escaped so literal punctuation in facts and queries works).
  No FTS query syntax is honored: user input is split into quoted phrase
  tokens, so FTS operators in queries are inert. A query with no word
  characters matches nothing (not everything).
- Mirror setup is **one write transaction**: table, trigger, and any backfill
  commit together, so a concurrent opener either sees a finished mirror or
  waits and finds it ready, and a failed setup leaves nothing half-built.
  Backfill is required whenever the number of indexed documents
  (`facts_fts_docsize`) differs from the number of facts — an external-content
  FTS table answers `SELECT ... FROM facts_fts` from `facts` even when its
  index is empty, and a **partially** built index is possible (a build that
  created the trigger without a backfill, then stored one fact). Counting is
  the only completeness test; emptiness is not enough. A read-only readiness
  probe keeps normal connections out of the write lock.
- Schema setup (DDL, migration, guards, mirror) runs on the first connection
  of each `Store` instance and is cached: it is idempotent, but repeating the
  DDL per operation costs a write transaction for nothing. Every connection
  still sets the per-connection pragmas (`busy_timeout`, `synchronous`,
  `foreign_keys`); `journal_mode=WAL` is persistent.
- Connections are short-lived per operation, `PRAGMA journal_mode=WAL`,
  `busy_timeout` from `BUSY_TIMEOUT_MS` (5 s), `synchronous=NORMAL`,
  `foreign_keys=ON`. Writes run `BEGIN IMMEDIATE` so concurrent writers queue
  on the lock instead of failing with SQLITE_BUSY; when the lock outlasts the
  timeout the `OperationalError` surfaces instead of silently dropping the
  write.

### Invariants enforced by construction

- **No deletion / immutability**: the only code mutations are `INSERT INTO
  facts/sources` and `UPDATE facts SET archived_at`; triggers abort anything
  else, including an external writer. Tests assert row counts never shrink and
  that raw `DELETE`/content `UPDATE` fail.
- **Scope**: `supersedes` validates the target exists, is active, and belongs
  to the same resolved project, all inside the same write transaction as the
  insert — a failed supersede stores nothing.
- **Archive is the only retirement**: `recall_facts` (`archived_at IS NULL`)
  and `scour_archives` (`archived_at IS NOT NULL`) are complementary; double
  archive is an error; `recall_facts(id=)` on an archived fact is an error.

### Semantics decisions (spec gaps closed for tests)

- Timestamps: `datetime.now(timezone.utc).isoformat(timespec="milliseconds")`
  — ISO-8601 UTC, lexicographic == chronological — read **inside** the write
  transaction, so a writer that waited on the lock does not store a timestamp
  older than a writer that committed first.
- Ordering: recall no-query → `created_at DESC, id DESC` (newest first);
  recall with query + FTS → `rank` (relevance), ties newest id first;
  recall with query + LIKE → newest first (no ranking possible); scour →
  `archived_at DESC, id DESC` (most recently archived first) **with or
  without a query** — a scour query filters, it does not rank.
- `limit` default 50, must be a positive int, applied after tag filtering.
  When no tag filter is active it is pushed into the SQL `LIMIT` so recall
  does not materialize (and JSON-decode the tags of) the whole project.
- `id`/`supersedes` accept int, integral float/Decimal, or digit strings from
  untyped callers; positive only and at most `2**63 - 1` (SQLite's INTEGER
  bound, so larger ids are a `FactError`, never an `OverflowError` from the
  driver); bools and non-integral numbers rejected (`int(1.9)` would otherwise
  truncate onto a different fact). `get_fact` validates too — it is the audit
  accessor and must not alias or crash.
- `archive_fact(id=)` on an active fact is an error; `scour_archives(id=)` on a
  non-archived fact is an error — id lookups fail loudly on the wrong side of
  the archive flag. Filters compose with `id` on **both** surfaces (store and
  MCP): a valid id that no filter matches yields an empty result, never a
  silently filter-ignoring lookup.
- Input normalization: project/kind/text/source/note/query and provenance
  agent/session are stripped; empty agent → store default, empty session →
  absent; kind lowercased and validated against the closed taxonomy on every
  path (an empty or non-string `kind` filter is an error, not a silent
  no-filter); tags must be a list/tuple of strings, deduped + sorted;
  confidence coerced to float and bounds-checked (NaN/inf/overflow rejected,
  boundaries inclusive); `min_confidence` filter is inclusive
  (`confidence >= ?`) and `0.0` is a real floor, not "unset". Project
  resolution rejects non-string params with a `FactError`.
- Fact text is capped at `MAX_FACT_CHARS` (16,384 characters) so an agent
  cannot turn the store into a transcript dump; the error says so.
- Result shapes (MCP): every tool declares a dataclass output model, so the
  server advertises `outputSchema` and returns `structuredContent` alongside
  the JSON text. Envelopes: `store_fact`/`archive_fact` → `{"fact": full}`;
  `recall`/`scour` → `{"facts": [...]}` (summary rows, or exactly one full
  record when `id=`); `list_projects` →
  `{"projects": [{project, active, archived, top_tags, latest_activity}]}`
  sorted by project name; top tags = top 5 by count (desc), ties by name,
  counted across active + archived facts; latest activity = max of
  created_at/archived_at. Optional record fields (`archived_at`,
  `supersedes`, `sources`, source `session`/`note`/`pointer`) are null in the
  typed shape when absent; the store-layer dicts omit them.
- Errors: `FactError` → MCP **tool error** (`isError: true`) whose message is
  the actionable reason (the SDK prefixes the tool name); unexpected
  exceptions still surface as tool errors. Startup config failure prints the
  reason to stderr and exits non-zero instead of dying in a traceback.
- Provenance agent: MCP `clientInfo.name` when a request context exists →
  `STORE_N_RECALL_AGENT` → `unknown`. Session: taken from the connection when
  the SDK exposes one; null on stdio (session is optional in the model, so
  this degrades gracefully and is recorded as absent, never fabricated).
  Identity extraction failures are debug-logged rather than silent.
- Tool annotations: recall/scour/list are `readOnlyHint`; `store_fact` is a
  non-destructive write; `archive_fact` is destructive (it retires a fact).
- Server info advertises `version` from `store_n_recall.__version__`.
- DB path: `STORE_N_RECALL_DB` (expanded) → `$XDG_DATA_HOME/store-n-recall/
  facts.db` → `~/.local/share/store-n-recall/facts.db`; empty XDG value = unset.

## Concurrency

WAL allows one writer at a time; `BEGIN IMMEDIATE` + 5 s busy timeout queue
waiting writers. Covered by a real multi-process test (subprocess workers,
each with its own `Store`, burst-storing into one file). `Store` holds no
connection between operations, so threads may share one instance — the MCP
server runs sync tools on worker threads (`anyio.to_thread`) and relies on
exactly that. Nothing shares a single sqlite3 connection across threads.

## Release plan

1. Scaffold package + config + store + server.               [done]
2. Docs: BRIEF/IMPLEMENTATION, README status + config table. [done]
3. Tests: config, store (edge cases + invariants), concurrency (real
   processes), server (end-to-end over MCPServer).           [done]
4. Verify: full offline suite, compileall, import smoke,
   stdio server starts and registers tools with no store file. [done]
5. Hardening pass: schema version + migration, database guards, complete FTS
   readiness (partial-index backfill), id/fact-length hardening, provenance
   normalization, supersede links, SQL `LIMIT`, in-transaction record build,
   cached schema init. [done]
6. Tool contract: typed structured results, uniform envelopes, filters
   composed with `id`, tool errors, tool annotations, startup diagnostics.
   [done]
7. Commit: conventional, one logical change per commit.
