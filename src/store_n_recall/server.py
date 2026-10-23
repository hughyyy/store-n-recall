"""MCP server exposing the store_fact / recall_facts / archive_fact /
scour_archives / list_projects tools over stdio."""

from __future__ import annotations

import json

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.context import Context

from . import config as config_mod
from .config import Config, FactError
from .store import Store


def _identity(ctx: Context | None) -> tuple[str | None, str | None]:
    """Agent (MCP clientInfo.name) and session id from the request context.

    Falls back to None both when no context is in flight (e.g. tests) and when
    the SDK does not expose them; the store layer then uses its defaults.
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
        pass
    return agent, session


def _call(fn) -> str:
    """Run a store call; turn user-facing errors into model-actionable text."""
    try:
        return json.dumps(fn(), indent=2)
    except FactError as exc:
        return f"error: {exc}"


def _recall_by_id(store: Store, project: str, fact_id: int, archived: bool) -> str:
    """id= lookup: full record, but the recall path must stay blind to
    archived facts and the scour path to active ones (Store enforces)."""
    if archived:
        store.scour_archives(project, fact_id=fact_id, limit=1)
    else:
        store.recall_facts(project, fact_id=fact_id, limit=1)
    return {"fact": store.get_fact(fact_id)}


def create_server(
    cfg: Config | None = None,
    store: Store | None = None,
) -> MCPServer:
    cfg = cfg or config_mod.load_config()
    store = store or Store(cfg.db_path, default_agent=cfg.agent)
    mcp = MCPServer(name="store-n-recall")

    @mcp.tool()
    def store_fact(
        fact: str,
        project: str | None = None,
        kind: str | None = None,
        tags: list[str] | None = None,
        confidence: float | None = None,
        source: str | None = None,
        supersedes: int | None = None,
        ctx: Context | None = None,
    ) -> str:
        """Store a durable fact about a project. `supersedes` archives the
        given fact id in the same act (retire a stale fact with its
        correction). Returns the stored fact as JSON, provenance included."""
        agent, session = _identity(ctx)
        return _call(
            lambda: store.store_fact(
                cfg.resolve_project(project), fact,
                kind=kind,
                tags=tags,
                confidence=confidence,
                source=source,
                supersedes=supersedes,
                agent=agent,
                session=session,
            )
        )

    @mcp.tool()
    def recall_facts(
        project: str | None = None,
        id: int | None = None,
        query: str | None = None,
        kind: str | None = None,
        tags: list[str] | None = None,
        min_confidence: float | None = None,
        limit: int | None = None,
    ) -> str:
        """Recall active facts for a project. With `query`, relevance-ranked
        full-text match; otherwise newest first. `id` returns one full record
        including the complete source list. Returns {"facts": [...]}."""
        if id is not None:
            return _call(
                lambda: _recall_by_id(store, cfg.resolve_project(project), id, archived=False)
            )
        return _call(
            lambda: {
                "facts": store.recall_facts(
                    cfg.resolve_project(project),
                    query=query,
                    kind=kind,
                    tags=tags,
                    min_confidence=min_confidence,
                    limit=limit,
                )
            }
        )

    @mcp.tool()
    def archive_fact(id: int, note: str | None = None, ctx: Context | None = None) -> str:
        """Archive a fact by id: hidden from recall from then on, kept forever
        and reachable via scour_archives. Returns the archived fact as JSON."""
        agent, session = _identity(ctx)
        return _call(lambda: store.archive_fact(id, note=note, agent=agent, session=session))

    @mcp.tool()
    def scour_archives(
        project: str | None = None,
        id: int | None = None,
        query: str | None = None,
        kind: str | None = None,
        tags: list[str] | None = None,
        min_confidence: float | None = None,
        limit: int | None = None,
    ) -> str:
        """Scour archived facts only — same filters as recall_facts, most
        recently archived first. Returns {"facts": [...]}."""
        if id is not None:
            return _call(
                lambda: _recall_by_id(store, cfg.resolve_project(project), id, archived=True)
            )
        return _call(
            lambda: {
                "facts": store.scour_archives(
                    cfg.resolve_project(project),
                    query=query,
                    kind=kind,
                    tags=tags,
                    min_confidence=min_confidence,
                    limit=limit,
                )
            }
        )

    @mcp.tool()
    def list_projects() -> str:
        """List every project in the store with active/archived fact counts,
        top tags, and latest activity. Returns {"projects": [...]}."""
        return _call(lambda: {"projects": store.list_projects()})

    return mcp


def main() -> None:
    server = create_server()
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
