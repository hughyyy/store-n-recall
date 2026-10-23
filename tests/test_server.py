"""Server tests: tool registration, end-to-end behavior over the real
MCPServer call path, structured results, project resolution, error contract,
provenance defaults, and the stdio transport."""

import asyncio
import io
import json
import os
import runpy
import sys
import tempfile
import unittest
import warnings
from unittest import mock

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import Implementation

from store_n_recall import __version__
from store_n_recall.config import Config, FactError
from store_n_recall.server import _identity, create_server, main as server_main
from store_n_recall.store import KINDS, Store

TOOLS = {"store_fact", "recall_facts", "archive_fact", "scour_archives", "list_projects"}


def run_module_as_main(name: str) -> None:
    """Re-execute a module as __main__, covering its entry guard.

    runpy warns because the module is already imported; that is the point of
    this helper, so the warning is expected noise.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        runpy.run_module(name, run_name="__main__")


def call(mcp, name, arguments):
    """Call a tool expected to succeed; return the parsed JSON text."""
    result = asyncio.run(mcp.call_tool(name, arguments))
    assert not result.is_error, f"tool {name} failed: {result}"
    return json.loads(result.content[0].text)

def call_error(mcp, name, arguments) -> str:
    """Call a tool expected to fail; return the tool-error text.

    `MCPServer.call_tool` raises ToolError for a failed call (the low-level
    server turns it into `isError` on the wire), so this catches it.
    """
    try:
        result = asyncio.run(mcp.call_tool(name, arguments))
    except ToolError as exc:
        return str(exc)
    raise AssertionError(f"tool {name} should have failed: {result}")


def tool_names(mcp) -> set[str]:
    return {t.name for t in asyncio.run(mcp.list_tools())}


class ServerTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "facts.db")
        self.cfg = Config(db_path=self.db)
        self.store = Store(self.db)
        self.mcp = create_server(cfg=self.cfg, store=self.store)

    def store_one(self, **extra):
        args = {"project": "proj", "fact": "The build needs --offline"}
        args.update(extra)
        return call(self.mcp, "store_fact", args)


class RegistrationTests(ServerTestCase):
    def test_registers_exactly_the_five_tools(self):
        self.assertEqual(tool_names(self.mcp), TOOLS)

    def test_server_starts_with_no_store_file_created_on_demand(self):
        self.assertFalse(os.path.exists(self.db))
        self.store_one()
        self.assertTrue(os.path.exists(self.db))


class EndToEndTests(ServerTestCase):
    def test_full_lifecycle_via_mcp(self):
        stored = self.store_one(tags=["build"], kind="gotcha", source="docs/1.md")
        fact = stored["fact"]
        self.assertEqual(fact["kind"], "gotcha")
        self.assertEqual(fact["sources"][0]["pointer"], "docs/1.md")

        recalled = call(self.mcp, "recall_facts", {"project": "proj"})
        self.assertEqual([f["id"] for f in recalled["facts"]], [fact["id"]])
        self.assertIsNone(recalled["facts"][0]["sources"])  # summary row

        full = call(self.mcp, "recall_facts", {"project": "proj", "id": fact["id"]})
        self.assertEqual([f["id"] for f in full["facts"]], [fact["id"]])
        self.assertTrue(full["facts"][0]["sources"])  # full record by id

        archived = call(self.mcp, "archive_fact", {"id": fact["id"], "note": "stale"})
        self.assertIsNotNone(archived["fact"]["archived_at"])
        self.assertEqual(archived["fact"]["sources"][-1]["note"], "stale")

        recalled_after = call(self.mcp, "recall_facts", {"project": "proj"})
        self.assertEqual(recalled_after["facts"], [])

        scoured = call(self.mcp, "scour_archives", {"project": "proj"})
        self.assertEqual([f["id"] for f in scoured["facts"]], [fact["id"]])

        # recall by id is blind to archived facts; scour by id sees them
        text = call_error(self.mcp, "recall_facts", {"project": "proj", "id": fact["id"]})
        self.assertIn("no active fact", text)
        scoured_full = call(self.mcp, "scour_archives", {"project": "proj", "id": fact["id"]})
        self.assertEqual(
            [f["id"] for f in scoured_full["facts"]], [fact["id"]])
        self.assertTrue(scoured_full["facts"][0]["sources"])

        projects = call(self.mcp, "list_projects", {})["projects"]
        self.assertEqual(len(projects), 1)
        self.assertEqual(projects[0]["project"], "proj")
        self.assertEqual((projects[0]["active"], projects[0]["archived"]), (0, 1))
        self.assertEqual(projects[0]["top_tags"], ["build"])

    def test_supersede_via_mcp(self):
        old = self.store_one()["fact"]
        new = call(
            self.mcp, "store_fact",
            {"project": "proj", "fact": "corrected", "supersedes": old["id"]},
        )["fact"]
        self.assertEqual(new["supersedes"], old["id"])
        self.assertEqual(new["sources"][0]["note"], f"supersedes fact {old['id']}")
        active = call(self.mcp, "recall_facts", {"project": "proj"})["facts"]
        self.assertEqual([f["id"] for f in active], [new["id"]])
        # old is archived: visible via scour full record, linked both ways
        scoured = call(self.mcp, "scour_archives", {"project": "proj", "id": old["id"]})
        event = scoured["facts"][0]["sources"][-1]
        self.assertEqual(event["action"], "archived")
        self.assertEqual(event["note"], f"superseded by fact {new['id']}")

    def test_all_results_parse_as_json(self):
        out = [
            self.store_one(),
            call(self.mcp, "recall_facts", {"project": "proj"}),
            call(self.mcp, "archive_fact", {"id": self.store_one()["fact"]["id"]}),
            call(self.mcp, "scour_archives", {"project": "proj"}),
            call(self.mcp, "list_projects", {}),
        ]
        for item in out:
            json.dumps(item)  # round-trips


class StructuredOutputTests(ServerTestCase):
    """Every tool advertises an output schema and returns the same JSON as
    text and as structuredContent, in a consistent envelope."""

    def test_every_tool_declares_an_output_schema(self):
        tools = {t.name: t for t in asyncio.run(self.mcp.list_tools())}
        for name, tool in tools.items():
            with self.subTest(tool=name):
                self.assertIsNotNone(tool.output_schema)
                self.assertEqual(tool.output_schema.get("type"), "object")

    def test_structured_content_matches_text(self):
        self.store_one()
        for name, args in (
            ("store_fact", {"project": "proj", "fact": "another"}),
            ("recall_facts", {"project": "proj"}),
            ("list_projects", {}),
        ):
            with self.subTest(tool=name):
                result = asyncio.run(self.mcp.call_tool(name, args))
                self.assertFalse(result.is_error)
                self.assertEqual(
                    json.loads(result.content[0].text), result.structured_content)

    def test_envelopes_are_uniform(self):
        fact = self.store_one()["fact"]
        self.assertEqual(
            set(call(self.mcp, "recall_facts", {"project": "proj", "id": fact["id"]})),
            {"facts"})
        self.assertEqual(
            set(call(self.mcp, "scour_archives", {"project": "proj"})), {"facts"})
        self.assertEqual(
            set(call(self.mcp, "archive_fact", {"id": fact["id"]})), {"fact"})
        self.assertEqual(set(call(self.mcp, "list_projects", {})), {"projects"})

    def test_fact_fields_are_typed_even_when_absent(self):
        fid = self.store_one()["fact"]["id"]
        fact = call(self.mcp, "recall_facts",
                    {"project": "proj", "id": fid})["facts"][0]
        self.assertIn("archived_at", fact)
        self.assertIn("supersedes", fact)
        self.assertIsNone(fact["archived_at"])
        self.assertIsNone(fact["supersedes"])


class ProjectResolutionTests(ServerTestCase):
    def test_param_wins_over_env_default(self):
        mcp = create_server(cfg=Config(db_path=self.db, default_project="env-proj"))
        fact = call(mcp, "store_fact", {"project": "explicit", "fact": "x"})["fact"]
        self.assertEqual(fact["project"], "explicit")

    def test_env_default_used_when_param_absent(self):
        mcp = create_server(cfg=Config(db_path=self.db, default_project="env-proj"))
        fact = call(mcp, "store_fact", {"fact": "x"})["fact"]
        self.assertEqual(fact["project"], "env-proj")

    def test_no_project_is_a_clear_error(self):
        text = call_error(self.mcp, "store_fact", {"fact": "x"})
        self.assertIn("project", text)
        self.assertIn("STORE_N_RECALL_PROJECT", text)
        text2 = call_error(self.mcp, "recall_facts", {})
        self.assertIn("project", text2)

    def test_project_error_is_a_tool_error_not_a_crash(self):
        text = call_error(self.mcp, "store_fact", {"fact": "x"})
        self.assertIn("project", text)  # retryable tool error, not a crash
        self.assertEqual(
            len(call(self.mcp, "recall_facts",
                     {"project": "proj"})["facts"]), 0)


class ValidationViaMcpTests(ServerTestCase):
    def test_validation_errors_are_actionable_tool_errors(self):
        for name, args in (
            ("store_fact", {"project": "p", "fact": ""}),
            ("store_fact", {"project": "p", "fact": "x", "kind": "note"}),
            ("store_fact", {"project": "p", "fact": "x", "confidence": 3.0}),
            ("store_fact", {"project": "p", "fact": "x", "supersedes": 4242}),
            ("archive_fact", {"id": 4242}),
            ("archive_fact", {"id": 4242, "note": "   "}),
            ("recall_facts", {"project": "p", "limit": 0}),
            ("recall_facts", {"project": "p", "kind": "bogus"}),
            ("recall_facts", {"project": "p", "id": 0}),
            ("scour_archives", {"project": "p", "id": -1}),
            # NOTE: confidence=True is not here — the SDK coerces bool to
            # 1.0 at the schema layer before the store sees it; the store's
            # bool rejection is the direct-API contract (test_store).
            ("store_fact", {"project": "p", "fact": "x", "supersedes": True}),
            ("archive_fact", {"id": True}),
        ):
            text = call_error(self.mcp, name, args)
            self.assertIn("Error executing tool", text, f"{name}{args}")
            self.assertNotIn("Traceback", text)

    def test_schema_mismatches_fail_tool_call_not_server(self):
        # Garbage shapes fail pydantic validation at the SDK layer; the server
        # must report a tool error, never crash the process.
        for name, args in (
            ("store_fact", {"project": "p"}),  # fact required by the schema
            ("store_fact", {"project": "p", "fact": "x", "tags": "repo"}),
            ("store_fact", {"project": "p", "fact": "x", "kind": 5}),
            ("store_fact", {"project": "p", "fact": 42}),
            ("recall_facts", {"project": "p", "limit": 1.5}),
            ("recall_facts", {"project": "p", "min_confidence": {"x": 1}}),
            ("archive_fact", {"id": "not-a-number"}),
        ):
            with self.assertRaises(ToolError, msg=f"{name}{args}"):
                asyncio.run(self.mcp.call_tool(name, args))

    def test_digit_strings_coerce_to_numbers(self):
        fact = call(
            self.mcp, "store_fact",
            {"project": "p", "fact": "x", "confidence": "0.8", "kind": "gotcha"},
        )["fact"]
        self.assertEqual(fact["confidence"], 0.8)
        self.assertEqual(fact["kind"], "gotcha")
        fid = fact["id"]
        call(self.mcp, "store_fact",
             {"project": "p", "fact": "replacement", "supersedes": str(fid)})
        archived = call(self.mcp, "scour_archives", {"project": "p", "id": str(fid)})
        self.assertIsNotNone(archived["facts"][0]["archived_at"])

    def test_recall_by_id_narrows_with_query(self):
        fact = self.store_one()["fact"]
        matching = call(
            self.mcp, "recall_facts",
            {"project": "proj", "id": fact["id"], "query": "offline"})
        self.assertEqual([f["id"] for f in matching["facts"]], [fact["id"]])
        missing = call(
            self.mcp, "recall_facts",
            {"project": "proj", "id": fact["id"], "query": "zzz"})
        self.assertEqual(missing["facts"], [])

    def test_scour_by_id_active_fact_via_mcp_is_error(self):
        fact = self.store_one()["fact"]
        text = call_error(self.mcp, "scour_archives",
                          {"project": "proj", "id": fact["id"]})
        self.assertIn("no archived fact", text)

    def test_supersede_cross_project_via_mcp(self):
        old = call(self.mcp, "store_fact", {"project": "a", "fact": "x"})["fact"]
        text = call_error(
            self.mcp, "store_fact",
            {"project": "b", "fact": "y", "supersedes": old["id"]},
        )
        self.assertIn("belongs to project", text)


class ProvenanceTests(ServerTestCase):
    def test_agent_defaults_from_env_config(self):
        mcp = create_server(cfg=Config(db_path=self.db, agent="builder-42"))
        fact = call(mcp, "store_fact", {"project": "p", "fact": "x"})["fact"]
        self.assertEqual(fact["sources"][0]["agent"], "builder-42")

    def test_agent_unknown_when_nothing_configured(self):
        fact = self.store_one()["fact"]
        self.assertEqual(fact["sources"][0]["agent"], "unknown")


class _FakeCtx:
    """Duck-typed stand-in for the MCPServer request Context."""

    def __init__(self, session=None):
        self._session = session

    @property
    def session(self):
        return self._session


class _FakeClientInfo:
    def __init__(self, name):
        self.name = name


class _FakeParams:
    def __init__(self, client_info):
        self.client_info = client_info


class _FakeSession:
    def __init__(self, client_info=None, session_id=None):
        self.client_params = _FakeParams(client_info)
        self._client_params = self.client_params  # attribute used by some SDK builds
        self._connection = type("Conn", (), {"session_id": session_id})()


class IdentityTests(unittest.TestCase):
    def test_no_context_means_no_identity(self):
        self.assertEqual(_identity(None), (None, None))

    def test_client_info_name_extracted(self):
        ctx = _FakeCtx(_FakeSession(client_info=_FakeClientInfo("agent-7")))
        self.assertEqual(_identity(ctx), ("agent-7", None))

    def test_session_id_extracted_when_transport_has_one(self):
        ctx = _FakeCtx(_FakeSession(client_info=_FakeClientInfo("agent-7"), session_id="sess-abc"))
        self.assertEqual(_identity(ctx), ("agent-7", "sess-abc"))

    def test_session_id_without_client_info(self):
        ctx = _FakeCtx(_FakeSession(client_info=None, session_id="sess-abc"))
        self.assertEqual(_identity(ctx), (None, "sess-abc"))

    def test_missing_client_info_degrades_to_none(self):
        ctx = _FakeCtx(_FakeSession(client_info=None))
        self.assertEqual(_identity(ctx), (None, None))

    def test_identity_passthrough_to_stored_sources(self):
        # A real MCP request wires ctx into store_fact; simulate the wiring
        # (as the SDK does) and prove the agent lands in provenance.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = Store(os.path.join(tmp.name, "f.db"))
        agent, session = _identity(
            _FakeCtx(_FakeSession(client_info=_FakeClientInfo("agent-9")))
        )
        fact = store.store_fact("p", "x", agent=agent, session=session)
        self.assertEqual(fact["sources"][0]["agent"], "agent-9")


class StdioEndToEndTests(unittest.TestCase):
    """The real transport: spawn the installed server binary, drive it with an
    MCP client over stdio. Covers main(), startup with no store file, tool
    registration, the full lifecycle, provenance from clientInfo, structured
    results, and error strings over the wire."""

    def _run(self, coro):
        return asyncio.run(asyncio.wait_for(coro, timeout=90))

    def test_full_lifecycle_over_real_stdio(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "nested", "facts.db")
            env = dict(os.environ, STORE_N_RECALL_DB=db)
            env.pop("STORE_N_RECALL_PROJECT", None)

            async def lifecycle():
                params = StdioServerParameters(
                    command=sys.executable, args=["-m", "store_n_recall"], env=env)
                async with stdio_client(params) as (r, w):
                    async with ClientSession(
                        r, w, client_info=Implementation(name="e2e-agent", version="1")
                    ) as session:
                        init = await session.initialize()
                        self.assertEqual(init.server_info.name, "store-n-recall")
                        self.assertEqual(init.server_info.version, __version__)
                        tools = await session.list_tools()
                        names = {t.name for t in tools.tools}
                        self.assertEqual(names, TOOLS)

                        # advertised schema: fact is required, output typed
                        store_tool = next(
                            t for t in tools.tools if t.name == "store_fact")
                        self.assertEqual(
                            store_tool.input_schema.get("required", []), ["fact"],
                            "store_fact must advertise fact as required")
                        self.assertIsNotNone(store_tool.output_schema)

                        # no store file before first call
                        self.assertFalse(os.path.exists(db))

                        # error path over the wire: no project, no env default
                        res = await session.call_tool("store_fact", {"fact": "x"})
                        self.assertTrue(res.is_error)
                        self.assertIn("project", res.content[0].text)

                        # store -> provenance from clientInfo
                        res = await session.call_tool(
                            "store_fact",
                            {"project": "e2e", "fact": "the build is offline",
                             "tags": ["build"], "kind": "gotcha", "confidence": 0.9},
                        )
                        stored = json.loads(res.content[0].text)["fact"]
                        self.assertEqual(stored["sources"][0]["agent"], "e2e-agent")
                        self.assertEqual(stored["kind"], "gotcha")
                        self.assertTrue(os.path.exists(db))

                        # recall by query (FTS path over the wire)
                        res = await session.call_tool(
                            "recall_facts", {"project": "e2e", "query": "offline"})
                        recalled = json.loads(res.content[0].text)["facts"]
                        self.assertEqual([f["id"] for f in recalled], [stored["id"]])

                        # full record by id + supersede
                        res = await session.call_tool(
                            "store_fact",
                            {"project": "e2e", "fact": "the build is offline (corrected)",
                             "supersedes": stored["id"]},
                        )
                        replacement = json.loads(res.content[0].text)["fact"]
                        new_id = replacement["id"]
                        self.assertEqual(replacement["supersedes"], stored["id"])
                        res = await session.call_tool(
                            "recall_facts", {"project": "e2e", "id": stored["id"]})
                        self.assertTrue(res.is_error)

                        # archive + scour
                        res = await session.call_tool("archive_fact", {"id": new_id})
                        archived = json.loads(res.content[0].text)["fact"]
                        self.assertIsNotNone(archived["archived_at"])
                        res = await session.call_tool(
                            "scour_archives", {"project": "e2e"})
                        scoured = json.loads(res.content[0].text)["facts"]
                        # the superseded fact and the archived one, most recent first
                        self.assertEqual(
                            [f["id"] for f in scoured], [new_id, stored["id"]])

                        # list_projects aggregates
                        res = await session.call_tool("list_projects", {})
                        projects = json.loads(res.content[0].text)["projects"]
                        self.assertEqual(projects[0]["project"], "e2e")
                        self.assertEqual(
                            (projects[0]["active"], projects[0]["archived"]), (0, 2))
                        self.assertEqual(projects[0]["top_tags"], ["build"])

            self._run(lifecycle())

    def test_garbage_args_fail_tool_call_not_server_over_stdio(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, STORE_N_RECALL_DB=os.path.join(tmp, "f.db"),
                       STORE_N_RECALL_PROJECT="p")

            async def run():
                params = StdioServerParameters(
                    command=sys.executable, args=["-m", "store_n_recall"], env=env)
                async with stdio_client(params) as (r, w):
                    async with ClientSession(r, w) as session:
                        await session.initialize()
                        res = await session.call_tool(
                            "store_fact", {"fact": "x", "tags": "not-a-list"})
                        self.assertTrue(res.is_error)
                        self.assertIn("tags", res.content[0].text)
                        # server still alive and serving afterwards
                        res = await session.call_tool(
                            "store_fact", {"fact": "still works"})
                        self.assertFalse(res.is_error)
                        self.assertEqual(
                            json.loads(res.content[0].text)["fact"]["text"],
                            "still works")

            self._run(run())


class ToolSchemaTests(ServerTestCase):
    def test_advertised_schemas_match_the_tool_contract(self):
        tools = {t.name: t for t in asyncio.run(self.mcp.list_tools())}
        self.assertEqual(set(tools), TOOLS)
        required = {
            name: set(tool.input_schema.get("required", []))
            for name, tool in tools.items()
        }
        self.assertEqual(required, {
            "store_fact": {"fact"},
            "recall_facts": set(),
            "archive_fact": {"id"},
            "scour_archives": set(),
            "list_projects": set(),
        })
        for name, tool in tools.items():
            self.assertTrue(tool.description, f"{name} needs a description")
            self.assertIsNotNone(tool.output_schema, f"{name} needs an output schema")

    def test_annotations_mark_reads_and_writes(self):
        tools = {t.name: t for t in asyncio.run(self.mcp.list_tools())}
        for name in ("recall_facts", "scour_archives", "list_projects"):
            annotations = tools[name].annotations
            self.assertTrue(annotations.read_only_hint, name)
            self.assertFalse(annotations.destructive_hint, name)
        self.assertFalse(tools["store_fact"].annotations.read_only_hint)
        self.assertFalse(tools["store_fact"].annotations.destructive_hint)
        self.assertTrue(tools["archive_fact"].annotations.destructive_hint)

    def test_kind_error_lists_every_valid_kind(self):
        text = call_error(
            self.mcp, "store_fact", {"project": "proj", "fact": "x", "kind": "note"})
        self.assertIn("unknown kind", text)
        for kind in KINDS:
            self.assertIn(kind, text)


class FilterForwardingTests(ServerTestCase):
    def test_recall_forwards_kind_tags_and_confidence(self):
        target = self.store_one(fact="needle", kind="gotcha",
                                tags=["repo", "build"], confidence=0.9)["fact"]
        self.store_one(fact="needle", kind="fact", tags=["repo"], confidence=0.9)
        self.store_one(fact="needle", kind="gotcha", tags=["repo"], confidence=0.9)
        self.store_one(fact="needle", kind="gotcha",
                       tags=["repo", "build"], confidence=0.2)
        facts = call(self.mcp, "recall_facts", {
            "project": "proj", "kind": "gotcha", "tags": ["repo", "build"],
            "min_confidence": 0.8,
        })["facts"]
        self.assertEqual([f["id"] for f in facts], [target["id"]])

    def test_recall_forwards_query(self):
        hit = self.store_one(fact="the release build is offline")["fact"]
        self.store_one(fact="unrelated note")
        facts = call(self.mcp, "recall_facts",
                     {"project": "proj", "query": "offline"})["facts"]
        self.assertEqual([f["id"] for f in facts], [hit["id"]])

    def test_recall_forwards_limit(self):
        for i in range(3):
            self.store_one(fact=f"fact {i}")
        facts = call(self.mcp, "recall_facts",
                     {"project": "proj", "limit": 2})["facts"]
        self.assertEqual(len(facts), 2)

    def test_scour_forwards_kind_tags_and_limit(self):
        gotcha = self.store_one(fact="old build gotcha", kind="gotcha", tags=["repo"])["fact"]
        other = self.store_one(fact="old build fact", kind="fact", tags=["repo"])["fact"]
        call(self.mcp, "archive_fact", {"id": gotcha["id"]})
        call(self.mcp, "archive_fact", {"id": other["id"]})
        facts = call(self.mcp, "scour_archives", {
            "project": "proj", "kind": "gotcha", "tags": ["repo"]})["facts"]
        self.assertEqual([f["id"] for f in facts], [gotcha["id"]])
        self.assertEqual(
            len(call(self.mcp, "scour_archives",
                     {"project": "proj", "limit": 1})["facts"]),
            1,
        )


class ErrorPathTests(ServerTestCase):
    def test_second_archive_is_error_text(self):
        fid = self.store_one()["fact"]["id"]
        call(self.mcp, "archive_fact", {"id": fid})
        text = call_error(self.mcp, "archive_fact", {"id": fid})
        self.assertIn("already archived", text)

    def test_id_paths_require_a_project(self):
        fid = self.store_one()["fact"]["id"]
        for name in ("recall_facts", "scour_archives"):
            text = call_error(self.mcp, name, {"id": fid})
            self.assertIn("project", text, f"{name} -> {text}")

    def test_id_filters_narrow_and_are_validated(self):
        fid = self.store_one(kind="gotcha", tags=["build"])["fact"]["id"]
        matched = call(self.mcp, "recall_facts",
                       {"project": "proj", "id": fid, "kind": "gotcha"})
        self.assertEqual([f["id"] for f in matched["facts"]], [fid])
        narrowed = call(self.mcp, "recall_facts",
                        {"project": "proj", "id": fid, "kind": "decision"})
        self.assertEqual(narrowed["facts"], [])
        # invalid filters are never silently ignored just because id is set
        for bad in ({"kind": "bogus"}, {"limit": 0}, {"min_confidence": 5}):
            text = call_error(self.mcp, "recall_facts",
                              {"project": "proj", "id": fid, **bad})
            self.assertIn("Error executing tool", text, repr(bad))

    def test_unknown_id_messages_are_specific(self):
        recall_text = call_error(self.mcp, "recall_facts",
                                 {"project": "proj", "id": 777})
        self.assertIn("no active fact with id 777", recall_text)
        scour_text = call_error(self.mcp, "scour_archives",
                                {"project": "proj", "id": 777})
        self.assertIn("no archived fact with id 777", scour_text)

    def test_fractional_id_is_rejected_by_the_schema(self):
        fid = self.store_one()["fact"]["id"]
        with self.assertRaises(ToolError):
            asyncio.run(self.mcp.call_tool("archive_fact", {"id": fid + 0.9}))
        self.assertEqual(
            len(call(self.mcp, "recall_facts", {"project": "proj"})["facts"]), 1)


class _RaisingSessionCtx:
    @property
    def session(self):
        raise ValueError("no request in flight")


class _CamelParams:
    def __init__(self, client_info):
        self.clientInfo = client_info


class _CamelSession:
    def __init__(self, client_info, session_id=None):
        self.client_params = _CamelParams(client_info)
        self._connection = type("Conn", (), {"session_id": session_id})()


class _WrappedName:
    def __init__(self, raw):
        self.name = raw

    def __str__(self):
        return "agent-wrapped"


class IdentityEdgeCaseTests(unittest.TestCase):
    def test_context_without_a_request_degrades_to_none(self):
        self.assertEqual(_identity(_RaisingSessionCtx()), (None, None))

    def test_session_without_client_attributes_degrades_to_none(self):
        self.assertEqual(_identity(_FakeCtx(object())), (None, None))

    def test_camel_case_client_info_is_read(self):
        ctx = _FakeCtx(_CamelSession(_FakeClientInfo("camel-agent"), "s9"))
        self.assertEqual(_identity(ctx), ("camel-agent", "s9"))

    def test_non_string_client_name_is_stringified(self):
        ctx = _FakeCtx(_FakeSession(client_info=_WrappedName(123)))
        self.assertEqual(_identity(ctx)[0], "123")


class EntryPointTests(unittest.TestCase):
    def test_main_serves_stdio_without_touching_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "facts.db")
            with mock.patch.dict(os.environ, {"STORE_N_RECALL_DB": db}), \
                    mock.patch.object(MCPServer, "run") as run:
                server_main()
            run.assert_called_once_with(transport="stdio")
            self.assertFalse(os.path.exists(db))

    def test_main_reports_config_failure_and_exits(self):
        stderr = io.StringIO()
        with mock.patch("store_n_recall.server.config_mod.load_config",
                        side_effect=FactError("set STORE_N_RECALL_DB explicitly")), \
                mock.patch("sys.stderr", stderr):
            with self.assertRaises(SystemExit) as cm:
                server_main()
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("set STORE_N_RECALL_DB explicitly", stderr.getvalue())

    def test_module_entrypoint_calls_main(self):
        with mock.patch("store_n_recall.server.main") as entry:
            run_module_as_main("store_n_recall")
        entry.assert_called_once_with()

    def test_server_module_guard_runs_main(self):
        # `python -m store_n_recall.server` re-executes the module as __main__,
        # so patch the transport instead of the module-level main.
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "facts.db")
            with mock.patch.dict(os.environ, {"STORE_N_RECALL_DB": db}), \
                    mock.patch.object(MCPServer, "run") as run:
                run_module_as_main("store_n_recall.server")
            run.assert_called_once_with(transport="stdio")
            self.assertFalse(os.path.exists(db))

    def test_main_module_guard_is_inert_when_imported(self):
        import importlib
        module = importlib.import_module("store_n_recall.__main__")
        self.assertTrue(module.__name__)


class ListProjectsToolTests(ServerTestCase):
    def test_lists_every_project_sorted_with_counts_and_tags(self):
        call(self.mcp, "store_fact", {"project": "zeta", "fact": "z"})
        fid = call(self.mcp, "store_fact",
                   {"project": "alpha", "fact": "a", "tags": ["repo"]})["fact"]["id"]
        call(self.mcp, "archive_fact", {"id": fid})
        projects = call(self.mcp, "list_projects", {})["projects"]
        self.assertEqual([p["project"] for p in projects], ["alpha", "zeta"])
        self.assertEqual((projects[0]["active"], projects[0]["archived"]), (0, 1))
        self.assertEqual(projects[0]["top_tags"], ["repo"])
        self.assertEqual((projects[1]["active"], projects[1]["archived"]), (1, 0))


class _BrokenSession:
    @property
    def client_params(self):
        raise RuntimeError("transport detail unavailable")


class IdentityDegradationTests(unittest.TestCase):
    def test_client_info_without_a_name_degrades(self):
        self.assertEqual(
            _identity(_FakeCtx(_FakeSession(client_info=_FakeClientInfo(None)))),
            (None, None),
        )

    def test_raising_session_attributes_degrade(self):
        self.assertEqual(_identity(_FakeCtx(_BrokenSession())), (None, None))


class StdioUnicodeTests(unittest.TestCase):
    def test_unicode_facts_and_projects_round_trip_over_stdio(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(
                os.environ,
                STORE_N_RECALL_DB=os.path.join(tmp, "facts.db"),
                STORE_N_RECALL_PROJECT="проект-α",
            )

            async def run():
                params = StdioServerParameters(
                    command=sys.executable, args=["-m", "store_n_recall"], env=env)
                async with stdio_client(params) as (r, w):
                    async with ClientSession(r, w) as session:
                        await session.initialize()
                        res = await session.call_tool("store_fact", {
                            "fact": "Сборка требует флаг --offline 😀",
                            "kind": "gotcha",
                            "tags": ["сборка"],
                        })
                        stored = json.loads(res.content[0].text)["fact"]
                        self.assertEqual(stored["project"], "проект-α")
                        self.assertEqual(stored["tags"], ["сборка"])

                        res = await session.call_tool(
                            "recall_facts", {"query": "сборка"})
                        self.assertEqual(
                            [f["id"] for f in json.loads(res.content[0].text)["facts"]],
                            [stored["id"]],
                        )
                        res = await session.call_tool(
                            "recall_facts", {"tags": ["сборка"]})
                        self.assertEqual(
                            len(json.loads(res.content[0].text)["facts"]), 1)
                        res = await session.call_tool("list_projects", {})
                        self.assertEqual(
                            [p["project"] for p in
                             json.loads(res.content[0].text)["projects"]],
                            ["проект-α"],
                        )

            asyncio.run(asyncio.wait_for(run(), timeout=90))


class ConcurrentToolCallTests(ServerTestCase):
    """One server process serving overlapping calls from several agents."""

    def test_concurrent_stores_all_land_exactly_once(self):
        async def run():
            results = await asyncio.gather(*[
                self.mcp.call_tool(
                    "store_fact", {"project": "proj", "fact": f"fact {i}"})
                for i in range(16)
            ])
            return [json.loads(r.content[0].text)["fact"] for r in results]

        stored = asyncio.run(run())
        self.assertEqual(len({f["id"] for f in stored}), 16)
        facts = call(self.mcp, "recall_facts",
                     {"project": "proj", "limit": 100})["facts"]
        self.assertEqual(
            sorted(f["text"] for f in facts), sorted(f"fact {i}" for i in range(16)))
        for fact in facts[:3]:
            full = call(self.mcp, "recall_facts",
                        {"project": "proj", "id": fact["id"]})["facts"][0]
            self.assertEqual([s["action"] for s in full["sources"]], ["stored"])

    def test_concurrent_archives_have_one_winner_and_loud_losers(self):
        fid = self.store_one()["fact"]["id"]

        async def attempt():
            try:
                result = await self.mcp.call_tool("archive_fact", {"id": fid})
            except ToolError as exc:
                return ("error", str(exc))
            return ("ok", result.structured_content)

        async def run():
            return await asyncio.gather(*[attempt() for _ in range(6)])

        results = asyncio.run(run())
        winners = [r for r in results if r[0] == "ok"]
        losers = [r for r in results if r[0] == "error"]
        self.assertEqual(len(winners), 1)
        self.assertEqual(len(losers), 5)
        for _, text in losers:
            self.assertIn("already archived", text)
        self.assertEqual(
            [s["action"] for s in self.store.get_fact(fid)["sources"]],
            ["stored", "archived"],
        )
        # the server keeps serving after the race
        self.assertEqual(call(self.mcp, "recall_facts", {"project": "proj"})["facts"], [])


class SharedStoreTests(unittest.TestCase):
    """Two server instances on one file: knowledge crosses sessions."""

    def test_second_server_sees_and_can_retire_first_servers_facts(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = os.path.join(tmp.name, "facts.db")
        first = create_server(cfg=Config(db_path=db))
        second = create_server(cfg=Config(db_path=db))

        fact = call(first, "store_fact", {
            "project": "proj", "fact": "shared knowledge", "tags": ["repo"]})["fact"]
        seen = call(second, "recall_facts", {"project": "proj", "tags": ["repo"]})
        self.assertEqual([f["id"] for f in seen["facts"]], [fact["id"]])

        archived = call(second, "archive_fact", {"id": fact["id"]})
        self.assertIsNotNone(archived["fact"]["archived_at"])
        self.assertEqual(call(first, "recall_facts", {"project": "proj"})["facts"], [])
        self.assertEqual(
            [f["id"] for f in call(first, "scour_archives", {"project": "proj"})["facts"]],
            [fact["id"]],
        )
        self.assertEqual(
            call(first, "list_projects", {})["projects"][0]["archived"], 1)


class StdioProvenanceTests(unittest.TestCase):
    def test_client_info_wins_over_env_and_session_stays_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(
                os.environ,
                STORE_N_RECALL_DB=os.path.join(tmp, "facts.db"),
                STORE_N_RECALL_PROJECT="p",
                STORE_N_RECALL_AGENT="env-agent",
            )

            async def run():
                params = StdioServerParameters(
                    command=sys.executable, args=["-m", "store_n_recall"], env=env)
                async with stdio_client(params) as (r, w):
                    async with ClientSession(r, w) as session:  # default clientInfo
                        await session.initialize()
                        res = await session.call_tool("store_fact", {"fact": "x"})
                        stored = json.loads(res.content[0].text)["fact"]
                        event = stored["sources"][0]
                        self.assertEqual(event["action"], "stored")
                        self.assertTrue(event["agent"])      # from clientInfo
                        self.assertNotEqual(event["agent"], "env-agent")
                        # session is optional: recorded as absent, never fabricated
                        self.assertIsNone(event["session"])

                        res = await session.call_tool(
                            "archive_fact", {"id": stored["id"]})
                        archived = json.loads(res.content[0].text)["fact"]["sources"][-1]
                        self.assertEqual(archived["agent"], event["agent"])
                        self.assertIsNone(archived["session"])

            asyncio.run(asyncio.wait_for(run(), timeout=90))


if __name__ == "__main__":
    unittest.main()
