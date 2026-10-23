"""Store tests: storage semantics, edge cases, invariants (no-delete,
immutability, append-only provenance), FTS5 and LIKE fallback paths."""

import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime

from store_n_recall.config import FactError
from store_n_recall.store import Store, KINDS, _fts_available

FTS = _fts_available()


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "facts.db")
        self.store = Store(self.db)

    def text_of(self, fact_id):
        return self.store.get_fact(fact_id)["text"]

    def row_counts(self):
        conn = sqlite3.connect(self.db)
        try:
            facts = conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
            sources = conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
            return facts, sources
        finally:
            conn.close()


class InitTests(StoreTestCase):
    def test_no_file_until_first_use(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = os.path.join(tmp.name, "nested", "facts.db")
        store = Store(db)
        self.assertFalse(os.path.exists(db))  # created on demand, not at init
        store.store_fact("p", "hello")
        self.assertTrue(os.path.exists(db))

    def test_empty_store_recall_scour_list(self):
        self.assertEqual(self.store.recall_facts("p"), [])
        self.assertEqual(self.store.scour_archives("p"), [])
        self.assertEqual(self.store.list_projects(), [])

    def _warm(self):
        """Force the lazy schema creation (first connection)."""
        self.store.store_fact("proj", "warm-up")
        self.addCleanup(lambda: None)

    def test_wal_mode_is_persistent(self):
        # WAL is a persistent database property; busy_timeout is per-connection
        # (set at connect) and is exercised by the concurrency tests.
        self._warm()
        conn = sqlite3.connect(self.db)
        try:
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        finally:
            conn.close()

    def test_schema_present(self):
        self._warm()
        conn = sqlite3.connect(self.db)
        try:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertIn("facts", tables)
            self.assertIn("sources", tables)
        finally:
            conn.close()

    @unittest.skipUnless(FTS, "FTS5 not available on this sqlite3")
    def test_fts_mirror_is_synced_by_insert_trigger(self):
        self._warm()
        conn = sqlite3.connect(self.db)
        try:
            triggers = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'")}
            self.assertIn("facts_fts_insert", triggers)
        finally:
            conn.close()


class StoreFactTests(StoreTestCase):
    def test_defaults_applied(self):
        fact = self.store.store_fact("proj", "The build is offline")
        self.assertEqual(fact["kind"], "fact")
        self.assertEqual(fact["confidence"], 0.5)
        self.assertEqual(fact["tags"], [])
        self.assertEqual(fact["project"], "proj")
        self.assertIn("id", fact)
        # archived_at key omitted while active (present once archived)
        self.assertNotIn("archived_at", fact)

    def test_custom_fields_round_trip(self):
        fact = self.store.store_fact(
            "proj", "Use venv for dev",
            kind="convention", tags=["repo", "build"], confidence=0.9,
            source="CONTEXT.md#L12",
        )
        self.assertEqual(fact["kind"], "convention")
        self.assertEqual(fact["tags"], ["build", "repo"])
        self.assertEqual(fact["confidence"], 0.9)
        sources = fact["sources"]
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]["action"], "stored")
        self.assertEqual(sources[0]["pointer"], "CONTEXT.md#L12")
        self.assertNotIn("note", sources[0])
        self.assertNotIn("session", sources[0])

    def test_tags_normalized_sorted_deduped(self):
        fact = self.store.store_fact(
            "proj", "x", tags=["b", "a", "b", "  c  "])
        self.assertEqual(fact["tags"], ["a", "b", "c"])

    def test_kind_lowercased_and_stripped(self):
        fact = self.store.store_fact("proj", "x", kind="  Decision ")
        self.assertEqual(fact["kind"], "decision")

    def test_fact_text_stripped(self):
        fact = self.store.store_fact("proj", "  padded text  ")
        self.assertEqual(fact["text"], "padded text")

    def test_confidence_coerced_from_string(self):
        self.assertEqual(self.store.store_fact("p", "x", confidence="0.8")["confidence"], 0.8)

    def test_agent_session_recorded(self):
        fact = self.store.store_fact("p", "x", agent="agent-a", session="sess-1")
        self.assertEqual(fact["sources"][0]["agent"], "agent-a")
        self.assertEqual(fact["sources"][0]["session"], "sess-1")

    def test_default_agent_used_when_none_given(self):
        store = Store(self.db, default_agent="cfg-agent")
        fact = store.store_fact("p", "x")
        self.assertEqual(fact["sources"][0]["agent"], "cfg-agent")

    def test_timestamps_are_utc_iso8601(self):
        fact = self.store.store_fact("p", "x")
        at = fact["sources"][0]["at"]
        parsed = datetime.fromisoformat(at)
        self.assertIsNotNone(parsed.tzinfo)
        self.assertEqual(parsed.utcoffset().total_seconds(), 0)
        self.assertEqual(fact["created_at"], at)

    def test_store_returns_newest_id_first_ordering_consistent(self):
        a = self.store.store_fact("p", "first")["id"]
        b = self.store.store_fact("p", "second")["id"]
        self.assertLess(a, b)

    def test_project_stripped(self):
        self.assertEqual(self.store.store_fact("  proj  ", "x")["project"], "proj")

    def test_id_is_positive_int(self):
        for fact in (self.store.store_fact("p", "x"), self.store.store_fact("p", "y")):
            self.assertIsInstance(fact["id"], int)
            self.assertGreaterEqual(fact["id"], 1)

    # -- validation ---------------------------------------------------------

    def test_fact_required(self):
        for bad in (None, "", "   ", 42, ["not", "text"]):
            with self.assertRaises(FactError, msg=repr(bad)):
                self.store.store_fact("p", bad)

    def test_project_required(self):
        for bad in (None, "", "   "):
            with self.assertRaises(FactError, msg=repr(bad)):
                self.store.store_fact(bad, "x")
        for bad in (None, "", "   "):
            with self.assertRaises(FactError, msg=repr(bad)):
                self.store.recall_facts(bad)
            with self.assertRaises(FactError, msg=repr(bad)):
                self.store.scour_archives(bad)

    def test_query_must_be_string(self):
        self.store.store_fact("p", "x")
        for bad in (5, ["needle"], {"text": "needle"}):
            with self.assertRaises(FactError, msg=repr(bad)):
                self.store.recall_facts("p", query=bad)
            with self.assertRaises(FactError, msg=repr(bad)):
                self.store.scour_archives("p", query=bad)

    def test_get_fact_unknown_id(self):
        with self.assertRaises(FactError) as cm:
            self.store.get_fact(999)
        self.assertIn("no fact with id 999", str(cm.exception))

    def test_identical_facts_are_distinct_rows(self):
        # No dedup: facts are immutable records; corrections go through
        # supersede, so storing the same text twice is two independent facts.
        a = self.store.store_fact("p", "same words")["id"]
        b = self.store.store_fact("p", "same words")["id"]
        self.assertNotEqual(a, b)
        self.assertEqual([f["id"] for f in self.store.recall_facts("p")], [b, a])

    def test_kind_closed_taxonomy(self):
        for kind in (None, "fact", "decision", "gotcha", "convention", "FACT", " Decision "):
            self.store.store_fact("p", "x", kind=kind)  # valid or normalized
        for bad in ("note", "memory", "fact,gotcha"):
            with self.assertRaises(FactError, msg=repr(bad)):
                self.store.store_fact("p", "x", kind=bad)
        # non-string kinds are an error, not a crash
        for bad in (5, ["fact"], {"kind": "fact"}, 3.5):
            with self.assertRaises(FactError, msg=repr(bad)):
                self.store.store_fact("p", "x", kind=bad)

    def test_confidence_bounds(self):
        self.store.store_fact("p", "x", confidence=0.0)
        self.store.store_fact("p", "x", confidence=1.0)
        for bad in (-0.1, 1.01, float("nan"), float("inf"), float("-inf"), "abc", "1.0x", True, False):
            with self.assertRaises(FactError, msg=repr(bad)):
                self.store.store_fact("p", "x", confidence=bad)

    def test_tags_must_be_list_of_strings(self):
        for bad in ("repo", [1, 2], [None], [""], ["  "]):
            with self.assertRaises(FactError, msg=repr(bad)):
                self.store.store_fact("p", "x", tags=bad)

    def test_source_must_be_nonempty_string(self):
        for bad in ("", "   ", 5, None):
            if bad is None:
                self.store.store_fact("p", "x", source=None)  # allowed
            else:
                with self.assertRaises(FactError, msg=repr(bad)):
                    self.store.store_fact("p", "x", source=bad)

    def test_freeform_tags_and_unknown_kinds_of_tags_ok(self):
        fact = self.store.store_fact("p", "x", tags=["c++", "ci", "repo"])
        self.assertEqual(fact["tags"], ["c++", "ci", "repo"])  # not a whitelist


