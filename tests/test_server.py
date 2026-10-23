"""Server tests: tool registration, end-to-end behavior over the real
MCPServer call path, project resolution, error strings, provenance defaults."""

import asyncio
import json
import os
import tempfile
import unittest

from store_n_recall.config import Config, FactError
from store_n_recall.server import create_server
from store_n_recall.store import Store

TOOLS = {"store_fact", "recall_facts", "archive_fact", "scour_archives", "list_projects"}


def call(mcp, name, arguments):
    result = asyncio.run(mcp.call_tool(name, arguments))
    assert not result.is_error, f"tool {name} failed: {result}"
    return json.loads(result.content[0].text)


def call_raw(mcp, name, arguments):
    result = asyncio.run(mcp.call_tool(name, arguments))
    return result.content[0].text


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
        fact = self.store_one(tags=["build"], kind="gotcha", source="docs/1.md")
        self.assertEqual(fact["kind"], "gotcha")
        self.assertEqual(fact["sources"][0]["pointer"], "docs/1.md")

        recalled = call(self.mcp, "recall_facts", {"project": "proj"})
        self.assertEqual([f["id"] for f in recalled["facts"]], [fact["id"]])
        self.assertNotIn("sources", recalled["facts"][0])  # summary row

        full = call(self.mcp, "recall_facts", {"project": "proj", "id": fact["id"]})
        self.assertEqual(full["fact"]["id"], fact["id"])
        self.assertIn("sources", full["fact"])

        archived = call(self.mcp, "archive_fact", {"id": fact["id"], "note": "stale"})
        self.assertIsNotNone(archived["archived_at"])
        self.assertEqual(archived["sources"][-1]["note"], "stale")

        recalled_after = call(self.mcp, "recall_facts", {"project": "proj"})
        self.assertEqual(recalled_after["facts"], [])

        scoured = call(self.mcp, "scour_archives", {"project": "proj"})
        self.assertEqual([f["id"] for f in scoured["facts"]], [fact["id"]])

        # recall by id is blind to archived facts; scour by id sees them
        error = call_raw(self.mcp, "recall_facts", {"project": "proj", "id": fact["id"]})
        self.assertTrue(error.startswith("error:"))
        scoured_full = call(self.mcp, "scour_archives", {"project": "proj", "id": fact["id"]})
        self.assertEqual(scoured_full["fact"]["id"], fact["id"])

        projects = call(self.mcp, "list_projects", {})["projects"]
        self.assertEqual(len(projects), 1)
        self.assertEqual(projects[0]["project"], "proj")
        self.assertEqual((projects[0]["active"], projects[0]["archived"]), (0, 1))
        self.assertEqual(projects[0]["top_tags"], ["build"])

    def test_supersede_via_mcp(self):
        old = self.store_one()
        new = call(
            self.mcp, "store_fact",
            {"project": "proj", "fact": "corrected", "supersedes": old["id"]},
        )
        active = call(self.mcp, "recall_facts", {"project": "proj"})["facts"]
        self.assertEqual([f["id"] for f in active], [new["id"]])
        # old is archived: visible via scour full record
        scoured = call(self.mcp, "scour_archives", {"project": "proj", "id": old["id"]})
        self.assertEqual(
            [s["action"] for s in scoured["fact"]["sources"]], ["stored", "archived"]
        )

    def test_all_results_parse_as_json(self):
        out = [
            self.store_one(),
            call(self.mcp, "recall_facts", {"project": "proj"}),
            call(self.mcp, "archive_fact", {"id": self.store_one()["id"]}),
            call(self.mcp, "scour_archives", {"project": "proj"}),
            call(self.mcp, "list_projects", {}),
        ]
        for item in out:
            json.dumps(item)  # round-trips


class ProjectResolutionTests(ServerTestCase):
    def test_param_wins_over_env_default(self):
        mcp = create_server(cfg=Config(db_path=self.db, default_project="env-proj"))
        fact = call(mcp, "store_fact", {"project": "explicit", "fact": "x"})
        self.assertEqual(fact["project"], "explicit")

    def test_env_default_used_when_param_absent(self):
        mcp = create_server(cfg=Config(db_path=self.db, default_project="env-proj"))
        fact = call(mcp, "store_fact", {"fact": "x"})
        self.assertEqual(fact["project"], "env-proj")

    def test_no_project_is_a_clear_error(self):
        text = call_raw(self.mcp, "store_fact", {"fact": "x"})
        self.assertTrue(text.startswith("error:"), text)
        self.assertIn("project", text)
        text2 = call_raw(self.mcp, "recall_facts", {})
        self.assertTrue(text2.startswith("error:"))

    def test_project_error_not_a_crash(self):
        result = asyncio.run(self.mcp.call_tool("store_fact", {"fact": "x"}))
        self.assertFalse(result.is_error)  # user-facing error, not tool failure
        self.assertTrue(result.content[0].text.startswith("error:"))


