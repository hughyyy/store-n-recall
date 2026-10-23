"""Concurrency: the store must survive real concurrent writers on one file.

WAL + busy timeout mean parallel processes (the separate agent sessions each
spawn their own server process against the same store) must not corrupt the
file or lose committed facts. Threads share nothing: each worker opens its own
Store, exactly like separate processes would.
"""

import multiprocessing
import os
import sqlite3
import tempfile
import time
import unittest
from unittest import mock

from store_n_recall.store import Store, _fts_available


WORKERS = 4
FACTS_PER_WORKER = 25


def _worker_store(db_path: str, worker_id: int, count: int) -> int:
    """Store `count` facts; return how many survived a concurrent read-back."""
    store = Store(db_path)
    for i in range(count):
        store.store_fact(
            f"proj-{worker_id}",
            f"fact {i} from worker {worker_id}",
            tags=["concurrency", f"w{worker_id}"],
        )
    return len(store.recall_facts(f"proj-{worker_id}"))


def _worker_contend(db_path: str, op: str, target_id: int) -> str:
    """Race to retire `target_id`; exactly one caller may win."""
    from store_n_recall.config import FactError
    store = Store(db_path)
    try:
        if op == "supersede":
            store.store_fact("proj", "replacement fact", supersedes=target_id)
        else:
            store.archive_fact(target_id)
        return "won"
    except FactError:
        return "lost"


def _worker_write_many(db_path: str, project: str, count: int) -> int:
    store = Store(db_path)
    for i in range(count):
        store.store_fact(project, f"concurrent fact {i}")
    return count


def _worker_supersede_own(db_path: str, fact_id: int) -> str:
    """Supersede a fact no other worker touches; every caller must win."""
    from store_n_recall.config import FactError
    store = Store(db_path)
    try:
        store.store_fact("proj", f"replacement for {fact_id}", supersedes=fact_id)
        return "won"
    except FactError:
        return "lost"

def _worker_open_legacy(db_path: str, worker_id: int, barrier, out) -> None:
    """Open a LIKE-written store with FTS enabled and store one more fact."""
    barrier.wait()  # all first opens race for the backfill
    store = Store(db_path)
    store.store_fact("p", f"new w{worker_id:03d} fact")
    out.put((worker_id, len(store.recall_facts("p", query="legacy", limit=1000))))


def _worker_mixed(db_path: str, use_fts: bool, project: str, count: int) -> int:
    """Write with one backend; a mixed fleet shares one store file."""
    store = Store(db_path, use_fts=use_fts)
    for i in range(count):
        store.store_fact(project, f"mixed {project} fact {i}")
    return len(store.recall_facts(project, limit=1000))