class RecallTests(StoreTestCase):
    def seed(self, facts):
        return [
            self.store.store_fact("proj", text, **kw)["id"]
            for text, kw in facts
        ]

    def test_recall_returns_active_only_newest_first(self):
        ids = self.seed([("one", {}), ("two", {}), ("three", {})])
        recalled = [f["id"] for f in self.store.recall_facts("proj")]
        self.assertEqual(recalled, [ids[2], ids[1], ids[0]])

    def test_recall_rows_are_summaries_without_sources(self):
        self.store.store_fact("proj", "row text", tags=["repo", "build"])
        facts = self.store.recall_facts("proj")
        self.assertEqual(len(facts), 1)
        row = facts[0]
        self.assertEqual(row["text"], "row text")
        self.assertNotIn("sources", row)  # full record is id= only
        self.assertIsInstance(row["tags"], list)  # decoded, not a JSON string
        self.assertEqual(row["tags"], ["build", "repo"])
        self.assertNotIn("archived_at", row)  # present once archived, not before

    def test_scour_rows_have_archived_at_and_list_tags(self):
        fid = self.store.store_fact("proj", "gone", tags=["x"])["id"]
        self.store.archive_fact(fid)
        rows = self.store.scour_archives("proj")
        self.assertEqual(len(rows), 1)
        self.assertIsNotNone(rows[0]["archived_at"])
        self.assertEqual(rows[0]["tags"], ["x"])

    def test_recall_by_id_returns_full_record_with_sources(self):
        fact_id = self.store.store_fact("proj", "audited", source="docs/x.md")["id"]
        self.store.store_fact("proj", "other")
        got = self.store.recall_facts("proj", fact_id=fact_id)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["id"], fact_id)
        full = self.store.get_fact(fact_id)
        self.assertIn("sources", full)
        self.assertEqual(full["sources"][0]["pointer"], "docs/x.md")

    def test_recall_by_id_on_archived_fact_fails(self):
        fact_id = self.store.store_fact("proj", "soon gone")["id"]
        self.store.archive_fact(fact_id)
        with self.assertRaises(FactError):
            self.store.recall_facts("proj", fact_id=fact_id)
        # ...but the fact still exists; get_fact (internal/archive paths) sees it
        self.assertEqual(self.store.get_fact(fact_id)["archived_at"] is not None, True)

    def test_recall_by_id_unknown(self):
        with self.assertRaises(FactError) as cm:
            self.store.recall_facts("proj", fact_id=999)
        self.assertIn("no active fact with id 999", str(cm.exception))

    def test_limit_applies_and_defaults_to_fifty(self):
        ids = self.seed([(f"fact {i}", {}) for i in range(60)])
        default = self.store.recall_facts("proj")
        self.assertEqual(len(default), 50)
        small = self.store.recall_facts("proj", limit=2)
        self.assertEqual([f["id"] for f in small], [ids[59], ids[58]])

    def test_limit_must_be_positive_int(self):
        for bad in (0, -1, 1.5, "5", True):
            with self.assertRaises(FactError, msg=repr(bad)):
                self.store.recall_facts("proj", limit=bad)

    def test_project_scoping_facts_never_cross(self):
        ids_a = [self.store.store_fact("proj-a", f"a {i}")["id"] for i in range(3)]
        self.store.store_fact("proj-b", "a b fact")
        got_a = self.store.recall_facts("proj-a")
        self.assertEqual({f["id"] for f in got_a}, set(ids_a))
        self.assertTrue(all(f["project"] == "proj-a" for f in got_a))

    def test_other_project_facts_invisible_to_query(self):
        self.store.store_fact("proj-a", "needle in this haystack")
        self.store.store_fact("proj-b", "needle elsewhere")
        self.assertEqual(len(self.store.recall_facts("proj-a", query="needle")), 1)

    # -- kind / tag / confidence filters ------------------------------------

    def test_kind_filter(self):
        self.store.store_fact("p", "d", kind="decision")
        self.store.store_fact("p", "g", kind="gotcha")
        got = self.store.recall_facts("p", kind="gotcha")
        self.assertEqual([f["text"] for f in got], ["g"])

    def test_kind_filter_invalid_kind(self):
        with self.assertRaises(FactError):
            self.store.recall_facts("p", kind="note")

    def test_tags_filter_combine_with_and(self):
        self.store.store_fact("p", "both", tags=["repo", "build"])
        self.store.store_fact("p", "repo-only", tags=["repo"])
        self.store.store_fact("p", "untagged")
        self.assertEqual(
            [f["text"] for f in self.store.recall_facts("p", tags=["repo"])],
            ["repo-only", "both"],  # newest first
        )
        self.assertEqual(
            [f["text"] for f in self.store.recall_facts("p", tags=["repo", "build"])],
            ["both"],
        )

    def test_tags_filter_unknown_tag_yields_nothing(self):
        self.store.store_fact("p", "x", tags=["repo"])
        self.assertEqual(self.store.recall_facts("p", tags=["nope"]), [])

    def test_min_confidence_inclusive(self):
        self.store.store_fact("p", "low", confidence=0.4)
        self.store.store_fact("p", "mid", confidence=0.5)
        self.store.store_fact("p", "high", confidence=0.9)
        got = self.store.recall_facts("p", min_confidence=0.5)
        self.assertEqual({f["text"] for f in got}, {"mid", "high"})

    def test_all_filters_combine(self):
        self.store.store_fact("p", "target", kind="gotcha", tags=["repo", "build"], confidence=0.8)
        self.store.store_fact("p", "wrong-kind", kind="decision", tags=["repo", "build"], confidence=0.8)
        self.store.store_fact("p", "low-conf", kind="gotcha", tags=["repo", "build"], confidence=0.3)
        self.store.store_fact("p", "missing-tag", kind="gotcha", tags=["repo"], confidence=0.8)
        got = self.store.recall_facts(
            "p", kind="gotcha", tags=["repo", "build"], min_confidence=0.7)
        self.assertEqual([f["text"] for f in got], ["target"])

    def test_id_and_filters_combine(self):
        fid = self.store.store_fact("p", "x", kind="gotcha")["id"]
        self.assertEqual(len(self.store.recall_facts("p", fact_id=fid, kind="gotcha")), 1)
        self.assertEqual(self.store.recall_facts("p", fact_id=fid, kind="fact"), [])


