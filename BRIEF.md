# store-n-recall — Project Brief

## Purpose

An MCP server that gives agents a persistent, lightweight knowledgebase: store
and recall facts about projects, so knowledge survives between agents and
sessions. Agents re-derive the same facts every session (build commands,
gotchas, decisions, conventions); store-n-recall keeps one small store per
machine where any agent can persist facts under a project name and recall them
in one call. Third in the collection — look-n-see (vision/OCR), scrape-n-search
(web), store-n-recall (project memory).

## Requirements

### Tool surface (exactly five tools; a sixth needs a README row + a reason)

| Tool | Params | Returns |
| --- | --- | --- |
| `store_fact` | `project`?, `fact`* , `kind`?, `tags`?, `confidence`?, `source`?, `supersedes`? | stored fact as JSON |
| `recall_facts` | `project`?, `id`?, `query`?, `kind`?, `tags`?, `min_confidence`?, `limit`? | `{"facts": [...]}` (active only) |
| `archive_fact` | `id`* , `note`? | archived fact as JSON |
| `scour_archives` | same filters as `recall_facts` | `{"facts": [...]}` (archived only) |
| `list_projects` | — | per-project counts, top tags, latest activity |

### Scoping and provenance

- Project resolution: `project` param wins → `STORE_N_RECALL_PROJECT` env → a
  clear error. **No cwd-derived default** — the MCP process cwd is not the
  agent's repo.
- Facts never cross projects: every fact names exactly one project; recall
  filters by it; `supersedes` may only reference a fact in the same project.
- Provenance: every fact carries an append-only `sources` list — `stored` at
  creation, `archived` on archive — each event recording agent, session (when
  the transport has one), timestamp, and optional pointer/note. Agent comes
  from the MCP client's `clientInfo.name` with env fallback `STORE_N_RECALL_AGENT`.
- The full record (sources included) is fetched with `recall_facts(id=...)` /
  `scour_archives(id=...)`; list calls return summary rows.

### Fact model

- `id` server-assigned and stable; `project`; `kind` from the closed taxonomy
  `decision`/`gotcha`/`convention`/`fact` (default `fact`); `text` (required,
  non-empty); `tags` (free-form, any count; default tags are a convention, not
  a whitelist); `confidence` 0.0–1.0 (default 0.5); `created_at` UTC;
  `archived_at` UTC once archived.
- Facts are immutable and **never hard-deleted** — no code path may remove a
  row. Corrections go through `supersedes` (new fact stored + old archived in
  one act) or `archive_fact`.

### Recall semantics

- Active facts only (`recall_facts`); archived facts only, most recently
  archived first (`scour_archives`); archived records stay forever.
- `query` → relevance-ranked full-text match via FTS5 where the platform
  sqlite3 has it, with a LIKE fallback (newest-first) where it doesn't.
  Filters (`kind`, `tags` AND-combined, `min_confidence` inclusive) compose
  with each other and with `query`; `limit` caps the result (default 50).
- No query (`None`/empty) → newest-first.

### Storage

- One SQLite file, default `~/.local/share/store-n-recall/facts.db`
  (`STORE_N_RECALL_DB` overrides; honors `XDG_DATA_HOME`).
- WAL mode + busy timeout so parallel server processes share the file safely;
  test with real concurrent processes before calling storage done.
- Server starts and registers tools with no store file present (created on
  demand). Fully offline; no network, no secrets, no embeddings.

### Config (env vars; all optional)

| Variable | Default | Purpose |
| --- | --- | --- |
| `STORE_N_RECALL_DB` | `~/.local/share/store-n-recall/facts.db` | store path |
| `STORE_N_RECALL_PROJECT` | — | default project |
| `STORE_N_RECALL_AGENT` | `unknown` | provenance agent when MCP clientInfo absent |

### Verification

- `python -m compileall` / import smoke after changes.
- `python -m unittest discover -s tests -q` — offline suite.
- Server starts and registers tools with no store file; never requires network.

## Non-goals

- No embeddings/similarity ranking; no automatic capture or transcripts; no
  TTL garbage collection; no per-session scoping; no server-side aggregation
  beyond `list_projects`. Tools stay the five above — filters, not new tools,
  express new recall needs; storage is stdlib sqlite3 only (`mcp>=2` is the
  only dependency).