class ConcurrentWritersTests(unittest.TestCase):
    def test_many_writers_one_file_no_loss_no_corruption(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "shared", "facts.db")
            ctx = multiprocessing.get_context("spawn")
            with ctx.Pool(WORKERS) as pool:
                results = pool.starmap(
                    _worker_store,
                    [(db, w, FACTS_PER_WORKER) for w in range(WORKERS)],
                )
            self.assertEqual(results, [FACTS_PER_WORKER] * WORKERS)

            checker = Store(db)
            for w in range(WORKERS):
                facts = checker.recall_facts(f"proj-{w}", limit=100)
                self.assertEqual(len(facts), FACTS_PER_WORKER)
                texts = {f["text"] for f in facts}
                self.assertEqual(len(texts), FACTS_PER_WORKER)
            projects = {p["project"] for p in checker.list_projects()}
            self.assertEqual(projects, {f"proj-{w}" for w in range(WORKERS)})
            # totals: 4 workers x 25 facts, none lost, none duplicated
            total = sum(p["active"] for p in checker.list_projects())
            self.assertEqual(total, WORKERS * FACTS_PER_WORKER)

    def test_store_is_usable_while_another_store_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "facts.db")
            writer = Store(db)
            reader = Store(db)
            writer.store_fact("p", "first")
            # concurrent write + read on separate connections
            reader.recall_facts("p")
            writer.store_fact("p", "second")
            self.assertEqual(len(reader.recall_facts("p")), 2)

    def test_concurrent_supersedes_have_exactly_one_winner(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "facts.db")
            target = Store(db).store_fact("proj", "stale claim")["id"]
            ctx = multiprocessing.get_context("spawn")
            with ctx.Pool(8) as pool:
                results = pool.starmap(
                    _worker_contend, [(db, "supersede", target)] * 8
                )
            self.assertEqual(results.count("won"), 1)
            self.assertEqual(results.count("lost"), 7)
            checker = Store(db)
            record = checker.get_fact(target)
            # archived exactly once: one archived provenance event, no repeats
            self.assertEqual(
                [s["action"] for s in record["sources"]], ["stored", "archived"]
            )
            replacements = checker.recall_facts("proj")
            self.assertEqual(len(replacements), 1)  # losers stored nothing

    def test_concurrent_archives_have_exactly_one_winner(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "facts.db")
            target = Store(db).store_fact("proj", "doomed fact")["id"]
            ctx = multiprocessing.get_context("spawn")
            with ctx.Pool(8) as pool:
                results = pool.starmap(
                    _worker_contend, [(db, "archive", target)] * 8
                )
            self.assertEqual(results.count("won"), 1)
            self.assertEqual(results.count("lost"), 7)
            checker = Store(db)
            record = checker.get_fact(target)
            self.assertEqual(
                [s["action"] for s in record["sources"]], ["stored", "archived"]
            )
            self.assertEqual(checker.recall_facts("proj"), [])


class ConcurrencyInvariantTests(unittest.TestCase):
    def test_reads_during_writes_never_fail_or_lose_facts(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "facts.db")
            Store(db).store_fact("p", "seed")
            ctx = multiprocessing.get_context("spawn")
            proc = ctx.Process(target=_worker_write_many, args=(db, "p", 120))
            proc.start()
            counts = []
            try:
                reader = Store(db)
                while proc.is_alive():
                    counts.append(len(reader.recall_facts("p", limit=1000)))
                    time.sleep(0.002)
            finally:
                proc.join(60)
            self.assertFalse(proc.is_alive())
            self.assertEqual(proc.exitcode, 0)
            self.assertEqual(counts, sorted(counts))  # facts only ever appear
            self.assertEqual(len(reader.recall_facts("p", limit=1000)), 121)

    def test_parallel_supersedes_on_distinct_facts_all_land(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "facts.db")
            store = Store(db)
            targets = [store.store_fact("proj", f"stale {i}")["id"] for i in range(8)]
            ctx = multiprocessing.get_context("spawn")
            with ctx.Pool(8) as pool:
                results = pool.starmap(
                    _worker_supersede_own, [(db, target) for target in targets]
                )
            self.assertEqual(results, ["won"] * 8)
            checker = Store(db)
            for target in targets:
                self.assertIsNotNone(checker.get_fact(target)["archived_at"])
            active = checker.recall_facts("proj", limit=100)
            self.assertEqual(len(active), 8)
            self.assertEqual(len({f["text"] for f in active}), 8)

    def test_provenance_rows_stay_consistent_under_contention(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "facts.db")
            ctx = multiprocessing.get_context("spawn")
            with ctx.Pool(WORKERS) as pool:
                pool.starmap(_worker_store, [(db, w, 10) for w in range(WORKERS)])
            checker = Store(db)
            for w in range(WORKERS):
                for fact in checker.recall_facts(f"proj-{w}", limit=100):
                    checker.archive_fact(fact["id"])
            conn = sqlite3.connect(db)
            try:
                fact_ids = {r[0] for r in conn.execute("SELECT id FROM facts")}
                stored = dict(conn.execute(
                    "SELECT fact_id, COUNT(*) FROM sources"
                    " WHERE action = 'stored' GROUP BY fact_id").fetchall())
                archived = dict(conn.execute(
                    "SELECT fact_id, COUNT(*) FROM sources"
                    " WHERE action = 'archived' GROUP BY fact_id").fetchall())
            finally:
                conn.close()
            self.assertEqual(len(fact_ids), WORKERS * 10)
            self.assertEqual(set(stored), fact_ids)      # one stored event each
            self.assertEqual(set(stored.values()), {1})
            self.assertEqual(set(archived), fact_ids)    # one archived event each
            self.assertEqual(set(archived.values()), {1})