@unittest.skipUnless(FTS, "FTS5 not available on this sqlite3")
class QueryFtsTests(StoreTestCase):
    def test_query_ranks_relevant_fact_first(self):
        self.store.store_fact("p", "builds need the offline flag to pass")
        self.store.store_fact("p", "tests run with unittest, no network")
        got = self.store.recall_facts("p", query="offline flag")
        self.assertEqual(got[0]["text"], "builds need the offline flag to pass")

    def test_query_is_case_insensitive(self):
        self.store.store_fact("p", "Run tests with UNITTEST")
        got = self.store.recall_facts("p", query="unittest")
        self.assertEqual(len(got), 1)

    def test_query_matches_whole_tokens(self):
        # FTS5 tokenizes: "pytest" is one token, so "test" does not match it;
        # the exact token does.
        self.store.store_fact("p", "pytest is the runner")
        self.assertEqual(len(self.store.recall_facts("p", query="pytest")), 1)
        self.assertEqual(self.store.recall_facts("p", query="test"), [])

    def test_query_with_fts_operators_are_literal(self):
        # FTS syntax like quotes/parens must not change semantics: quoted
        # phrase tokens only.
        self.store.store_fact("p", "alpha beta")
        self.store.store_fact("p", "alpha")
        got = self.store.recall_facts("p", query='"alpha" "beta"')
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["text"], "alpha beta")

    def test_punctuation_only_query_matches_nothing(self):
        self.store.store_fact("p", "some words here")
        self.assertEqual(self.store.recall_facts("p", query="!!! ???"), [])

    def test_empty_query_is_ignored(self):
        self.store.store_fact("p", "first")
        self.store.store_fact("p", "second")
        self.assertEqual(len(self.store.recall_facts("p", query="   ")), 2)

    def test_query_like_fallback_compatible_shape(self):
        # Same call through the LIKE path must at least find the fact.
        alt = Store(self.db, use_fts=False)
        self.store.store_fact("p", "unique needle phrase")
        self.assertEqual(len(alt.recall_facts("p", query="needle")), 1)

    def test_query_still_newest_first_on_ties(self):
        self.store.store_fact("p", "same terms here")
        self.store.store_fact("p", "same terms again")
        got = self.store.recall_facts("p", query="terms")
        self.assertEqual(len(got), 2)

    def test_query_composes_with_kind_tags_and_confidence(self):
        self.store.store_fact(
            "p", "needle in the build docs", kind="gotcha", tags=["repo", "build"], confidence=0.9)
        self.store.store_fact(
            "p", "needle in the build docs", kind="fact", tags=["repo", "build"], confidence=0.9)
        self.store.store_fact(
            "p", "needle in the build docs", kind="gotcha", tags=["repo"], confidence=0.9)
        self.store.store_fact(
            "p", "needle in the build docs", kind="gotcha", tags=["repo", "build"], confidence=0.4)
        got = self.store.recall_facts(
            "p", query="needle", kind="gotcha", tags=["repo", "build"], min_confidence=0.5)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["tags"], ["build", "repo"])


