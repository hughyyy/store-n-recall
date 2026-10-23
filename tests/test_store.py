"""Store tests: storage semantics, edge cases, invariants (no-delete,
immutability, append-only provenance), FTS5 and LIKE fallback paths."""

import os
import pathlib
import random
import sqlite3
import tempfile
import unittest
from datetime import datetime
from decimal import Decimal
from fractions import Fraction
from unittest import mock


from store_n_recall.config import FactError
from store_n_recall.store import (
    KINDS,
    MAX_FACT_CHARS,
    MAX_FACT_ID,
    SCHEMA_VERSION,
    Store,
    _FTS_TABLE,
    _FTS_TRIGGER,
    _fts_available,
    _fts_ready,
    _init_schema,
)

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

    def test_bare_relative_path_without_parent_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                store = Store("plain.db")
                store.store_fact("p", "x")
                self.assertTrue(os.path.exists("plain.db"))
                self.assertEqual(len(store.recall_facts("p")), 1)
            finally:
                os.chdir(cwd)

    def test_fts_probe_falls_back_when_fts5_missing(self):
        def boom(*args, **kwargs):
            raise sqlite3.OperationalError("no such module: fts5")

        with mock.patch("store_n_recall.store.sqlite3.connect", side_effect=boom):
            self.assertFalse(_fts_available())

    def test_empty_store_recall_scour_list(self):
        self.assertEqual(self.store.recall_facts("p"), [])
        self.assertEqual(self.store.scour_archives("p"), [])
        self.assertEqual(self.store.list_projects(), [])

    def _warm(self):
        """Force the lazy schema creation (first connection)."""
        self.store.store_fact("proj", "warm-up")

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

    def test_query_ties_break_newest_first(self):
        self.store.store_fact("p", "same terms here")
        self.store.store_fact("p", "same terms again")
        got = self.store.recall_facts("p", query="terms")
        # equal bm25 scores (same term frequency and length): id DESC decides
        self.assertEqual(
            [f["text"] for f in got], ["same terms again", "same terms here"]
        )

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

    def test_unicode_words_searchable(self):
        self.store.store_fact("p", "Привет мир, сборка готова")
        self.assertEqual(len(self.store.recall_facts("p", query="сборка")), 1)
        self.assertEqual(len(self.store.recall_facts("p", query="мир")), 1)
        # multi-word query is an implicit AND over tokens
        self.assertEqual(len(self.store.recall_facts("p", query="сборка мир")), 1)
        self.assertEqual(self.store.recall_facts("p", query="сборка отсутствует"), [])

    def test_emoji_has_no_word_tokens(self):
        self.store.store_fact("p", "docker deploy 🐳 everywhere")
        self.assertEqual(len(self.store.recall_facts("p", query="docker")), 1)
        # punctuation/emoji-only query matches nothing, per the token rule
        self.assertEqual(self.store.recall_facts("p", query="🐳"), [])
        # words in a mixed query still count; the emoji token is dropped
        self.assertEqual(len(self.store.recall_facts("p", query="docker 🐳")), 1)


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

    def test_like_fallback_unicode_exact_match(self):
        self.like.store_fact("p", "déjà vu 日本語")
        self.assertEqual(len(self.like.recall_facts("p", query="日本語")), 1)
        self.assertEqual(len(self.like.recall_facts("p", query="déjà")), 1)

    def test_scour_query_with_like_orders_most_recently_archived_first(self):
        a = self.like.store_fact("p", "retired needle")["id"]
        b = self.like.store_fact("p", "another retired needle")["id"]
        self.like.archive_fact(a)
        self.like.archive_fact(b)
        got = self.like.scour_archives("p", query="needle")
        self.assertEqual([f["id"] for f in got], [b, a])
        self.assertEqual(
            [f["id"] for f in self.like.scour_archives("p", query="needle", fact_id=b)],
            [b])

    def test_like_fallback_backslash_is_literal(self):
        self.like.store_fact("p", "C:\\temp\\build runs")
        self.like.store_fact("p", "C:/temp/build runs")
        got = self.like.recall_facts("p", query="\\temp\\")
        self.assertEqual([f["text"] for f in got], ["C:\\temp\\build runs"])

    def test_like_fallback_punctuation_query_matches_literally(self):
        # LIKE is a literal substring match: punctuation is searchable text
        # (unlike FTS, where a punctuation-only query has no tokens).
        self.like.store_fact("p", "it works!!!")
        self.like.store_fact("p", "it works")
        got = self.like.recall_facts("p", query="!!!")
        self.assertEqual([f["text"] for f in got], ["it works!!!"])

    def test_like_fallback_blank_query_is_newest_first(self):
        a = self.like.store_fact("p", "first")["id"]
        b = self.like.store_fact("p", "second")["id"]
        self.assertEqual(
            [f["id"] for f in self.like.recall_facts("p", query="   ")], [b, a]
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
        self.store.store_fact("p", "fact 1")  # a fact exists: bad ids must not land on it
        for bad in (0, -3, "x", None, 1.5, True):
            with self.assertRaises(FactError, msg=repr(bad)) as cm:
                self.store.archive_fact(bad)
            self.assertIn("positive integer", str(cm.exception))

    def test_recall_id_validation(self):
        self.store.store_fact("p", "fact 1")
        for bad in (0, -3, "x", 1.5, True):
            with self.assertRaises(FactError, msg=repr(bad)) as cm:
                self.store.recall_facts("p", fact_id=bad)
            self.assertIn("positive integer", str(cm.exception))

    def test_supersedes_id_validation(self):
        self.store.store_fact("p", "fact 1")
        for bad in (0, -3, "x", 1.5, True):
            with self.assertRaises(FactError, msg=repr(bad)) as cm:
                self.store.store_fact("p", "x", supersedes=bad)
            self.assertIn("positive integer", str(cm.exception))

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


class FixedClock:
    """Deterministic, strictly increasing ISO-8601 UTC timestamps."""

    def __init__(self, start: int = 0):
        self.tick = start

    def __call__(self) -> str:
        self.tick += 1
        return f"2026-01-01T00:00:00.{self.tick:03d}+00:00"


class FrozenClock:
    """One timestamp for every call: forces the id tie-break path."""

    def __call__(self) -> str:
        return "2026-01-01T00:00:00.000+00:00"


class IdCoercionTests(StoreTestCase):
    """An id addresses exactly one fact: no truncation, no overflow crash."""

    BAD_IDS = (
        0, -1, -1.5, 1.5, float("inf"), float("-inf"), float("nan"),
        "0", "1.5", "x", True,
    )

    def test_bad_ids_never_alias_an_existing_fact(self):
        fid = self.store.store_fact("p", "keep me")["id"]
        for bad in self.BAD_IDS:
            with self.assertRaises(FactError, msg=repr(bad)):
                self.store.archive_fact(bad)
            with self.assertRaises(FactError, msg=repr(bad)):
                self.store.recall_facts("p", fact_id=bad)
            with self.assertRaises(FactError, msg=repr(bad)):
                self.store.scour_archives("p", fact_id=bad)
            with self.assertRaises(FactError, msg=repr(bad)):
                self.store.store_fact("p", "replacement", supersedes=bad)
        self.assertNotIn("archived_at", self.store.get_fact(fid))
        self.assertEqual([f["id"] for f in self.store.recall_facts("p")], [fid])

    def test_integral_float_and_digit_string_ids_are_accepted(self):
        fid = self.store.store_fact("p", "x")["id"]
        for alias in (fid, float(fid), str(fid)):
            self.assertEqual(self.store.recall_facts("p", fact_id=alias)[0]["id"], fid)
        self.assertEqual(self.store.archive_fact(str(fid))["id"], fid)
        self.assertEqual(self.store.scour_archives("p", fact_id=str(fid))[0]["id"], fid)

    def test_bad_supersede_target_leaves_no_trace(self):
        fid = self.store.store_fact("p", "original")["id"]
        before = self.row_counts()
        with self.assertRaises(FactError):
            self.store.store_fact("p", "replacement", supersedes=fid + 0.5)
        self.assertEqual(self.row_counts(), before)
        self.assertNotIn("archived_at", self.store.get_fact(fid))


class FilterValidationTests(StoreTestCase):
    """Filters are validated as strictly as store_fact validates its inputs."""

    def test_tags_filter_must_be_a_list_of_strings(self):
        self.store.store_fact("p", "x", tags=["repo"])
        for bad in ("repo", {"repo": 1}, {"repo"}, [1], [None], [""], ["  "]):
            with self.assertRaises(FactError, msg=repr(bad)):
                self.store.recall_facts("p", tags=bad)
            with self.assertRaises(FactError, msg=repr(bad)):
                self.store.scour_archives("p", tags=bad)

    def test_empty_tags_filter_is_no_filter(self):
        self.store.store_fact("p", "x", tags=["repo"])
        self.assertEqual(len(self.store.recall_facts("p", tags=[])), 1)
        self.assertEqual(self.store.scour_archives("p", tags=[]), [])

    def test_valid_kinds_round_trip_through_filters(self):
        for kind in KINDS:
            self.store.store_fact("p", f"{kind} fact", kind=kind)
        for kind in KINDS:
            got = self.store.recall_facts("p", kind=kind)
            self.assertEqual([f["kind"] for f in got], [kind])

    def test_kind_filter_rejects_falsy_and_unknown_values(self):
        self.store.store_fact("p", "x")
        for bad in ("", "   ", 0, False, 5, ["fact"], {}, "note"):
            with self.assertRaises(FactError, msg=repr(bad)):
                self.store.recall_facts("p", kind=bad)
            with self.assertRaises(FactError, msg=repr(bad)):
                self.store.scour_archives("p", kind=bad)

    def test_limit_validated_on_recall_and_scour(self):
        for bad in (0, -1, 1.5, "5", True):
            with self.assertRaises(FactError, msg=repr(bad)):
                self.store.recall_facts("p", limit=bad)
            with self.assertRaises(FactError, msg=repr(bad)):
                self.store.scour_archives("p", limit=bad)

    def test_min_confidence_zero_is_a_filter_not_unset(self):
        self.store.store_fact("p", "zero", confidence=0.0)
        self.store.store_fact("p", "half", confidence=0.5)
        self.assertEqual(
            {f["text"] for f in self.store.recall_facts("p", min_confidence=0.0)},
            {"zero", "half"},
        )
        self.assertEqual(
            [f["text"] for f in self.store.recall_facts("p", min_confidence=0.4)],
            ["half"],
        )
        self.assertEqual(
            self.store.recall_facts("p", min_confidence=1.0), []
        )

    def test_min_confidence_validated(self):
        for bad in (-0.1, 1.01, float("nan"), float("inf"), "abc", True, 10 ** 400):
            with self.assertRaises(FactError, msg=repr(bad)):
                self.store.recall_facts("p", min_confidence=bad)
            with self.assertRaises(FactError, msg=repr(bad)):
                self.store.scour_archives("p", min_confidence=bad)


class LimitOrderTests(StoreTestCase):
    def test_tags_filter_applies_before_limit(self):
        tagged = [
            self.store.store_fact("p", f"tagged {i}", tags=["repo"])["id"]
            for i in range(2)
        ]
        for i in range(8):
            self.store.store_fact("p", f"untagged {i}")
        got = self.store.recall_facts("p", tags=["repo"], limit=1)
        self.assertEqual([f["id"] for f in got], [tagged[-1]])

    def test_scour_defaults_to_fifty_and_honours_limit(self):
        ids = [self.store.store_fact("p", f"f{i}")["id"] for i in range(55)]
        for fid in ids:
            self.store.archive_fact(fid)
        self.assertEqual(len(self.store.scour_archives("p")), 50)
        self.assertEqual(
            [f["id"] for f in self.store.scour_archives("p", limit=2)],
            [ids[-1], ids[-2]],
        )

    def test_scour_query_respects_limit(self):
        ids = [self.store.store_fact("p", "needle here")["id"] for _ in range(3)]
        for fid in ids:
            self.store.archive_fact(fid)
        got = self.store.scour_archives("p", query="needle", limit=1)
        self.assertEqual([f["id"] for f in got], [ids[-1]])


class OrderingTests(StoreTestCase):
    def test_scour_orders_by_archive_time_not_creation_id(self):
        with mock.patch("store_n_recall.store.utc_now", new=FixedClock()):
            a = self.store.store_fact("p", "a")["id"]
            b = self.store.store_fact("p", "b")["id"]
            c = self.store.store_fact("p", "c")["id"]
            for fid in (b, a, c):
                self.store.archive_fact(fid)
        self.assertEqual([f["id"] for f in self.store.scour_archives("p")], [c, a, b])

    def test_recall_ties_break_by_newest_id(self):
        with mock.patch("store_n_recall.store.utc_now", new=FrozenClock()):
            a = self.store.store_fact("p", "a")["id"]
            b = self.store.store_fact("p", "b")["id"]
        self.assertEqual([f["id"] for f in self.store.recall_facts("p")], [b, a])

    def test_scour_ties_break_by_newest_id(self):
        with mock.patch("store_n_recall.store.utc_now", new=FrozenClock()):
            a = self.store.store_fact("p", "a")["id"]
            b = self.store.store_fact("p", "b")["id"]
            self.store.archive_fact(a)
            self.store.archive_fact(b)
        self.assertEqual([f["id"] for f in self.store.scour_archives("p")], [b, a])

    def test_archive_time_is_never_before_creation_time(self):
        fid = self.store.store_fact("p", "x")["id"]
        archived = self.store.archive_fact(fid)
        self.assertGreaterEqual(archived["archived_at"], archived["created_at"])


class ScopeIsolationTests(StoreTestCase):
    def test_ids_are_scoped_to_the_requested_project(self):
        active = self.store.store_fact("proj-a", "active in a")["id"]
        archived = self.store.store_fact("proj-a", "archived in a")["id"]
        self.store.archive_fact(archived)
        with self.assertRaises(FactError) as cm:
            self.store.recall_facts("proj-b", fact_id=active)
        self.assertIn("no active fact", str(cm.exception))
        with self.assertRaises(FactError) as cm:
            self.store.scour_archives("proj-b", fact_id=archived)
        self.assertIn("no archived fact", str(cm.exception))
        # the audit path still resolves the record; it is not a recall surface
        self.assertEqual(self.store.get_fact(archived)["project"], "proj-a")

    def test_id_lookup_stays_scoped_when_a_query_is_given(self):
        fid = self.store.store_fact("proj-a", "needle")["id"]
        self.assertEqual(
            len(self.store.recall_facts("proj-a", fact_id=fid, query="needle")), 1
        )
        with self.assertRaises(FactError):
            self.store.recall_facts("proj-b", fact_id=fid, query="needle")


class HostileInputTests(StoreTestCase):
    def test_sql_metacharacters_round_trip_and_do_not_execute(self):
        text = "'; DROP TABLE facts; -- 100% _wild_ \\ path"
        fid = self.store.store_fact("p", text)["id"]
        self.assertEqual(self.store.get_fact(fid)["text"], text)
        self.assertEqual(self.row_counts()[0], 1)  # facts table intact
        self.assertEqual([f["text"] for f in self.store.recall_facts("p")], [text])

    def test_sql_metacharacters_in_query_are_data(self):
        self.store.store_fact("p", "coverage is at 100% now")
        self.store.store_fact("p", "unrelated")
        got = self.store.recall_facts("p", query="100%")
        self.assertEqual([f["text"] for f in got], ["coverage is at 100% now"])

    def test_project_names_with_quotes_are_scope_safe(self):
        weird = "proj'; DROP TABLE facts; --"
        fid = self.store.store_fact(weird, "scoped fact")["id"]
        self.assertEqual(self.store.recall_facts("proj"), [])
        self.assertEqual([f["id"] for f in self.store.recall_facts(weird)], [fid])
        self.assertEqual(self.row_counts()[0], 1)

    def test_multiline_text_round_trip(self):
        text = "line one\nline two\t--flag\n" + "x" * 5_000
        fid = self.store.store_fact("p", text)["id"]
        self.assertEqual(self.store.get_fact(fid)["text"], text)
        self.assertEqual(len(self.store.recall_facts("p", query="flag")), 1)

    def test_long_text_is_capped_with_an_actionable_error(self):
        self.store.store_fact("p", "x" * MAX_FACT_CHARS)  # at the cap: fine
        with self.assertRaises(FactError) as cm:
            self.store.store_fact("p", "x" * (MAX_FACT_CHARS + 1))
        self.assertIn("too long", str(cm.exception))
        self.assertIn(str(MAX_FACT_CHARS), str(cm.exception))
        self.assertEqual(self.row_counts(), (1, 1))

    def test_unicode_text_tags_and_project_round_trip(self):
        project = "项目-α"
        text = "Сборка требует флаг --offline 😀"
        fid = self.store.store_fact(
            project, text, tags=["сборка"], kind="gotcha")["id"]
        record = self.store.get_fact(fid)
        self.assertEqual((record["project"], record["text"]), (project, text))
        self.assertEqual(
            [f["id"] for f in self.store.recall_facts(project, tags=["сборка"])], [fid]
        )
        self.assertEqual([p["project"] for p in self.store.list_projects()], [project])


class TagSemanticsTests(StoreTestCase):
    def test_tags_stored_sorted_and_deduped_from_tuple(self):
        fact = self.store.store_fact("p", "x", tags=(" b ", "a", "b"))
        self.assertEqual(fact["tags"], ["a", "b"])

    def test_tags_compared_exactly_and_case_sensitively(self):
        lower = self.store.store_fact("p", "lower", tags=["repo"])["id"]
        self.store.store_fact("p", "upper", tags=["Repo"])
        self.assertEqual(
            [f["id"] for f in self.store.recall_facts("p", tags=["repo"])], [lower]
        )

    def test_duplicate_filter_tags_are_harmless(self):
        fid = self.store.store_fact("p", "x", tags=["repo"])["id"]
        self.assertEqual(
            [f["id"] for f in self.store.recall_facts("p", tags=["repo", "repo"])],
            [fid],
        )

    def test_repeated_tags_count_once_in_list_projects(self):
        self.store.store_fact("p", "x", tags=["repo", "repo", "repo"])
        self.assertEqual(self.store.list_projects()[0]["top_tags"], ["repo"])


class RecordShapeTests(StoreTestCase):
    def test_full_record_keys_and_source_fields(self):
        fid = self.store.store_fact("p", "x", source="a.md")["id"]
        self.store.archive_fact(fid, note="done")
        record = self.store.get_fact(fid)
        self.assertEqual(
            set(record),
            {"id", "project", "kind", "text", "tags", "confidence",
             "created_at", "archived_at", "sources"},
        )
        stored, archived = record["sources"]
        self.assertEqual(set(stored), {"agent", "at", "action", "pointer"})
        self.assertEqual(set(archived), {"agent", "at", "action", "note"})
        self.assertEqual((stored["action"], archived["action"]), ("stored", "archived"))

    def test_summary_rows_carry_only_defined_fields(self):
        self.store.store_fact("p", "active", source="a.md")
        row = self.store.recall_facts("p")[0]
        self.assertNotIn("archived_at", row)
        self.assertNotIn("sources", row)
        self.assertEqual(
            set(row),
            {"id", "project", "kind", "text", "tags", "confidence", "created_at"},
        )


class ArchiveEventTests(StoreTestCase):
    def test_failed_second_archive_appends_nothing(self):
        fid = self.store.store_fact("p", "x")["id"]
        self.store.archive_fact(fid)
        before = self.row_counts()
        with self.assertRaises(FactError):
            self.store.archive_fact(fid)
        self.assertEqual(self.row_counts(), before)
        self.assertEqual(
            [s["action"] for s in self.store.get_fact(fid)["sources"]],
            ["stored", "archived"],
        )

    def test_archived_at_is_stable_across_reads(self):
        fid = self.store.store_fact("p", "x")["id"]
        archived = self.store.archive_fact(fid)
        self.assertEqual(
            self.store.get_fact(fid)["archived_at"], archived["archived_at"]
        )


class TagFilterShapeTests(StoreTestCase):
    def test_tuple_filter_tags_are_accepted(self):
        fid = self.store.store_fact("p", "x", tags=["repo", "build"])["id"]
        self.assertEqual(
            [f["id"] for f in self.store.recall_facts("p", tags=("repo", "build"))],
            [fid],
        )


class ListProjectsEdgeTests(StoreTestCase):
    def test_latest_activity_is_tracked_per_project(self):
        self.store.store_fact("a", "x")
        self.store.store_fact("b", "y")
        by_name = {p["project"]: p for p in self.store.list_projects()}
        self.assertGreaterEqual(
            by_name["b"]["latest_activity"], by_name["a"]["latest_activity"]
        )

    def test_project_without_tags_has_empty_top_tags(self):
        self.store.store_fact("p", "x")
        self.assertEqual(self.store.list_projects()[0]["top_tags"], [])


class PersistenceTests(unittest.TestCase):
    def _db_path(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return os.path.join(tmp.name, "nested", "facts.db")

    def test_state_survives_reopen(self):
        db = self._db_path()
        first = Store(db)
        fid = first.store_fact("p", "durable", tags=["repo"], source="docs/1.md")["id"]
        first.archive_fact(fid, note="retired")
        second = Store(db)
        record = second.get_fact(fid)
        self.assertEqual(record["text"], "durable")
        self.assertEqual(record["tags"], ["repo"])
        self.assertEqual([s["action"] for s in record["sources"]],
                         ["stored", "archived"])
        self.assertEqual(record["sources"][0]["pointer"], "docs/1.md")
        self.assertEqual(record["sources"][1]["note"], "retired")
        self.assertEqual(second.recall_facts("p"), [])
        self.assertEqual([f["id"] for f in second.scour_archives("p")], [fid])

    def test_reopening_never_duplicates_state(self):
        db = self._db_path()
        fid = Store(db).store_fact("p", "x")["id"]
        for _ in range(3):
            reopened = Store(db)
            self.assertEqual([f["id"] for f in reopened.recall_facts("p")], [fid])
            self.assertEqual(reopened.list_projects()[0]["active"], 1)

    def test_pathlib_path_is_accepted(self):
        db = pathlib.Path(self._db_path())
        store = Store(db)
        store.store_fact("p", "x")
        self.assertTrue(db.exists())
        self.assertEqual(len(store.recall_facts("p")), 1)


@unittest.skipUnless(FTS, "FTS5 not available on this sqlite3")
class FtsMirrorTests(StoreTestCase):
    def test_mirror_backfills_facts_stored_without_fts(self):
        legacy_db = os.path.join(self.tmp.name, "legacy.db")
        legacy = Store(legacy_db, use_fts=False)
        fid = legacy.store_fact("p", "legacy fact about caching")["id"]
        modern = Store(legacy_db)
        self.assertTrue(modern.fts)
        self.assertEqual(
            [f["id"] for f in modern.recall_facts("p", query="caching")], [fid]
        )

    def test_backfill_keeps_archive_visibility_intact(self):
        legacy_db = os.path.join(self.tmp.name, "legacy-archived.db")
        legacy = Store(legacy_db, use_fts=False)
        fid = legacy.store_fact("p", "retired caching fact")["id"]
        legacy.archive_fact(fid)
        modern = Store(legacy_db)
        self.assertEqual(modern.recall_facts("p", query="caching"), [])
        self.assertEqual(
            [f["id"] for f in modern.scour_archives("p", query="caching")], [fid]
        )

    def test_mirror_rows_are_not_duplicated_by_reconnects(self):
        self.store.store_fact("p", "needle once")
        for _ in range(3):
            self.assertEqual(len(self.store.recall_facts("p", query="needle")), 1)
        conn = sqlite3.connect(self.db)
        try:
            n = conn.execute("SELECT COUNT(*) FROM facts_fts").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(n, 1)

    def test_scour_query_orders_by_archive_time_not_relevance(self):
        with mock.patch("store_n_recall.store.utc_now", new=FixedClock()):
            # the keyword-heavy fact would win on relevance but was archived first
            old = self.store.store_fact("p", "needle " * 20)["id"]
            new = self.store.store_fact("p", "needle appears once")["id"]
            self.store.archive_fact(old)
            self.store.archive_fact(new)
        got = self.store.scour_archives("p", query="needle")
        self.assertEqual([f["id"] for f in got], [new, old])

    def test_fts_operator_syntax_is_inert(self):
        self.store.store_fact("p", "alpha beta gamma")
        for query in ("NEAR(alpha beta)", "alpha AND beta", "(", "alpha*"):
            self.assertIsInstance(self.store.recall_facts("p", query=query), list)
        self.assertEqual(len(self.store.recall_facts("p", query="alpha*")), 1)
        self.assertEqual(self.store.recall_facts("p", query="NEAR(alpha beta)"), [])


class IdRangeTests(StoreTestCase):
    """Ids beyond SQLite's signed 64-bit range are rejections, not crashes."""

    def test_ids_above_the_sqlite_range_are_fact_errors(self):
        fid = self.store.store_fact("p", "x")["id"]
        for bad in (2 ** 63, 2 ** 64, 10 ** 30, "9" * 30):
            for call in (
                lambda: self.store.archive_fact(bad),
                lambda: self.store.recall_facts("p", fact_id=bad),
                lambda: self.store.scour_archives("p", fact_id=bad),
                lambda: self.store.store_fact("p", "y", supersedes=bad),
            ):
                with self.assertRaises(FactError, msg=repr(bad)) as cm:
                    call()
                self.assertIn("out of range", str(cm.exception))
        self.assertNotIn("archived_at", self.store.get_fact(fid))

    def test_largest_valid_id_is_a_clean_lookup(self):
        with self.assertRaises(FactError) as cm:
            self.store.recall_facts("p", fact_id=MAX_FACT_ID)
        self.assertIn("no active fact", str(cm.exception))


class StoreFailureModeTests(StoreTestCase):
    """Infrastructure failures surface loudly and leave committed data intact."""

    def test_corrupt_store_file_raises_database_error(self):
        bad = os.path.join(self.tmp.name, "corrupt.db")
        with open(bad, "wb") as fh:
            fh.write(b"this is not a sqlite database" * 64)
        with self.assertRaises(sqlite3.DatabaseError):
            Store(bad).store_fact("p", "x")

    def test_locked_store_fails_loudly_then_recovers(self):
        fid = self.store.store_fact("p", "committed")["id"]
        holder = sqlite3.connect(self.db, timeout=0.2)
        self.addCleanup(holder.close)
        holder.execute("PRAGMA journal_mode=WAL")
        holder.execute("BEGIN IMMEDIATE")
        holder.execute(
            "INSERT INTO facts (project, kind, text, tags, confidence, created_at)"
            " VALUES ('p', 'fact', 'uncommitted', '[]', 0.5,"
            " '2026-01-01T00:00:00.000+00:00')")
        with mock.patch("store_n_recall.store.BUSY_TIMEOUT_MS", 50):
            with self.assertRaises(sqlite3.OperationalError) as cm:
                Store(self.db).store_fact("p", "blocked write")
        self.assertIn("locked", str(cm.exception).lower())
        holder.rollback()
        # nothing partial survived, and writes work again after the lock clears
        self.assertEqual([f["id"] for f in self.store.recall_facts("p")], [fid])
        new = self.store.store_fact("p", "after recovery")
        self.assertIn(new["id"], {f["id"] for f in self.store.recall_facts("p")})

    def test_unrelated_tables_in_the_file_are_left_untouched(self):
        conn = sqlite3.connect(self.db)
        try:
            conn.execute("CREATE TABLE notes (body TEXT)")
            conn.execute("INSERT INTO notes VALUES ('keep me')")
            conn.commit()
        finally:
            conn.close()
        self.store.store_fact("p", "x")
        self.store.archive_fact(1)
        conn = sqlite3.connect(self.db)
        try:
            rows = [r[0] for r in conn.execute("SELECT body FROM notes")]
        finally:
            conn.close()
        self.assertEqual(rows, ["keep me"])


class ProvenanceEdgeTests(StoreTestCase):
    def test_archive_uses_the_store_default_agent(self):
        store = Store(self.db, default_agent="cfg-agent")
        fid = store.store_fact("p", "x")["id"]
        self.assertEqual(
            store.archive_fact(fid)["sources"][-1]["agent"], "cfg-agent")

    def test_empty_agent_falls_back_to_the_store_default(self):
        store = Store(self.db, default_agent="cfg-agent")
        fact = store.store_fact("p", "x", agent="")
        self.assertEqual(fact["sources"][0]["agent"], "cfg-agent")

    def test_supersede_records_session_on_both_events(self):
        old = self.store.store_fact("p", "old", agent="a1", session="s1")["id"]
        self.store.store_fact("p", "new", supersedes=old, agent="a2", session="s2")
        events = self.store.get_fact(old)["sources"]
        self.assertEqual(
            [(e["action"], e["agent"], e["session"]) for e in events],
            [("stored", "a1", "s1"), ("archived", "a2", "s2")],
        )

    def test_archive_is_id_addressed_not_project_scoped(self):
        # archive_fact takes a globally unique id and no project, so an id from
        # another project retires that project's fact, not a same-numbered one.
        other = self.store.store_fact("proj-b", "fact in b")["id"]
        self.store.archive_fact(other)
        self.assertEqual(
            [f["id"] for f in self.store.scour_archives("proj-b")], [other])
        self.assertEqual(self.store.scour_archives("proj-a"), [])

    def test_text_with_embedded_nul_round_trips(self):
        text = "before\x00after\ttab"
        fid = self.store.store_fact("p", text)["id"]
        self.assertEqual(self.store.get_fact(fid)["text"], text)
        self.assertEqual(
            [f["id"] for f in self.store.recall_facts("p", query="before")], [fid])


def _shape(rows):
    """Comparable projection: timestamps legitimately differ between stores."""
    return [
        (r["id"], r["project"], r["kind"], r["text"], r["tags"], r["confidence"])
        for r in rows
    ]


class BackendParityTests(unittest.TestCase):
    """The LIKE fallback answers the same questions as FTS5."""

    def _pair(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return (
            Store(os.path.join(tmp.name, "fts.db")),
            Store(os.path.join(tmp.name, "like.db"), use_fts=False),
        )

    def _seed(self, store):
        ids = [
            store.store_fact("p", "alpha build facts", kind="gotcha",
                             tags=["repo"], confidence=0.9)["id"],
            store.store_fact("p", "beta test notes", kind="fact",
                             tags=["repo", "build"], confidence=0.4)["id"],
            store.store_fact("q", "alpha other project", kind="decision",
                             confidence=0.7)["id"],
        ]
        store.archive_fact(ids[1])
        return ids

    def test_no_query_results_and_filters_agree(self):
        fts, like = self._pair()
        self.assertEqual(self._seed(fts), self._seed(like))
        for kwargs in (
            {}, {"kind": "gotcha"}, {"tags": ["repo"]},
            {"min_confidence": 0.5}, {"limit": 1},
        ):
            with self.subTest(kwargs=kwargs):
                self.assertEqual(
                    _shape(fts.recall_facts("p", **kwargs)),
                    _shape(like.recall_facts("p", **kwargs)),
                )
                self.assertEqual(
                    _shape(fts.scour_archives("p", **kwargs)),
                    _shape(like.scour_archives("p", **kwargs)),
                )
        listed_fts = [(p["project"], p["active"], p["archived"], p["top_tags"])
                      for p in fts.list_projects()]
        listed_like = [(p["project"], p["active"], p["archived"], p["top_tags"])
                       for p in like.list_projects()]
        self.assertEqual(listed_fts, listed_like)

    def test_query_result_sets_agree_for_whole_words(self):
        fts, like = self._pair()
        self._seed(fts)
        self._seed(like)
        for query in ("alpha", "build", "notes", "absent"):
            with self.subTest(query=query):
                self.assertEqual(
                    {f["id"] for f in fts.recall_facts("p", query=query)},
                    {f["id"] for f in like.recall_facts("p", query=query)},
                )
                self.assertEqual(
                    {f["id"] for f in fts.scour_archives("p", query=query)},
                    {f["id"] for f in like.scour_archives("p", query=query)},
                )


@unittest.skipUnless(FTS, "FTS5 not available on this sqlite3")
class BackendDivergenceTests(unittest.TestCase):
    """Where the two backends deliberately differ (documented in README).

    FTS5 matches whole tokens and folds case + diacritics; the LIKE fallback
    matches literal substrings and folds ASCII case only. These tests pin the
    difference so it is a decision rather than an accident.
    """

    def _pair(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return (
            Store(os.path.join(tmp.name, "fts.db")),
            Store(os.path.join(tmp.name, "like.db"), use_fts=False),
        )

    def test_prefix_matches_only_on_the_like_backend(self):
        fts, like = self._pair()
        for store in (fts, like):
            store.store_fact("p", "deployment pipeline is offline")
        self.assertEqual(fts.recall_facts("p", query="deploy"), [])
        self.assertEqual(len(like.recall_facts("p", query="deploy")), 1)

    def test_diacritics_fold_only_on_the_fts_backend(self):
        fts, like = self._pair()
        for store in (fts, like):
            store.store_fact("p", "Le café est fermé")
        self.assertEqual(len(fts.recall_facts("p", query="cafe")), 1)
        self.assertEqual(like.recall_facts("p", query="cafe"), [])
        # non-ASCII case folding: FTS5 folds, SQLite LIKE does not
        self.assertEqual(len(fts.recall_facts("p", query="CAFÉ")), 1)
        self.assertEqual(like.recall_facts("p", query="CAFÉ"), [])

    def test_cjk_substrings_match_only_on_the_like_backend(self):
        fts, like = self._pair()
        for store in (fts, like):
            store.store_fact("p", "构建需要离线标志")
        self.assertEqual(fts.recall_facts("p", query="离线"), [])
        self.assertEqual(len(like.recall_facts("p", query="离线")), 1)

    def test_punctuation_only_query_matches_only_on_the_like_backend(self):
        fts, like = self._pair()
        for store in (fts, like):
            store.store_fact("p", "it works!!!")
        self.assertEqual(fts.recall_facts("p", query="!!!"), [])
        self.assertEqual(len(like.recall_facts("p", query="!!!")), 1)


@unittest.skipUnless(FTS, "FTS5 not available on this sqlite3")
class QueryLimitCompositionTests(StoreTestCase):
    def test_tags_filter_applies_before_limit_on_the_query_path(self):
        tagged = [
            self.store.store_fact("p", "needle in the build docs", tags=["repo"])["id"]
            for _ in range(3)
        ]
        for _ in range(5):
            self.store.store_fact("p", "needle in the tests")
        got = self.store.recall_facts("p", query="needle", tags=["repo"], limit=2)
        # equal relevance: newest tagged ids survive the tags filter + limit
        self.assertEqual([f["id"] for f in got], [tagged[-1], tagged[-2]])

    def test_limit_is_pushed_down_when_no_tags_filter_is_given(self):
        ids = [self.store.store_fact("p", f"fact {i}")["id"] for i in range(10)]
        got = self.store.recall_facts("p", limit=2)
        self.assertEqual([f["id"] for f in got], [ids[-1], ids[-2]])
        self.assertEqual(
            [f["id"] for f in self.store.recall_facts("p", query="fact", limit=2)],
            [ids[-1], ids[-2]],
        )


class ModelStore:
    """In-memory mirror of what a scenario test has created and retired."""

    def __init__(self):
        self.facts = {}      # id -> content fields
        self.created = {}    # id -> creation sequence
        self.archived = {}   # id -> archive sequence
        self.seq = 0

    def add(self, record):
        self.seq += 1
        self.created[record["id"]] = self.seq
        self.facts[record["id"]] = {
            key: record[key]
            for key in ("project", "kind", "text", "tags", "confidence", "created_at")
        }

    def retire(self, fact_id):
        self.seq += 1
        self.archived[fact_id] = self.seq

    def active_ids(self):
        return [i for i in self.facts if i not in self.archived]

    def archived_ids(self):
        return list(self.archived)


class RandomizedScenarioTests(unittest.TestCase):
    """Seeded operation sequences checked against an in-memory model.

    Every store, supersede, archive, recall, scour, and rejected call is
    verified against the model on both the FTS5 and the LIKE backend:
    row counts, active/archived sets, ordering, filters, and content.
    """

    SEEDS = (1, 2, 3)
    STEPS = 120

    def _open(self, use_fts):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return Store(os.path.join(tmp.name, "facts.db"), use_fts=use_fts)

    def test_fts_backend_scenarios(self):
        for seed in self.SEEDS:
            with self.subTest(seed=seed):
                self._scenario(use_fts=True, seed=seed)

    def test_like_backend_scenarios(self):
        for seed in self.SEEDS:
            with self.subTest(seed=seed):
                self._scenario(use_fts=False, seed=seed)

    # -- driver -------------------------------------------------------------

    def _scenario(self, *, use_fts, seed):
        store = self._open(use_fts)
        store.recall_facts("alpha")  # create the schema before counting rows
        model = ModelStore()
        rng = random.Random(seed)
        with mock.patch("store_n_recall.store.utc_now", new=FixedClock()):
            for step in range(self.STEPS):
                op = rng.choices(
                    ("store", "supersede", "archive", "recall", "scour", "lookup", "rejected"),
                    weights=(5, 2, 2, 3, 3, 2, 1),
                )[0]
                project = rng.choice(("alpha", "beta"))
                if op == "store":
                    model.add(store.store_fact(
                        project,
                        f"w{step:04d} statement",
                        kind=rng.choice(KINDS),
                        tags=rng.sample(["repo", "build", "docs"], rng.randint(0, 2)),
                        confidence=rng.choice((0.0, 0.25, 0.5, 1.0)),
                    ))
                elif op == "supersede" and model.active_ids():
                    target = rng.choice(model.active_ids())
                    project = model.facts[target]["project"]
                    replacement = store.store_fact(
                        project, f"w{step:04d} correction", supersedes=target)
                    model.retire(target)
                    model.add(replacement)
                elif op == "archive" and model.active_ids():
                    target = rng.choice(model.active_ids())
                    store.archive_fact(target)
                    model.retire(target)
                elif op == "rejected":
                    self._check_rejections(store, model, project)
                elif op == "recall":
                    self._check_recall(store, model, project, rng)
                elif op == "scour":
                    self._check_scour(store, model, project, rng)
                elif op == "lookup" and model.facts:
                    self._check_lookup(store, model, rng)
                self._check_invariants(store, model)
        self._check_contents(store, model)
        self._check_projects(store, model)

    # -- expectations -------------------------------------------------------

    def _expected(self, ids, model, project, *, kind, tags, min_confidence, query):
        picked = []
        for fid in ids:
            fact = model.facts[fid]
            if fact["project"] != project:
                continue
            if kind is not None and fact["kind"] != kind:
                continue
            if tags and not set(tags) <= set(fact["tags"]):
                continue
            if min_confidence is not None and fact["confidence"] < min_confidence:
                continue
            if query is not None and query not in fact["text"]:
                continue
            picked.append(fid)
        return picked

    def _filters(self, model, rng, pool):
        query = None
        if pool and rng.random() < 0.5:
            query = model.facts[rng.choice(pool)]["text"].split()[0]
        return {
            "kind": rng.choice((None, rng.choice(KINDS))),
            "tags": rng.sample(["repo", "build", "docs"], rng.randint(0, 2)),
            "min_confidence": rng.choice((None, 0.0, 0.25, 0.5, 1.0)),
            # a query is answered with an unlimited set: which subset survives a
            # limit is backend-defined (rank vs newest), so only the set matters
            "limit": 1000 if query else rng.choice((None, 1, 4, 1000)),
            "query": query,
        }

    def _check_recall(self, store, model, project, rng):
        filters = self._filters(model, rng, model.active_ids())
        filters_for_model = dict(filters)
        del filters_for_model["limit"]
        got = store.recall_facts(project, **filters)
        expected = self._expected(
            model.active_ids(), model, project, **filters_for_model)
        if filters["query"] is None:
            expected.sort(key=lambda i: model.created[i], reverse=True)
            expected = expected[: filters["limit"] or len(expected)]
            self.assertEqual([f["id"] for f in got], expected)
        else:
            self.assertEqual(sorted(f["id"] for f in got), sorted(expected))

    def _check_scour(self, store, model, project, rng):
        filters = self._filters(model, rng, model.archived_ids())
        filters_for_model = dict(filters)
        del filters_for_model["limit"]
        got = store.scour_archives(project, **filters)
        expected = self._expected(
            model.archived_ids(), model, project, **filters_for_model)
        expected.sort(key=lambda i: model.archived[i], reverse=True)
        expected = expected[: filters["limit"] or len(expected)]
        self.assertEqual([f["id"] for f in got], expected)

    def _check_rejections(self, store, model, project):
        before = self._row_counts(store.path)
        if model.archived_ids():
            target = model.archived_ids()[0]
            with self.assertRaises(FactError):
                store.archive_fact(target)
            with self.assertRaises(FactError):
                store.store_fact(project, "should not land", supersedes=target)
        with self.assertRaises(FactError):
            store.archive_fact(MAX_FACT_ID)
        with self.assertRaises(FactError):
            store.store_fact(project, "should not land", supersedes=MAX_FACT_ID)
        self.assertEqual(self._row_counts(store.path), before)

    def _check_lookup(self, store, model, rng):
        fid = rng.choice(list(model.facts))
        project = model.facts[fid]["project"]
        other = "beta" if project == "alpha" else "alpha"
        if fid in model.archived:
            got = store.scour_archives(project, fact_id=fid, limit=1)
            self.assertEqual([f["id"] for f in got], [fid])
            with self.assertRaises(FactError):
                store.recall_facts(project, fact_id=fid)
        else:
            got = store.recall_facts(project, fact_id=fid, limit=1)
            self.assertEqual([f["id"] for f in got], [fid])
            with self.assertRaises(FactError):
                store.scour_archives(project, fact_id=fid)
        # the id is invisible to recall from any other project, either way
        with self.assertRaises(FactError):
            store.recall_facts(other, fact_id=fid)
        with self.assertRaises(FactError):
            store.scour_archives(other, fact_id=fid)

    # -- invariants ---------------------------------------------------------

    @staticmethod
    def _row_counts(db_path):
        conn = sqlite3.connect(db_path)
        try:
            return (
                conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0],
                conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0],
            )
        finally:
            conn.close()

    @staticmethod
    def _indexed_docs(db_path):
        conn = sqlite3.connect(db_path)
        try:
            return conn.execute("SELECT COUNT(*) FROM facts_fts_docsize").fetchone()[0]
        finally:
            conn.close()

    def _check_invariants(self, store, model):
        facts_n, sources_n = self._row_counts(store.path)
        self.assertEqual(facts_n, len(model.facts))          # never deleted
        self.assertEqual(sources_n, len(model.facts) + len(model.archived))
        if store.fts:
            # the insert-only mirror indexes exactly the fact rows
            self.assertEqual(self._indexed_docs(store.path), len(model.facts))
        active = set()
        archived = set()
        for project in ("alpha", "beta"):
            active |= {f["id"] for f in store.recall_facts(project, limit=1000)}
            archived |= {f["id"] for f in store.scour_archives(project, limit=1000)}
        self.assertEqual(active, set(model.active_ids()))
        self.assertEqual(archived, set(model.archived_ids()))

    def _check_contents(self, store, model):
        for fid, expected in model.facts.items():
            record = store.get_fact(fid)
            for key in ("project", "kind", "text", "tags", "confidence", "created_at"):
                self.assertEqual(record[key], expected[key], f"fact {fid} {key}")
            times = [s["at"] for s in record["sources"]]
            self.assertEqual(times, sorted(times), f"fact {fid} source times")
            actions = [s["action"] for s in record["sources"]]
            if fid in model.archived:
                self.assertEqual(actions, ["stored", "archived"])
            else:
                self.assertEqual(actions, ["stored"])

    def _check_projects(self, store, model):
        listed = {p["project"]: p for p in store.list_projects()}
        for project in ("alpha", "beta"):
            ids = [i for i in model.facts if model.facts[i]["project"] == project]
            if not ids:
                self.assertNotIn(project, listed)
                continue
            self.assertEqual(listed[project]["active"],
                             sum(1 for i in ids if i not in model.archived))
            self.assertEqual(listed[project]["archived"],
                             sum(1 for i in ids if i in model.archived))


class FtsIndexInvariantTests(StoreTestCase):
    """The mirror indexes every fact exactly once, and heals when it can't."""

    @staticmethod
    def _indexed(db_path):
        conn = sqlite3.connect(db_path)
        try:
            return conn.execute("SELECT COUNT(*) FROM facts_fts_docsize").fetchone()[0]
        finally:
            conn.close()

    @unittest.skipUnless(FTS, "FTS5 not available on this sqlite3")
    def test_index_tracks_store_archive_and_supersede(self):
        a = self.store.store_fact("p", "first indexed fact")["id"]
        b = self.store.store_fact("p", "second indexed fact")["id"]
        self.assertEqual(self._indexed(self.db), 2)
        self.store.archive_fact(a)
        self.store.store_fact("p", "replacement fact", supersedes=b)
        # insert-only: archived rows stay indexed, supersede adds one
        self.assertEqual(self._indexed(self.db), 3)
        self.assertEqual(len(self.store.scour_archives("p", query="indexed")), 2)
        self.assertEqual(len(self.store.recall_facts("p", query="indexed")), 0)

    @unittest.skipUnless(FTS, "FTS5 not available on this sqlite3")
    def test_unbuilt_mirror_is_repaired_on_next_open(self):
        # Simulate the half-finished state older code could leave behind: the
        # table and trigger exist but the index was never backfilled.
        broken_db = os.path.join(self.tmp.name, "broken.db")
        legacy = Store(broken_db, use_fts=False)
        fid = legacy.store_fact("p", "legacy fact about caching")["id"]
        conn = sqlite3.connect(broken_db)
        try:
            conn.execute(_FTS_TABLE)
            conn.execute(_FTS_TRIGGER)
            conn.commit()
            self.assertEqual(self._indexed(broken_db), 0)
        finally:
            conn.close()
        healed = Store(broken_db)
        self.assertEqual(
            [f["id"] for f in healed.recall_facts("p", query="caching")], [fid])
        self.assertEqual(self._indexed(broken_db), 1)

    @unittest.skipUnless(FTS, "FTS5 not available on this sqlite3")
    def test_empty_mirror_with_empty_store_needs_no_rebuild(self):
        store = Store(os.path.join(self.tmp.name, "empty.db"))
        store.recall_facts("p")  # opens the connection, creates schema
        self.assertEqual(self._indexed(os.path.join(self.tmp.name, "empty.db")), 0)
        store.store_fact("p", "first fact")
        self.assertEqual(self._indexed(os.path.join(self.tmp.name, "empty.db")), 1)

    @unittest.skipUnless(FTS, "FTS5 not available on this sqlite3")
    def test_broken_mirror_is_recreated_rather_than_fatal(self):
        db = os.path.join(self.tmp.name, "broken-mirror.db")
        fid = Store(db).store_fact("p", "alpha fact about caching")["id"]
        conn = sqlite3.connect(db)
        try:
            conn.execute("DROP TABLE facts_fts_docsize")
            conn.commit()
        finally:
            conn.close()
        healed = Store(db)  # must repair, not raise
        self.assertEqual(
            [f["id"] for f in healed.recall_facts("p", query="caching")], [fid])
        conn = sqlite3.connect(db)
        try:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM facts_fts_docsize").fetchone()[0], 1)
        finally:
            conn.close()


class StoreInputEdgeTests(StoreTestCase):
    """Transport-level shapes and filesystem states the tools may meet."""

    def test_zero_byte_store_file_is_initialized(self):
        empty = os.path.join(self.tmp.name, "zero.db")
        open(empty, "wb").close()
        store = Store(empty)
        store.store_fact("p", "x")
        self.assertEqual(len(store.recall_facts("p")), 1)

    def test_path_that_is_a_directory_fails_loudly(self):
        directory = os.path.join(self.tmp.name, "adir")
        os.makedirs(directory)
        with self.assertRaises(sqlite3.OperationalError):
            Store(directory).store_fact("p", "x")

    def test_unwritable_parent_directory_fails_loudly(self):
        if os.geteuid() == 0:
            self.skipTest("root ignores directory permissions")
        blocked = os.path.join(self.tmp.name, "blocked")
        os.makedirs(blocked)
        os.chmod(blocked, 0o500)
        self.addCleanup(os.chmod, blocked, 0o700)
        with self.assertRaises(PermissionError):
            Store(os.path.join(blocked, "nested", "facts.db")).store_fact("p", "x")

    def test_non_string_project_and_query_shapes(self):
        for name, call in (
            ("project", lambda v: self.store.recall_facts(v)),
            ("project", lambda v: self.store.scour_archives(v)),
            ("project", lambda v: self.store.store_fact(v, "x")),
            ("query", lambda v: self.store.recall_facts("p", query=v)),
            ("query", lambda v: self.store.scour_archives("p", query=v)),
        ):
            for bad in (True, 5, 3.5, [], {}, object()):
                with self.assertRaises(FactError, msg=f"{name} {bad!r}"):
                    call(bad)

    def test_non_string_source_and_note_shapes(self):
        fid = self.store.store_fact("p", "x")["id"]
        for bad in (True, 5, 3.5, [], {}):
            with self.assertRaises(FactError, msg=repr(bad)):
                self.store.store_fact("p", "x", source=bad)
            with self.assertRaises(FactError, msg=repr(bad)):
                self.store.archive_fact(fid, note=bad)

    def test_many_tags_round_trip_and_rank(self):
        tags = [f"t{i:02d}" for i in range(40)]
        fact = self.store.store_fact("p", "x", tags=tags)
        self.assertEqual(fact["tags"], sorted(tags))
        self.assertEqual(
            [f["id"] for f in self.store.recall_facts("p", tags=tags)], [fact["id"]]
        )
        top = self.store.list_projects()[0]["top_tags"]
        self.assertEqual(top, sorted(tags)[:5])  # ties by name, five max


class FtsReadinessProbeTests(unittest.TestCase):
    def test_missing_docsize_table_means_not_ready(self):
        conn = sqlite3.connect(":memory:")
        try:
            conn.executescript(
                "CREATE TABLE facts (id INTEGER PRIMARY KEY, text TEXT NOT NULL);"
                + _FTS_TABLE + ";" + _FTS_TRIGGER
            )
            conn.execute("DROP TABLE facts_fts_docsize")
            # an unqueryable index is never "ready"
            self.assertFalse(_fts_ready(conn))
        finally:
            conn.close()

    def test_partially_indexed_mirror_is_not_ready(self):
        conn = sqlite3.connect(":memory:")
        try:
            conn.executescript(
                "CREATE TABLE facts (id INTEGER PRIMARY KEY, text TEXT NOT NULL);"
                + _FTS_TABLE + ";" + _FTS_TRIGGER
            )
            conn.execute("INSERT INTO facts (text) VALUES ('alpha')")
            conn.execute("INSERT INTO facts (text) VALUES ('beta')")
            self.assertTrue(_fts_ready(conn))  # trigger indexed both
            # simulate a mirror that is non-empty but missing a document
            conn.execute("DELETE FROM facts_fts_docsize WHERE rowid = 2")
            self.assertFalse(_fts_ready(conn))
        finally:
            conn.close()


_LEGACY_SCHEMA = """
CREATE TABLE facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project TEXT NOT NULL,
    kind TEXT NOT NULL,
    text TEXT NOT NULL,
    tags TEXT NOT NULL,
    confidence REAL NOT NULL,
    created_at TEXT NOT NULL,
    archived_at TEXT
);
CREATE TABLE sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fact_id INTEGER NOT NULL REFERENCES facts(id),
    action TEXT NOT NULL,
    agent TEXT NOT NULL,
    session TEXT,
    at TEXT NOT NULL,
    note TEXT,
    pointer TEXT
);
"""


class SchemaMigrationTests(unittest.TestCase):
    """Stores written before versioning are upgraded in place, data intact."""

    def _legacy_db(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = os.path.join(tmp.name, "legacy.db")
        conn = sqlite3.connect(db)
        try:
            conn.executescript(_LEGACY_SCHEMA)
            conn.execute(
                "INSERT INTO facts (project, kind, text, tags, confidence, created_at)"
                " VALUES ('p', 'fact', 'ancient fact about caching', '[]', 0.5,"
                " '2026-01-01T00:00:00.000+00:00')")
            conn.execute(
                "INSERT INTO sources (fact_id, action, agent, at)"
                " VALUES (1, 'stored', 'old-agent',"
                " '2026-01-01T00:00:00.000+00:00')")
            conn.commit()
        finally:
            conn.close()
        return db

    def test_old_store_is_upgraded_in_place(self):
        db = self._legacy_db()
        store = Store(db)
        record = store.get_fact(1)
        self.assertEqual(record["text"], "ancient fact about caching")
        self.assertEqual(record["sources"][0]["agent"], "old-agent")
        conn = sqlite3.connect(db)
        try:
            self.assertEqual(
                conn.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
            columns = {r[1] for r in conn.execute("PRAGMA table_info(facts)")}
            triggers = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'")}
        finally:
            conn.close()
        self.assertIn("supersedes", columns)
        self.assertIn("facts_no_delete", triggers)
        # the new column is usable and the old fact is now searchable
        replacement = store.store_fact("p", "corrected caching claim", supersedes=1)
        self.assertEqual(replacement["supersedes"], 1)
        self.assertEqual(
            [f["id"] for f in store.scour_archives("p", query="caching")], [1])

    def test_migration_is_idempotent(self):
        db = self._legacy_db()
        for _ in range(3):
            Store(db).recall_facts("p")
        conn = sqlite3.connect(db)
        try:
            self.assertEqual(
                conn.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0], 1)
        finally:
            conn.close()

    def test_failed_migration_rolls_back_cleanly(self):
        db = self._legacy_db()

        class _FailOnVersionSet:
            """Real connection, but the version bump fails after the ALTER."""

            def __init__(self, conn):
                self.conn = conn

            def executescript(self, sql):
                return self.conn.executescript(sql)

            def execute(self, sql, *args):
                if str(sql).startswith("PRAGMA user_version ="):
                    raise sqlite3.OperationalError("simulated migration failure")
                return self.conn.execute(sql, *args)

        conn = sqlite3.connect(db)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                _init_schema(_FailOnVersionSet(conn))
            conn.rollback()
            columns = {r[1] for r in conn.execute("PRAGMA table_info(facts)")}
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            rows = conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
        finally:
            conn.close()
        self.assertNotIn("supersedes", columns)  # the ALTER was rolled back
        self.assertEqual(version, 0)
        self.assertEqual(rows, 1)
        # and a later open migrates cleanly
        upgraded = Store(db)
        self.assertEqual(upgraded.get_fact(1)["text"], "ancient fact about caching")


class DatabaseGuardTests(StoreTestCase):
    """The database itself enforces the no-delete / immutable / append-only
    invariants, so an external writer or a future bug cannot rewrite history."""

    def _raw(self):
        conn = sqlite3.connect(self.db)
        self.addCleanup(conn.close)
        return conn

    def test_facts_cannot_be_deleted(self):
        self.store.store_fact("p", "x")
        conn = self._raw()
        with self.assertRaises(sqlite3.IntegrityError) as cm:
            conn.execute("DELETE FROM facts")
        self.assertIn("never deleted", str(cm.exception))

    def test_fact_content_cannot_be_updated(self):
        fid = self.store.store_fact("p", "x", tags=["repo"])["id"]
        conn = self._raw()
        for column, value in (
            ("project", "other"), ("kind", "decision"), ("text", "rewritten"),
            ("tags", "[]"), ("confidence", 0.9),
            ("created_at", "2020-01-01T00:00:00.000+00:00"), ("supersedes", 1),
        ):
            with self.assertRaises(sqlite3.IntegrityError, msg=column):
                conn.execute(f"UPDATE facts SET {column} = ? WHERE id = ?", (value, fid))
        self.assertEqual(self.store.get_fact(fid)["text"], "x")

    def test_sources_are_append_only(self):
        fid = self.store.store_fact("p", "x")["id"]
        conn = self._raw()
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("UPDATE sources SET agent = 'forged' WHERE fact_id = ?", (fid,))
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM sources WHERE fact_id = ?", (fid,))

    def test_insert_guards_reject_out_of_contract_rows(self):
        self.store.store_fact("p", "warm")  # create the schema first
        conn = self._raw()
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO facts (project, kind, text, tags, confidence, created_at)"
                " VALUES ('p', 'rumour', 'x', '[]', 0.5, '2026-01-01T00:00:00.000+00:00')")
        conn.rollback()
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO facts (project, kind, text, tags, confidence, created_at)"
                " VALUES ('p', 'fact', 'x', '[]', 2.0, '2026-01-01T00:00:00.000+00:00')")
        conn.rollback()
        fid = self.store.store_fact("p", "real")["id"]
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO sources (fact_id, action, agent, at)"
                " VALUES (?, 'edited', 'x', '2026-01-01T00:00:00.000+00:00')", (fid,))



