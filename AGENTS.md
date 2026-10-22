# AGENTS.md — store-n-recall

## Project

MCP server giving agents a persistent, lightweight knowledgebase: store and recall
facts about projects, so knowledge survives between agents and sessions. Third in
the collection (look-n-see, scrape-n-search). **Status: design captured,
implementation not started** — the design lives in `README.md` (planned tool
surface, storage, config) and `CONTEXT.md` (vocabulary).

## Conventions

- Use `CONTEXT.md` terms exactly. Fact, project, store, recall, supersede,
  forget, stale are not synonyms for anything else.
- Python 3.10+. No Node/Cargo toolchains on this workstation.
- Dependency-light: `mcp>=2` only — parity with look-n-see's `pyproject.toml`.
  Storage is stdlib `sqlite3` (WAL + busy timeout). Tests use stdlib `unittest`,
  zero dev deps.
- setuptools src layout: package `store_n_recall`, console script
  `store-n-recall` (stdio transport), module `store_n_recall.server:main`.
- Facts are immutable. Corrections go through supersede (new fact + old retired
  in one act), removals through forget; no in-place edits, no history rewrite.
- Project scoping: tool `project` param wins, `STORE_N_RECALL_PROJECT` env
  fallback, else a clear error. Never derive scope from server cwd — the MCP
  process cwd is not the agent's repo.
- Every tool returns structured output the calling model can act on (JSON where
  it matters, not prose-only).
- Tool surface stays minimal (`store_fact`, `recall_facts`, `forget_fact`,
  `list_projects`). A new tool needs a row in README and a reason it cannot be a
  recall filter; a new kind or env var needs a README update in the same change.
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