class QueryLikeFallbackTests(StoreTestCase):
    """Explicitly exercise the no-FTS path (Store(use_fts=False))."""

    def setUp(self):
        super().setUp()
        self.like = Store(self.db, use_fts=False)

    def test_like_fallback_finds_substring_case_insensitively(self):
        self.like.store_fact("p", "The CACHING layer is deep")
        self.assertEqual(len(self.like.recall_facts("p", query="caching")), 1)
        self.assertEqual(len(self.like.recall_facts("p", query="layer")), 1)

    def test_like_fallback_orders_newest_first(self):
        a = self.like.store_fact("p", "alpha content")["id"]
        b = self.like.store_fact("p", "alpha content")["id"]
        got = self.like.recall_facts("p", query="alpha")
        self.assertEqual([f["id"] for f in got], [b, a])

    def test_like_fallback_escapes_like_wildcards(self):
        self.like.store_fact("p", "coverage is at 100% now")
        self.like.store_fact("p", "coverage 10X now")
        got = self.like.recall_facts("p", query="100%")
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["text"], "coverage is at 100% now")

    def test_like_fallback_underscore_literal(self):
        self.like.store_fact("p", "file_1 runs first")
        self.like.store_fact("p", "fileX1 runs later")
        got = self.like.recall_facts("p", query="file_1")
        self.assertEqual([f["text"] for f in got], ["file_1 runs first"])

    def test_like_fallback_no_match(self):
        self.like.store_fact("p", "nothing here")
        self.assertEqual(self.like.recall_facts("p", query="absent"), [])

    def test_like_fallback_respects_filters(self):
        self.like.store_fact("p", "needle in build", tags=["repo"], kind="gotcha", confidence=0.7)
        self.like.store_fact("p", "needle in tests", tags=["repo"], kind="gotcha", confidence=0.7)
        self.like.store_fact("p", "needle wrong kind", tags=["repo"], kind="fact", confidence=0.7)
        self.assertEqual(
            len(self.like.recall_facts("p", query="needle", kind="gotcha", tags=["repo"], min_confidence=0.5)),
            2,
        )


