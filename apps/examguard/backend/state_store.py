import json
import os
import sqlite3
import threading
import time


def build_payload(exam_state, students, student_sessions):
    return {
        "exam_state": exam_state,
        "students": students,
        "student_sessions": student_sessions,
    }


class SQLiteStateStore:
    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS app_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                payload TEXT NOT NULL
            )
            """
        )
        self._connection.commit()

    def load(self):
        with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM app_state WHERE id = 1"
            ).fetchone()
        if not row:
            return {}
        try:
            data = json.loads(row[0])
        except (TypeError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def save(self, exam_state, students, student_sessions):
        payload = json.dumps(
            build_payload(exam_state, students, student_sessions),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO app_state (id, payload)
                VALUES (1, ?)
                ON CONFLICT(id) DO UPDATE SET payload = excluded.payload
                """,
                (payload,),
            )
            self._connection.commit()

    def close(self):
        with self._lock:
            self._connection.close()


class PostgresStateStore:
    def __init__(self, database_url, connect_attempts=8, retry_delay=2):
        try:
            import psycopg
            from psycopg.types.json import Jsonb
        except ImportError as exc:
            raise RuntimeError(
                "PostgreSQL için psycopg paketi kurulu olmalıdır."
            ) from exc

        self.database_url = database_url
        self._psycopg = psycopg
        self._jsonb = Jsonb
        self.connect_attempts = max(1, int(connect_attempts))
        self.retry_delay = max(0, float(retry_delay))
        self._initialize()

    def _connect(self):
        return self._psycopg.connect(self.database_url, autocommit=True)

    def _with_retry(self, operation):
        last_error = None
        for attempt in range(1, self.connect_attempts + 1):
            try:
                return operation()
            except self._psycopg.OperationalError as exc:
                last_error = exc
                if attempt == self.connect_attempts:
                    break
                print(
                    '[ExamGuard] PostgreSQL henüz hazır değil; '
                    f'{attempt}/{self.connect_attempts} bağlantı denemesi başarısız.'
                )
                time.sleep(self.retry_delay)

        raise last_error

    def _initialize(self):
        def initialize():
            with self._connect() as connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS app_state (
                        id SMALLINT PRIMARY KEY CHECK (id = 1),
                        payload JSONB NOT NULL,
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                    """
                )

        try:
            self._with_retry(initialize)
        except self._psycopg.OperationalError as exc:
            raise RuntimeError(
                'PostgreSQL bağlantısı kurulamadı. Railway DATABASE_URL '
                'değişkeninin PostgreSQL servisine referans verdiğini kontrol edin.'
            ) from exc

    def load(self):
        def load_row():
            with self._connect() as connection:
                return connection.execute(
                    "SELECT payload FROM app_state WHERE id = 1"
                ).fetchone()

        row = self._with_retry(load_row)
        if not row:
            return {}
        return row[0] if isinstance(row[0], dict) else {}

    def save(self, exam_state, students, student_sessions):
        payload = build_payload(exam_state, students, student_sessions)

        def save_payload():
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO app_state (id, payload, updated_at)
                    VALUES (1, %s, NOW())
                    ON CONFLICT (id) DO UPDATE
                    SET payload = EXCLUDED.payload, updated_at = NOW()
                    """,
                    (self._jsonb(payload),),
                )

        self._with_retry(save_payload)

    def close(self):
        return None


def create_state_store(
    database_url="",
    sqlite_path="examguard_state.sqlite3",
    connect_attempts=8,
    retry_delay=2,
):
    if database_url:
        return PostgresStateStore(
            database_url,
            connect_attempts=connect_attempts,
            retry_delay=retry_delay,
        )
    return SQLiteStateStore(sqlite_path)