@unittest.skipUnless(_fts_available(), "FTS5 not available on this sqlite3")
class FtsBackfillRaceTests(unittest.TestCase):
    """First open of a LIKE-written store backfills the index exactly once."""

    def test_concurrent_first_open_indexes_every_fact_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "legacy.db")
            legacy = Store(db, use_fts=False)
            for i in range(30):
                legacy.store_fact("p", f"legacy w{i:03d} fact")
            ctx = multiprocessing.get_context("spawn")
            barrier, out = ctx.Barrier(4), ctx.Queue()
            procs = [
                ctx.Process(target=_worker_open_legacy,
                            args=(db, worker, barrier, out))
                for worker in range(4)
            ]
            for proc in procs:
                proc.start()
            for proc in procs:
                proc.join(60)
            self.assertEqual([proc.exitcode for proc in procs], [0] * 4)
            counts = sorted(out.get(timeout=5)[1] for _ in range(4))
            out.close()
            self.assertEqual(counts, [30] * 4)

            checker = Store(db)
            conn = sqlite3.connect(db)
            try:
                facts = conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
                indexed = conn.execute(
                    "SELECT COUNT(*) FROM facts_fts_docsize").fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(facts, 34)      # 30 legacy + 4 concurrent writes
            self.assertEqual(indexed, facts)  # no duplicates, nothing missed
            self.assertEqual(len(checker.recall_facts("p", limit=1000)), 34)
            self.assertEqual(
                len(checker.recall_facts("p", query="legacy", limit=1000)), 30)

    def test_failed_setup_rolls_back_the_partial_mirror(self):
        # Creation, trigger and rebuild are one transaction: a failure must
        # leave no half-built mirror behind for other processes to trust.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = os.path.join(tmp.name, "legacy.db")
        legacy = Store(db, use_fts=False)
        fid = legacy.store_fact("p", "legacy fact about caching")["id"]
        with mock.patch("store_n_recall.store._FTS_TRIGGER",
                        "CREATE TRIGGER broken syntax here"):
            with self.assertRaises(sqlite3.OperationalError):
                Store(db).recall_facts("p")
        conn = sqlite3.connect(db)
        try:
            leftovers = {
                r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE name LIKE 'facts_fts%'")
            }
        finally:
            conn.close()
        self.assertEqual(leftovers, set())
        healed = Store(db)
        self.assertEqual(
            [f["id"] for f in healed.recall_facts("p", query="caching")], [fid])


@unittest.skipUnless(_fts_available(), "FTS5 not available on this sqlite3")
class MixedBackendTests(unittest.TestCase):
    """FTS-enabled and LIKE-mode processes share one file without losing facts
    or leaving the mirror incomplete."""

    def test_mixed_writers_end_with_a_complete_mirror(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "facts.db")
            ctx = multiprocessing.get_context("spawn")
            specs = [
                (db, True, "fts-a", 15),
                (db, False, "like-a", 15),
                (db, True, "fts-b", 15),
                (db, False, "like-b", 15),
            ]
            with ctx.Pool(4) as pool:
                results = pool.starmap(_worker_mixed, specs)
            self.assertEqual(results, [15] * 4)

            checker = Store(db)  # first open heals any partially built mirror
            for _, _, project, count in specs:
                with self.subTest(project=project):
                    self.assertEqual(
                        len(checker.recall_facts(project, limit=1000)), count)
                    # every fact is searchable, whichever process wrote it
                    self.assertEqual(
                        len(checker.recall_facts(
                            project, query="mixed", limit=1000)), count)
            conn = sqlite3.connect(db)
            try:
                facts = conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
                indexed = conn.execute(
                    "SELECT COUNT(*) FROM facts_fts_docsize").fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(facts, 60)
            self.assertEqual(indexed, facts)


if __name__ == "__main__":
    unittest.main()
