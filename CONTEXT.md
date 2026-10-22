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

### Classification

**Kind**:
The closed taxonomy classifying a fact: `decision`, `gotcha`, `convention`, or `fact` (the fallback when nothing more specific holds).
_Avoid_: category, type, tag

**Tag**:
A free-form label on a fact, usable as an optional recall filter. Tags narrow recall; kind classifies the fact itself.
_Avoid_: label, keyword

### Provenance

**Agent**:
The reasoning process that stores and recalls facts, identified by name for provenance.
_Avoid_: user, model, client

**Session**:
One run of an agent. Provenance only — never a recall scope, because the point of the store is knowledge that crosses sessions.
_Avoid_: conversation, run

### Lifecycle

**Recall**:
Fetching facts from the store for use in the current turn — by project, query, kind, tag, or id.
_Avoid_: search, query (the request itself), lookup

**Supersede**:
Replacing a stale fact with a corrected one in a single act — the old fact is retired, never edited in place.
_Avoid_: update, edit

**Forget**:
Removing a fact from the store permanently.
_Avoid_: delete, purge

**Stale**:
A fact that no longer holds for its project. Stale facts are superseded or forgotten promptly, never left standing to contradict fresh ones.
_Avoid_: outdated, expired
