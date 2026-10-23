# AGENTS.md — store-n-recall

## Project

MCP server giving agents a persistent, lightweight knowledgebase: store and recall
facts about projects, so knowledge survives between agents and sessions. Third in
the collection (look-n-see, scrape-n-search). **Status: implemented** — the server in `src/` matches the design in
`README.md`; requirements in `BRIEF.md`, design decisions in `IMPLEMENTATION.md`.

## Conventions

- Use `CONTEXT.md` terms exactly. Fact, project, store, recall, supersede,
  archive, scour, stale are not synonyms for anything else.
- Python 3.10+. No Node/Cargo toolchains on this workstation.
- Dependency-light: `mcp>=2` only — parity with look-n-see's `pyproject.toml`.
  Storage is stdlib `sqlite3` (WAL + busy timeout). Tests use stdlib `unittest`,
  zero dev deps.
- setuptools src layout: package `store_n_recall`, console script
  `store-n-recall` (stdio transport), module `store_n_recall.server:main`.
- Facts are immutable and never hard-deleted — no code path may remove a row.
  Corrections go through supersede (new fact stored + old archived in one act);
  the only retirement is `archive_fact`, and archived rows stay forever.
- Project scoping: tool `project` param wins, `STORE_N_RECALL_PROJECT` env
  fallback, else a clear error. Never derive scope from server cwd — the MCP
  process cwd is not the agent's repo.
- Every tool returns structured output the calling model can act on (JSON where
  it matters, not prose-only).
- Tool surface stays minimal (`store_fact`, `recall_facts`, `archive_fact`,
  `scour_archives`, `list_projects`). A new tool needs a row in README and a
  reason it cannot be a recall or scour filter; a new kind, default tag, or env
  var needs a README update in the same change.
- Default tags (`repo`, `project`, `feature`, `build`, `test`, `deps`, `docs`,
  `infra`) are a convention, not a schema constraint — free-form tags are valid.
  `tags` filters combine with AND: several tags = granular, one tag = broad.
- Fully offline: no network calls, no secrets, no embeddings. Config via
  environment variables with sane defaults (`STORE_N_RECALL_DB`,
  `STORE_N_RECALL_PROJECT`).

## Workflow

- The design in `README.md` is the spec. When implementation starts, create
  `BRIEF.md` (requirements) and `IMPLEMENTATION.md` (design + release plan) like
  look-n-see, and keep them in sync with decisions made during implementation.
- One commit per logical change; conventional commits, imperative subject.
  Docs-only changes say so.

## Verification

- `python -m compileall` / import smoke test after changes.
- `python -m unittest discover -s tests -q` — the offline suite.
- Server must start and register tools with no store file present (created on
  demand) and must never require network.
- Concurrency: two server processes writing the same store concurrently must not
  corrupt it (WAL + busy timeout) — cover with a test before calling storage done.
- Archive behavior: archived facts are invisible to `recall_facts` and visible
  only via `scour_archives`; storage tests must show no code path deletes rows.
