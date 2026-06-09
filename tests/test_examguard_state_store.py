import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path


BACKEND_DIR = (
    Path(__file__).resolve().parents[1] / "apps" / "examguard" / "backend"
)
sys.path.insert(0, str(BACKEND_DIR))

from state_store import SQLiteStateStore, create_state_store


class SQLiteStateStoreTests(unittest.TestCase):
    def test_factory_uses_sqlite_without_database_url(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = create_state_store(
                sqlite_path=str(Path(temp_dir) / "state.sqlite3")
            )
            self.assertIsInstance(store, SQLiteStateStore)
            store.close()

    def test_round_trip_persists_exam_students_and_sessions(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "state.sqlite3"
            store = SQLiteStateStore(str(path))
            store.save(
                {"active": True, "mode": "coding"},
                {"42": {"id": "42", "name": "Ada", "status": "active"}},
                {"secret-token": "42"},
            )
            store.close()

            reopened = SQLiteStateStore(str(path))
            state = reopened.load()
            reopened.close()

            self.assertTrue(state["exam_state"]["active"])
            self.assertEqual(state["exam_state"]["mode"], "coding")
            self.assertEqual(state["students"]["42"]["name"], "Ada")
            self.assertEqual(state["student_sessions"]["secret-token"], "42")

    def test_missing_state_returns_empty_dict(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteStateStore(str(Path(temp_dir) / "state.sqlite3"))
            self.assertEqual(store.load(), {})
            store.close()

    def test_latest_snapshot_replaces_previous_snapshot(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteStateStore(str(Path(temp_dir) / "state.sqlite3"))
            store.save({"active": True}, {"1": {"id": "1"}}, {"old": "1"})
            store.save({"active": False}, {}, {})
            state = store.load()
            store.close()

            self.assertFalse(state["exam_state"]["active"])
            self.assertEqual(state["students"], {})
            self.assertEqual(state["student_sessions"], {})

    def test_factory_forwards_postgres_retry_settings(self):
        import state_store

        sentinel = object()
        with patch.object(
            state_store, "PostgresStateStore", return_value=sentinel
        ) as postgres_store:
            result = create_state_store(
                database_url="postgresql://example",
                connect_attempts=5,
                retry_delay=1.5,
                connect_timeout=3,
            )

        self.assertIs(result, sentinel)
        postgres_store.assert_called_once_with(
            "postgresql://example",
            connect_attempts=5,
            retry_delay=1.5,
            connect_timeout=3,
        )

    def test_postgres_store_rejects_bare_hostname(self):
        import state_store

        with self.assertRaisesRegex(
            RuntimeError,
            "tam PostgreSQL bağlantı URLsi",
        ):
            state_store.PostgresStateStore("postgres.railway.internal")


if __name__ == "__main__":
    unittest.main()