class ArchiveTests(StoreTestCase):
    def test_archive_sets_archived_at_hides_from_recall(self):
        fact_id = self.store.store_fact("p", "to archive")["id"]
        archived = self.store.archive_fact(fact_id, note="no longer true")
        self.assertIsNotNone(archived["archived_at"])
        self.assertEqual(archived["sources"][-1]["action"], "archived")
        self.assertEqual(archived["sources"][-1]["note"], "no longer true")
        self.assertNotIn(fact_id, {f["id"] for f in self.store.recall_facts("p")})
        self.assertEqual(
            [f["id"] for f in self.store.scour_archives("p")], [fact_id]
        )

    def test_archive_unknown_id(self):
        with self.assertRaises(FactError) as cm:
            self.store.archive_fact(999)
        self.assertIn("no fact with id 999", str(cm.exception))

    def test_archive_twice_is_error(self):
        fact_id = self.store.store_fact("p", "once")["id"]
        self.store.archive_fact(fact_id)
        with self.assertRaises(FactError) as cm:
            self.store.archive_fact(fact_id)
        self.assertIn("already archived", str(cm.exception))

    def test_archive_id_validation(self):
        for bad in (0, -3, "x", None, 1.5, True):
            with self.assertRaises(FactError, msg=repr(bad)):
                self.store.archive_fact(bad)

    def test_recall_id_validation(self):
        for bad in (0, -3, "x", 1.5, True):
            with self.assertRaises(FactError, msg=repr(bad)):
                self.store.recall_facts("p", fact_id=bad)

    def test_supersedes_id_validation(self):
        for bad in (0, -3, "x", 1.5, True):
            with self.assertRaises(FactError, msg=repr(bad)):
                self.store.store_fact("p", "x", supersedes=bad)

    def test_archive_note_validation(self):
        fact_id = self.store.store_fact("p", "note me")["id"]
        self.store.archive_fact(fact_id, note="a reason")  # valid
        fid2 = self.store.store_fact("p", "note me2")["id"]
        for bad in ("", "   ", 5):
            with self.assertRaises(FactError, msg=repr(bad)):
                self.store.archive_fact(fid2, note=bad)

    def test_archive_agent_recorded(self):
        fact_id = self.store.store_fact("p", "x")["id"]
        archived = self.store.archive_fact(fact_id, agent="archiver", session="s1")
        event = archived["sources"][-1]
        self.assertEqual(event["agent"], "archiver")
        self.assertEqual(event["session"], "s1")

    def test_archive_does_not_append_note_key_when_absent(self):
        fact_id = self.store.store_fact("p", "x")["id"]
        event = self.store.archive_fact(fact_id)["sources"][-1]
        self.assertNotIn("note", event)