@unittest.skipUnless(FTS, "FTS5 not available on this sqlite3")
class PartialMirrorHealTests(StoreTestCase):
    def test_partial_index_is_backfilled_on_open(self):
        # A build that created the mirror without a backfill, then stored one
        # fact: the index is non-empty (so "not empty" is not enough) but three
        # facts are missing.
        db = os.path.join(self.tmp.name, "partial.db")
        legacy = Store(db, use_fts=False)
        ids = [legacy.store_fact("p", f"legacy {word}")["id"]
               for word in ("alpha", "bravo", "charlie")]
        conn = sqlite3.connect(db)
        try:
            conn.execute(_FTS_TABLE)
            conn.execute(_FTS_TRIGGER)
            conn.execute(
                "INSERT INTO facts (project, kind, text, tags, confidence, created_at)"
                " VALUES ('p', 'fact', 'delta new', '[]', 0.5,"
                " '2026-01-01T00:00:00.000+00:00')")
            conn.commit()
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM facts_fts_docsize").fetchone()[0], 1)
        finally:
            conn.close()

        healed = Store(db)
        for word in ("alpha", "bravo", "charlie", "delta"):
            with self.subTest(word=word):
                self.assertEqual(len(healed.recall_facts("p", query=word)), 1)
        conn = sqlite3.connect(db)
        try:
            facts = conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
            indexed = conn.execute(
                "SELECT COUNT(*) FROM facts_fts_docsize").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(facts, len(ids) + 1)
        self.assertEqual(indexed, facts)


