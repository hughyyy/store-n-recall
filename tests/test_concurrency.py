"""Concurrency: the store must survive real concurrent writers on one file.

WAL + busy timeout mean parallel processes (the separate agent sessions each
spawn their own server process against the same store) must not corrupt the
file or lose committed facts. Threads share nothing: each worker opens its own
Store, exactly like separate processes would.
"""

import multiprocessing
import os
import tempfile
import unittest

from store_n_recall.store import Store

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


if __name__ == "__main__":
    unittest.main()