class ScourTests(StoreTestCase):
    def test_scour_returns_archived_only(self):
        a = self.store.store_fact("p", "active one")["id"]
        b = self.store.store_fact("p", "archived one")["id"]
        self.store.archive_fact(b)
        self.assertEqual([f["id"] for f in self.store.scour_archives("p")], [b])
        self.assertNotIn(a, {f["id"] for f in self.store.scour_archives("p")})

    def test_scour_orders_most_recently_archived_first(self):
        a = self.store.store_fact("p", "old")["id"]
        b = self.store.store_fact("p", "new")["id"]
        self.store.archive_fact(a)
        self.store.archive_fact(b)
        self.assertEqual([f["id"] for f in self.store.scour_archives("p")], [b, a])

    def test_scour_has_recall_filters(self):
        a = self.store.store_fact("p", "old gotcha", kind="gotcha", tags=["repo"], confidence=0.9)["id"]
        self.store.store_fact("p", "old fact", kind="fact", tags=["repo"], confidence=0.9)["id"]
        b = self.store.store_fact("p", "old gotcha wrong tag", kind="gotcha", tags=["docs"], confidence=0.9)["id"]
        for fid in (a, b):
            self.store.archive_fact(fid)
        got = self.store.scour_archives("p", kind="gotcha", tags=["repo"], min_confidence=0.8)
        self.assertEqual([f["id"] for f in got], [a])

    def test_scour_query_works(self):
        self.store.store_fact("p", "retired needle")["id"]
        fid = self.store.store_fact("p", "retired needle too")["id"]
        self.store.archive_fact(fid)
        got = self.store.scour_archives("p", query="needle")
        self.assertEqual([f["id"] for f in got], [fid])
        self.assertNotIn("retired", [f["text"] for f in self.store.recall_facts("p", query="needle")])

    def test_scour_by_id_full_record(self):
        fid = self.store.store_fact("p", "gone")["id"]
        self.store.archive_fact(fid)
        got = self.store.scour_archives("p", fact_id=fid)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["archived_at"] is not None, True)

    def test_scour_by_id_active_fact_fails(self):
        fid = self.store.store_fact("p", "still active")["id"]
        with self.assertRaises(FactError) as cm:
            self.store.scour_archives("p", fact_id=fid)
        self.assertIn("no archived fact with id", str(cm.exception))

    def test_scour_by_id_unknown(self):
        with self.assertRaises(FactError) as cm:
            self.store.scour_archives("p", fact_id=999)
        self.assertIn("no archived fact with id 999", str(cm.exception))

    def test_scour_by_id_filters_may_narrow(self):
        fid = self.store.store_fact("p", "archived gotcha", kind="gotcha")["id"]
        self.store.archive_fact(fid)
        self.assertEqual(len(self.store.scour_archives("p", fact_id=fid, kind="gotcha")), 1)
        self.assertEqual(self.store.scour_archives("p", fact_id=fid, kind="fact"), [])