class NormalizationTests(StoreTestCase):
    def test_source_and_note_are_stripped(self):
        fact = self.store.store_fact("p", "x", source="  docs/x.md  ")
        self.assertEqual(fact["sources"][0]["pointer"], "docs/x.md")
        archived = self.store.archive_fact(fact["id"], note="  stale reason  ")
        self.assertEqual(archived["sources"][-1]["note"], "stale reason")

    def test_agent_is_stripped_and_blank_falls_back(self):
        store = Store(self.db, default_agent="cfg-agent")
        self.assertEqual(
            store.store_fact("p", "x", agent="  named  ")["sources"][0]["agent"],
            "named")
        self.assertEqual(
            store.store_fact("p", "y", agent="   ")["sources"][0]["agent"],
            "cfg-agent")

    def test_session_is_stripped_and_blank_is_absent(self):
        fact = self.store.store_fact("p", "x", session="  sess  ")
        self.assertEqual(fact["sources"][0]["session"], "sess")
        blank = self.store.store_fact("p", "y", session="   ")
        self.assertNotIn("session", blank["sources"][0])

    def test_non_string_agent_and_session_are_rejected(self):
        for bad in (5, True, [], {}):
            with self.assertRaises(FactError, msg=repr(bad)):
                self.store.store_fact("p", "x", agent=bad)
            with self.assertRaises(FactError, msg=repr(bad)):
                self.store.store_fact("p", "x", session=bad)
            fid = self.store.store_fact("p", "y")["id"]
            with self.assertRaises(FactError, msg=repr(bad)):
                self.store.archive_fact(fid, agent=bad)


