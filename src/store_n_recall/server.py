"""MCP server exposing the store_fact / recall_facts / archive_fact /
scour_archives / list_projects tools over stdio.

Result contract:
- every tool declares a structured output model (advertised as `outputSchema`
  and returned as `structuredContent`, with the same JSON as text);
- `store_fact` / `archive_fact` return the full record in `{"fact": ...}`;
- `recall_facts` / `scour_archives` always return `{"facts": [...]}` — a
  summary list, or exactly one full record when `id` is given;
- `list_projects` returns `{"projects": [...]}`;
- invalid input raises a tool error (`isError: true`) with a reason the calling
  model can act on, rather than a prose string inside a successful result.
"""

# Annotations are real objects here (no `from __future__ import annotations`):
# MCPServer resolves dataclass return annotations with `get_type_hints`, which
# would otherwise re-resolve strings against sys.modules["__main__"] when the
# module is executed by runpy (`python -m store_n_recall.server`).
import logging
import sys
from dataclasses import dataclass
from typing import Any, Callable, TypeVar

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.context import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from . import __version__
from . import config as config_mod
from .config import Config, FactError
from .store import Store

_log = logging.getLogger(__name__)
T = TypeVar("T")

_READ_ONLY = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True,
    open_world_hint=False,
)
_WRITE = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=False,
    open_world_hint=False,
)
_RETIRE = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=False,
    open_world_hint=False,
)


# ---------------------------------------------------------------------------
# Result models (also the advertised output schemas)
# ---------------------------------------------------------------------------

@dataclass
class SourceOut:
    """One provenance entry: which agent, when, doing what, plus context."""

    action: str
    agent: str
    at: str
    session: str | None = None
    note: str | None = None
    pointer: str | None = None


@dataclass
class FactOut:
    """A fact. `sources` is populated on full records, null on summary rows."""

    id: int
    project: str
    kind: str
    text: str
    tags: list[str]
    confidence: float
    created_at: str
    archived_at: str | None = None
    supersedes: int | None = None
    sources: list[SourceOut] | None = None


@dataclass
class FactResult:
    fact: FactOut


@dataclass
class FactList:
    facts: list[FactOut]


@dataclass
class ProjectOut:
    project: str
    active: int
    archived: int
    top_tags: list[str]
    latest_activity: str


@dataclass
class ProjectList:
    projects: list[ProjectOut]


