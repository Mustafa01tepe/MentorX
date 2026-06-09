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
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS evidence_history (
                event_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                metadata TEXT NOT NULL,
                image BLOB NOT NULL,
                mime_type TEXT NOT NULL
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

    def save_evidence(self, event_id, created_at, metadata, image, mime_type):
        payload = json.dumps(metadata, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO evidence_history
                    (event_id, created_at, metadata, image, mime_type)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(event_id) DO UPDATE SET
                    created_at = excluded.created_at,
                    metadata = excluded.metadata,
                    image = excluded.image,
                    mime_type = excluded.mime_type
                """,
                (event_id, created_at, payload, image, mime_type),
            )
            self._connection.commit()

    def list_evidence(self, limit=200, exam_id=None):
        safe_limit = max(1, min(int(limit), 1000))
        with self._lock:
            if exam_id is None:
                rows = self._connection.execute(
                    """
                    SELECT event_id, created_at, metadata, mime_type
                    FROM evidence_history
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (safe_limit,),
                ).fetchall()
            else:
                rows = self._connection.execute(
                    """
                    SELECT event_id, created_at, metadata, mime_type
                    FROM evidence_history
                    WHERE json_extract(metadata, '$.examId') = ?
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (exam_id, safe_limit),
                ).fetchall()
        return [
            {
                "eventId": row[0],
                "createdAt": row[1],
                "metadata": json.loads(row[2]),
                "mimeType": row[3],
            }
            for row in rows
        ]

    def get_evidence_image(self, event_id):
        with self._lock:
            row = self._connection.execute(
                """
                SELECT image, mime_type
                FROM evidence_history
                WHERE event_id = ?
                """,
                (event_id,),
            ).fetchone()
        return (row[0], row[1]) if row else None

    def get_evidence(self, event_id):
        with self._lock:
            row = self._connection.execute(
                """
                SELECT event_id, created_at, metadata, image, mime_type
                FROM evidence_history
                WHERE event_id = ?
                """,
                (event_id,),
            ).fetchone()
        if not row:
            return None
        return {
            "eventId": row[0],
            "createdAt": row[1],
            "metadata": json.loads(row[2]),
            "image": row[3],
            "mimeType": row[4],
        }


class PostgresStateStore:
    def __init__(
        self,
        database_url,
        connect_attempts=8,
        retry_delay=2,
        connect_timeout=5,
    ):
        normalized_url = str(database_url or '').strip()
        if not normalized_url.startswith(('postgres://', 'postgresql://')):
            raise RuntimeError(
                'DATABASE_URL tam PostgreSQL bağlantı URLsi olmalıdır; '
                'yalnızca host adı kullanılamaz.'
            )
        try:
            import psycopg
            from psycopg.types.json import Jsonb
        except ImportError as exc:
            raise RuntimeError(
                "PostgreSQL için psycopg paketi kurulu olmalıdır."
            ) from exc

        self.database_url = normalized_url
        self._psycopg = psycopg
        self._jsonb = Jsonb
        self.connect_attempts = max(1, int(connect_attempts))
        self.retry_delay = max(0, float(retry_delay))
        self.connect_timeout = max(1, int(connect_timeout))
        self._initialize()

    def _connect(self):
        return self._psycopg.connect(
            self.database_url,
            autocommit=True,
            connect_timeout=self.connect_timeout,
        )

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
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS evidence_history (
                        event_id TEXT PRIMARY KEY,
                        created_at TIMESTAMPTZ NOT NULL,
                        metadata JSONB NOT NULL,
                        image BYTEA NOT NULL,
                        mime_type TEXT NOT NULL
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

    def save_evidence(self, event_id, created_at, metadata, image, mime_type):
        def save_row():
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO evidence_history
                        (event_id, created_at, metadata, image, mime_type)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (event_id) DO UPDATE SET
                        created_at = EXCLUDED.created_at,
                        metadata = EXCLUDED.metadata,
                        image = EXCLUDED.image,
                        mime_type = EXCLUDED.mime_type
                    """,
                    (event_id, created_at, self._jsonb(metadata), image, mime_type),
                )

        self._with_retry(save_row)

    def list_evidence(self, limit=200, exam_id=None):
        safe_limit = max(1, min(int(limit), 1000))

        def load_rows():
            with self._connect() as connection:
                if exam_id is not None:
                    return connection.execute(
                        """
                        SELECT event_id, created_at, metadata, mime_type
                        FROM evidence_history
                        WHERE metadata ->> 'examId' = %s
                        ORDER BY created_at DESC
                        LIMIT %s
                        """,
                        (exam_id, safe_limit),
                    ).fetchall()
                return connection.execute(
                    """
                    SELECT event_id, created_at, metadata, mime_type
                    FROM evidence_history
                    ORDER BY created_at DESC
                    LIMIT %s
                    """,
                    (safe_limit,),
                ).fetchall()

        return [
            {
                "eventId": row[0],
                "createdAt": row[1].isoformat(),
                "metadata": row[2],
                "mimeType": row[3],
            }
            for row in self._with_retry(load_rows)
        ]

    def get_evidence_image(self, event_id):
        def load_row():
            with self._connect() as connection:
                return connection.execute(
                    """
                    SELECT image, mime_type
                    FROM evidence_history
                    WHERE event_id = %s
                    """,
                    (event_id,),
                ).fetchone()

        row = self._with_retry(load_row)
        return (bytes(row[0]), row[1]) if row else None

    def get_evidence(self, event_id):
        def load_row():
            with self._connect() as connection:
                return connection.execute(
                    """
                    SELECT event_id, created_at, metadata, image, mime_type
                    FROM evidence_history
                    WHERE event_id = %s
                    """,
                    (event_id,),
                ).fetchone()

        row = self._with_retry(load_row)
        if not row:
            return None
        return {
            "eventId": row[0],
            "createdAt": row[1].isoformat(),
            "metadata": row[2],
            "image": bytes(row[3]),
            "mimeType": row[4],
        }


def create_state_store(
    database_url="",
    sqlite_path="examguard_state.sqlite3",
    connect_attempts=8,
    retry_delay=2,
    connect_timeout=5,
):
    if database_url:
        return PostgresStateStore(
            database_url,
            connect_attempts=connect_attempts,
            retry_delay=retry_delay,
            connect_timeout=connect_timeout,
        )
    return SQLiteStateStore(sqlite_path)