class ValidationViaMcpTests(ServerTestCase):
    def test_validation_errors_are_actionable_text(self):
        for name, args in (
            ("store_fact", {"project": "p", "fact": ""}),
            ("store_fact", {"project": "p", "fact": "x", "kind": "note"}),
            ("store_fact", {"project": "p", "fact": "x", "confidence": 3.0}),
            ("store_fact", {"project": "p", "fact": "x", "supersedes": 4242}),
            ("archive_fact", {"id": 4242}),
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
            text = call_raw(self.mcp, name, args)
            self.assertTrue(text.startswith("error:"), f"{name}{args} -> {text}")

    def test_schema_mismatches_fail_tool_call_not_server(self):
        # Garbage shapes fail pydantic validation at the SDK layer; the server
        # must report a tool error, never crash the process.
        from mcp.server.mcpserver.exceptions import ToolError
        for name, args in (
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
        )
        self.assertEqual(fact["confidence"], 0.8)
        self.assertEqual(fact["kind"], "gotcha")
        fid = fact["id"]
        call(self.mcp, "store_fact", {"project": "p", "fact": "replacement", "supersedes": str(fid)})
        self.assertIsNotNone(
            call(self.mcp, "scour_archives", {"project": "p", "id": str(fid)})["fact"]["archived_at"]
        )

    def test_supersede_cross_project_via_mcp(self):
        old = call(self.mcp, "store_fact", {"project": "a", "fact": "x"})
        text = call_raw(
            self.mcp, "store_fact",
            {"project": "b", "fact": "y", "supersedes": old["id"]},
        )
        self.assertTrue(text.startswith("error:"))
        self.assertIn("belongs to project", text)


class ProvenanceTests(ServerTestCase):
    def test_agent_defaults_from_env_config(self):
        mcp = create_server(cfg=Config(db_path=self.db, agent="builder-42"))
        fact = call(mcp, "store_fact", {"project": "p", "fact": "x"})
        self.assertEqual(fact["sources"][0]["agent"], "builder-42")

    def test_agent_unknown_when_nothing_configured(self):
        fact = call(self.mcp, "store_fact", {"project": "p", "fact": "x"})
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
        from store_n_recall.server import _identity
        self.assertEqual(_identity(None), (None, None))

    def test_client_info_name_extracted(self):
        from store_n_recall.server import _identity
        ctx = _FakeCtx(_FakeSession(client_info=_FakeClientInfo("agent-7")))
        self.assertEqual(_identity(ctx), ("agent-7", None))

    def test_session_id_extracted_when_transport_has_one(self):
        from store_n_recall.server import _identity
        ctx = _FakeCtx(_FakeSession(client_info=_FakeClientInfo("agent-7"), session_id="sess-abc"))
        self.assertEqual(_identity(ctx), ("agent-7", "sess-abc"))

    def test_session_id_without_client_info(self):
        from store_n_recall.server import _identity
        ctx = _FakeCtx(_FakeSession(client_info=None, session_id="sess-abc"))
        self.assertEqual(_identity(ctx), (None, "sess-abc"))

    def test_missing_client_info_degrades_to_none(self):
        from store_n_recall.server import _identity
        ctx = _FakeCtx(_FakeSession(client_info=None))
        self.assertEqual(_identity(ctx), (None, None))

    def test_identity_passthrough_to_stored_sources(self):
        # A real MCP request wires ctx into store_fact; simulate the wiring
        # (as the SDK does) and prove the agent lands in provenance.
        store = Store(os.path.join(tempfile.mkdtemp(), "f.db"))
        from store_n_recall.server import _identity
        agent, session = _identity(
            _FakeCtx(_FakeSession(client_info=_FakeClientInfo("agent-9")))
        )
        fact = store.store_fact("p", "x", agent=agent, session=session)
        self.assertEqual(fact["sources"][0]["agent"], "agent-9")


if __name__ == "__main__":
    unittest.main()