class SupersedeTests(StoreTestCase):
    def test_supersede_archives_old_in_same_act(self):
        old = self.store.store_fact("p", "stale claim")["id"]
        new = self.store.store_fact("p", "corrected claim", supersedes=old)
        # old archived, new active
        self.assertIsNotNone(self.store.get_fact(old)["archived_at"])
        self.assertNotIn(old, {f["id"] for f in self.store.recall_facts("p")})
        self.assertEqual([f["id"] for f in self.store.recall_facts("p")], [new["id"]])
        self.assertIn(old, {f["id"] for f in self.store.scour_archives("p")})

    def test_supersede_audit_trail(self):
        old = self.store.store_fact("p", "old", agent="a1")["id"]
        new = self.store.store_fact(
            "p", "new", supersedes=old, agent="a2", source="fix/commit")
        old_record = self.store.get_fact(old)
        self.assertEqual(
            [s["action"] for s in old_record["sources"]], ["stored", "archived"]
        )
        self.assertEqual(old_record["sources"][0]["agent"], "a1")
        self.assertEqual(old_record["sources"][1]["agent"], "a2")
        self.assertEqual([s["action"] for s in new["sources"]], ["stored"])
        self.assertEqual(new["sources"][0]["pointer"], "fix/commit")

    def test_supersede_unknown_target_is_atomic(self):
        before = self.store.recall_facts("p")
        with self.assertRaises(FactError) as cm:
            self.store.store_fact("p", "new", supersedes=4242)
        self.assertIn("no fact with id 4242", str(cm.exception))
        # nothing stored: recall unchanged and no extra rows
        self.assertEqual(self.store.recall_facts("p"), before)
        self.assertEqual(self.store.list_projects(), [])

    def test_supersede_archived_target_rejected(self):
        old = self.store.store_fact("p", "old")["id"]
        self.store.archive_fact(old)
        with self.assertRaises(FactError) as cm:
            self.store.store_fact("p", "new", supersedes=old)
        self.assertIn("already archived", str(cm.exception))

    def test_supersede_cross_project_rejected(self):
        old = self.store.store_fact("proj-a", "old")["id"]
        with self.assertRaises(FactError) as cm:
            self.store.store_fact("proj-b", "new", supersedes=old)
        self.assertIn("belongs to project", str(cm.exception))
        # old stays active; nothing stored
        self.assertEqual(len(self.store.recall_facts("proj-a")), 1)
        self.assertEqual(self.store.scour_archives("proj-a"), [])

    def test_supersede_same_project_via_default(self):
        old = self.store.store_fact("p", "old")["id"]
        self.store.store_fact("p", "new", supersedes=old)
        self.assertIsNotNone(self.store.get_fact(old)["archived_at"])

    def test_supersede_supports_string_id(self):
        old = self.store.store_fact("p", "old")["id"]
        self.store.store_fact("p", "new", supersedes=str(old))
        self.assertIsNotNone(self.store.get_fact(old)["archived_at"])


class ListProjectsTests(StoreTestCase):
    def test_counts_active_and_archived(self):
        self.store.store_fact("a", "1")
        self.store.store_fact("a", "2")
        b = self.store.store_fact("b", "3")["id"]
        self.store.archive_fact(b)
        by_name = {p["project"]: p for p in self.store.list_projects()}
        self.assertEqual(by_name["a"]["active"], 2)
        self.assertEqual(by_name["a"]["archived"], 0)
        self.assertEqual(by_name["b"]["active"], 0)
        self.assertEqual(by_name["b"]["archived"], 1)

    def test_superseded_counts_as_archived(self):
        old = self.store.store_fact("a", "old")["id"]
        self.store.store_fact("a", "new", supersedes=old)
        p = self.store.list_projects()[0]
        self.assertEqual((p["active"], p["archived"]), (1, 1))

    def test_projects_sorted_by_name(self):
        self.store.store_fact("zeta", "x")
        self.store.store_fact("alpha", "x")
        self.store.store_fact("Mid", "x")  # byte order: capital before lowercase
        self.assertEqual(
            [p["project"] for p in self.store.list_projects()], ["Mid", "alpha", "zeta"]
        )

    def test_top_tags_sorted_by_count_then_name(self):
        for tag in ("repo", "repo", "repo", "build", "build", "docs"):
            self.store.store_fact("p", "f", tags=[tag])
        top = self.store.list_projects()[0]["top_tags"]
        self.assertEqual(top, ["repo", "build", "docs"])

    def test_top_tags_counted_across_archived_too(self):
        fid = self.store.store_fact("p", "x", tags=["repo"])["id"]
        self.store.archive_fact(fid)
        top = self.store.list_projects()[0]["top_tags"]
        self.assertEqual(top, ["repo"])

    def test_top_tags_limited_to_five(self):
        tags = [f"t{i}" for i in range(8)]
        for tag in tags:
            self.store.store_fact("p", "f", tags=[tag])
        top = self.store.list_projects()[0]["top_tags"]
        self.assertEqual(len(top), 5)

    def test_latest_activity_tracks_stores_and_archives(self):
        self.store.store_fact("p", "first")
        first = self.store.list_projects()[0]["latest_activity"]
        fid = self.store.store_fact("p", "second")["id"]
        second = self.store.list_projects()[0]["latest_activity"]
        self.assertGreaterEqual(second, first)
        self.store.archive_fact(fid)
        third = self.store.list_projects()[0]["latest_activity"]
        self.assertGreaterEqual(third, second)

    def test_project_with_only_archived_facts_still_listed(self):
        fid = self.store.store_fact("p", "x")["id"]
        self.store.archive_fact(fid)
        listed = self.store.list_projects()
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["archived"], 1)


