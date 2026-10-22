# store-n-recall

A persistent knowledgebase that lets agents store and recall facts about projects, so knowledge survives between agents and sessions.

## Language

### Corpus

**Store**:
The persistent record of every fact for every project. One store per machine, shared by all agents and all sessions.
_Avoid_: database (implementation), knowledgebase (product category), memory

**Fact**:
One durable, self-contained statement about a project that an agent chose to persist — a decision, a gotcha, or a convention worth keeping.
_Avoid_: note, memory, entry, chunk

**Project**:
The scope a fact belongs to: one codebase. Every fact names exactly one project, and facts never cross projects.
_Avoid_: repo (the thing a project describes), namespace, workspace

### Fact metadata

**Kind**:
The closed taxonomy classifying a fact: `decision`, `gotcha`, `convention`, or `fact` (the fallback when nothing more specific holds).
_Avoid_: category, type, tag

**Tag**:
A label on a fact naming the area it concerns, usable as a recall filter. Tags narrow recall; kind classifies the fact itself.
_Avoid_: label, keyword

**Default tags**:
The starter tag set agents reach for first: `repo`, `project`, `feature`, `build`, `test`, `deps`, `docs`, `infra`. A convention, not a whitelist — free-form tags remain allowed.
_Avoid_: whitelist, taxonomy (reserved for kind)

**Confidence**:
The storing agent's stated certainty that the fact holds, from 0.0 to 1.0.
_Avoid_: score, weight, certainty

### Provenance

**Agent**:
The reasoning process that stores and recalls facts, identified by name for provenance.
_Avoid_: user, model, client

**Session**:
One run of an agent. Provenance only — never a recall scope, because the point of the store is knowledge that crosses sessions.
_Avoid_: conversation, run

**Source**:
One append-only provenance entry on a fact: which agent, which session, when, doing what (`stored` or `archived`), plus optionally a pointer — commit, file, URL — and a note. A fact's source list is its history.
_Avoid_: provenance (the quality, not the entry), citation

### Lifecycle

**Recall**:
Fetching active facts from the store for use in the current turn — by project, id, query, kind, tag, confidence floor, or combinations of these.
_Avoid_: search, query (the request itself), lookup

**Scour**:
Recall over archived facts only.
_Avoid_: dig, trawl, rummage

**Supersede**:
Replacing a stale fact with a corrected one in a single act — the new fact is stored and the old fact archived, never edited in place.
_Avoid_: update, edit

**Archive**:
Retiring a fact from active recall: hidden from recall, kept in the store forever, findable via scour. The only retirement — there is no hard delete.
_Avoid_: delete, forget, purge

**Stale**:
A fact that no longer holds for its project. Stale facts are superseded or archived promptly, never left standing to contradict fresh ones.
_Avoid_: outdated, expired