class SupersedeProvenanceTests(StoreTestCase):
    def test_links_are_recorded_on_both_sides(self):
        old = self.store.store_fact("p", "old claim")["id"]
        new = self.store.store_fact("p", "new claim", supersedes=old)
        self.assertEqual(new["supersedes"], old)
        self.assertEqual(new["sources"][0]["note"], f"supersedes fact {old}")
        old_events = self.store.get_fact(old)["sources"]
        self.assertEqual(old_events[-1]["action"], "archived")
        self.assertEqual(old_events[-1]["note"], f"superseded by fact {new['id']}")

    def test_plain_facts_carry_no_supersedes_field(self):
        fact = self.store.store_fact("p", "standalone")
        self.assertNotIn("supersedes", fact)
        self.assertNotIn("supersedes", self.store.recall_facts("p")[0])

    def test_supersedes_is_present_on_summary_rows_once_used(self):
        old = self.store.store_fact("p", "old")["id"]
        self.store.store_fact("p", "new", supersedes=old)
        row = self.store.recall_facts("p")[0]
        self.assertEqual(row["supersedes"], old)


class GetFactValidationTests(StoreTestCase):
    """`get_fact` is the audit accessor: it must never crash the driver or
    alias another fact, exactly like the filter paths."""

    def test_bad_ids_are_fact_errors(self):
        fid = self.store.store_fact("p", "x")["id"]
        for bad in (0, -1, 1.5, True, "x", None, 2 ** 63, Decimal("1.5"), Fraction(3, 2)):
            with self.assertRaises(FactError, msg=repr(bad)):
                self.store.get_fact(bad)
        self.assertEqual(self.store.get_fact(fid)["id"], fid)

    def test_integral_numeric_and_digit_ids_are_accepted(self):
        fid = self.store.store_fact("p", "x")["id"]
        for alias in (fid, float(fid), str(fid), Decimal(fid), Fraction(fid, 1)):
            self.assertEqual(self.store.get_fact(alias)["id"], fid)

    def test_decimal_and_fraction_filters_do_not_truncate(self):
        fid = self.store.store_fact("p", "x")["id"]
        self.assertEqual(
            self.store.recall_facts("p", fact_id=Decimal(fid))[0]["id"], fid)
        with self.assertRaises(FactError):
            self.store.recall_facts("p", fact_id=Decimal(fid) + Decimal("0.5"))
        with self.assertRaises(FactError):
            self.store.scour_archives("p", fact_id=Fraction(2 * fid + 1, 2))


if __name__ == "__main__":
    unittest.main()