def _fact_out(record: dict[str, Any]) -> FactOut:
    sources = record.get("sources")
    return FactOut(
        id=record["id"],
        project=record["project"],
        kind=record["kind"],
        text=record["text"],
        tags=list(record["tags"]),
        confidence=record["confidence"],
        created_at=record["created_at"],
        archived_at=record.get("archived_at"),
        supersedes=record.get("supersedes"),
        sources=(
            [SourceOut(**entry) for entry in sources]
            if sources is not None else None
        ),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _identity(ctx: Context | None) -> tuple[str | None, str | None]:
    """Agent (MCP clientInfo.name) and session id from the request context.

    Falls back to None both when no context is in flight (e.g. tests) and when
    the SDK does not expose them; the store layer then uses its defaults.
    Debug-logged rather than silent, because lost identity degrades provenance.
    """
    if ctx is None:
        return None, None
    agent: str | None = None
    session: str | None = None
    try:
        session_obj = ctx.session  # raises ValueError outside a request
        params = getattr(session_obj, "client_params", None)
        client_info = getattr(params, "client_info", None) or getattr(params, "clientInfo", None)
        name = getattr(client_info, "name", None)
        agent = str(name) if name else None
        connection = getattr(session_obj, "_connection", None)
        session = getattr(connection, "session_id", None)
    except Exception:
        _log.debug("no MCP request identity available", exc_info=True)
    return agent, session


def _run(fn: Callable[[], T]) -> T:
    """Run a store call, turning user-facing errors into tool errors."""
    try:
        return fn()
    except FactError as exc:
        raise ToolError(str(exc)) from exc


def create_server(
    cfg: Config | None = None,
    store: Store | None = None,
) -> MCPServer:
    cfg = cfg or config_mod.load_config()
    store = store or Store(cfg.db_path, default_agent=cfg.agent)
    mcp = MCPServer(name="store-n-recall", version=__version__)

    @mcp.tool(structured_output=True, annotations=_WRITE)
    def store_fact(
        fact: str,
        project: str | None = None,
        kind: str | None = None,
        tags: list[str] | None = None,
        confidence: float | None = None,
        source: str | None = None,
        supersedes: int | None = None,
        ctx: Context | None = None,
    ) -> FactResult:
        """Store a durable fact about a project. `supersedes` archives the
        given fact id in the same act (retire a stale fact with its
        correction) and records the link in both provenance trails. Returns
        the stored fact as {"fact": {...}}, provenance included."""
        agent, session = _identity(ctx)
        return _run(lambda: FactResult(fact=_fact_out(store.store_fact(
            cfg.resolve_project(project), fact,
            kind=kind,
            tags=tags,
            confidence=confidence,
            source=source,
            supersedes=supersedes,
            agent=agent,
            session=session,
        ))))

    @mcp.tool(structured_output=True, annotations=_READ_ONLY)
    def recall_facts(
        project: str | None = None,
        id: int | None = None,
        query: str | None = None,
        kind: str | None = None,
        tags: list[str] | None = None,
        min_confidence: float | None = None,
        limit: int | None = None,
    ) -> FactList:
        """Recall active facts for a project. With `query`, relevance-ranked
        full-text match; otherwise newest first. Filters compose with every
        lookup, including `id`. With `id`, the result holds exactly one full
        record (sources included) — or no facts at all if a filter excludes
        it; an unknown or archived id is an error. Returns {"facts": [...]}."""
        project = _run(lambda: cfg.resolve_project(project))
        if id is not None:
            return _run(lambda: FactList(facts=_by_id(
                store, project, id, archived=False,
                query=query, kind=kind, tags=tags,
                min_confidence=min_confidence, limit=limit,
            )))
        return _run(lambda: FactList(facts=[
            _fact_out(row) for row in store.recall_facts(
                project,
                query=query,
                kind=kind,
                tags=tags,
                min_confidence=min_confidence,
                limit=limit,
            )
        ]))

    @mcp.tool(structured_output=True, annotations=_RETIRE)
    def archive_fact(id: int, note: str | None = None, ctx: Context | None = None) -> FactResult:
        """Archive a fact by id: hidden from recall from then on, kept forever
        and reachable via scour_archives. Returns the archived fact as
        {"fact": {...}}."""
        agent, session = _identity(ctx)
        return _run(lambda: FactResult(fact=_fact_out(
            store.archive_fact(id, note=note, agent=agent, session=session)
        )))

    @mcp.tool(structured_output=True, annotations=_READ_ONLY)
    def scour_archives(
        project: str | None = None,
        id: int | None = None,
        query: str | None = None,
        kind: str | None = None,
        tags: list[str] | None = None,
        min_confidence: float | None = None,
        limit: int | None = None,
    ) -> FactList:
        """Scour archived facts only — same filters as recall_facts, most
        recently archived first. With `id`, the result holds exactly one full
        record (sources included) — or no facts at all if a filter excludes
        it; an unknown or active id is an error. Returns {"facts": [...]}."""
        project = _run(lambda: cfg.resolve_project(project))
        if id is not None:
            return _run(lambda: FactList(facts=_by_id(
                store, project, id, archived=True,
                query=query, kind=kind, tags=tags,
                min_confidence=min_confidence, limit=limit,
            )))
        return _run(lambda: FactList(facts=[
            _fact_out(row) for row in store.scour_archives(
                project,
                query=query,
                kind=kind,
                tags=tags,
                min_confidence=min_confidence,
                limit=limit,
            )
        ]))

    @mcp.tool(structured_output=True, annotations=_READ_ONLY)
    def list_projects() -> ProjectList:
        """List every project in the store with active/archived fact counts,
        top tags, and latest activity. Returns {"projects": [...]}."""
        return _run(lambda: ProjectList(projects=[
            ProjectOut(**row) for row in store.list_projects()
        ]))

    return mcp


def _by_id(
    store: Store,
    project: str,
    fact_id: int,
    *,
    archived: bool,
    query: str | None,
    kind: str | None,
    tags: list[str] | None,
    min_confidence: float | None,
    limit: int | None,
) -> list[FactOut]:
    """`id` lookup with the full filter set applied.

    The list call validates every filter as usual and raises for an id that is
    unknown or on the wrong side of the archive flag; an empty result means a
    filter excluded a valid record. The record is then fetched in full.
    """
    lookup = store.scour_archives if archived else store.recall_facts
    matched = lookup(
        project, fact_id=fact_id, query=query, kind=kind, tags=tags,
        min_confidence=min_confidence, limit=limit,
    )
    return [_fact_out(store.get_fact(fact_id))] if matched else []


def main() -> None:
    try:
        server = create_server()
    except FactError as exc:
        # A misconfigured server must not die with a traceback inside an MCP
        # client: say what to fix and exit non-zero.
        print(f"store-n-recall: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