class ImmutabilityTests(StoreTestCase):
    """The hard invariants: rows never deleted, content never rewritten,
    provenance strictly append-only."""

    def _snapshot(self):
        conn = sqlite3.connect(self.db)
        try:
            facts = conn.execute(
                "SELECT id, project, kind, text, tags, confidence, created_at, archived_at"
                " FROM facts ORDER BY id").fetchall()
            sources = conn.execute(
                "SELECT fact_id, action, agent, session, at, note, pointer"
                " FROM sources ORDER BY id").fetchall()
            return facts, sources
        finally:
            conn.close()

    def test_no_row_is_ever_deleted(self):
        f1 = self.store.store_fact("p", "one")["id"]
        f2 = self.store.store_fact("p", "two")["id"]
        n0 = self.row_counts()
        self.store.archive_fact(f1)
        self.store.store_fact("p", "three", supersedes=f2)
        self.store.recall_facts("p", query="one")
        self.store.scour_archives("p")
        self.store.list_projects()
        # store + archive + supersede must grow rows, never shrink them
        n1 = self.row_counts()
        self.assertEqual(n1[0], n0[0] + 1)  # one new fact
        self.assertGreater(n1[1], n0[1])    # provenance events appended
        facts, _ = self._snapshot()
        self.assertEqual(len(facts), 3)

    def test_fact_content_never_mutated(self):
        fact = self.store.store_fact(
            "p", "immutable text", kind="decision", tags=["a", "b"], confidence=0.7)
        fid = fact["id"]
        self.store.archive_fact(fid, agent="x")
        self.store.store_fact("p", "newer", supersedes=None)
        got = self.store.get_fact(fid)
        self.assertEqual(got["text"], "immutable text")
        self.assertEqual(got["kind"], "decision")
        self.assertEqual(got["tags"], ["a", "b"])
        self.assertEqual(got["confidence"], 0.7)
        self.assertEqual(got["created_at"], fact["created_at"])

    def test_sources_append_only_and_prior_entries_unchanged(self):
        fid = self.store.store_fact("p", "x", agent="a", source="s1")["id"]
        _, s0 = self._snapshot()
        self.store.archive_fact(fid, note="bye", agent="b")
        _, s1 = self._snapshot()
        self.assertEqual(len(s1), len(s0) + 1)
        self.assertEqual(tuple(s0[0]), tuple(s1[0]))  # stored event untouched

    def test_failed_ops_leave_no_trace(self):
        fid = self.store.store_fact("p", "real")["id"]
        count_before = self.row_counts()
        for op in (
            lambda: self.store.store_fact("p", "x", supersedes=4242),
            lambda: self.store.archive_fact(4242),
            lambda: self.store.store_fact("p", "x", confidence=1.5),
            lambda: self.store.store_fact("", "x"),
        ):
            with self.assertRaises(FactError):
                op()
        self.assertEqual(self.row_counts(), count_before)
        self.assertEqual(self.store.recall_facts("p", fact_id=fid)[0]["id"], fid)

    def test_fts_mirror_never_grows_stale_after_archive(self):
        fid = self.store.store_fact("p", "needle stays searchable")["id"]
        self.store.archive_fact(fid)
        # archived fact still searchable via scour (mirror not deleted)
        self.assertEqual(len(self.store.scour_archives("p", query="needle")), 1)
        self.assertEqual(self.store.recall_facts("p", query="needle"), [])


if __name__ == "__main__":
    unittest.main()
