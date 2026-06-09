import base64
import os
import sys
import tempfile
import unittest
import io
import json
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import flask  # noqa: F401
except ImportError as exc:
    raise unittest.SkipTest(
        "ExamGuard backend bağımlılıkları bu Python ortamında kurulu değil."
    ) from exc


BACKEND_DIR = (
    Path(__file__).resolve().parents[1] / "apps" / "examguard" / "backend"
)
sys.path.insert(0, str(BACKEND_DIR))

TEMP_DIR = Path(tempfile.mkdtemp(prefix="examguard-server-tests-"))
os.environ["ADMIN_TOKEN"] = "test-admin-token"
os.environ["SCREENSHOTS_DIR"] = str(TEMP_DIR / "screenshots")
os.environ["STATE_DB_PATH"] = str(TEMP_DIR / "state.sqlite3")
os.environ.pop("DATABASE_URL", None)

import server


JPEG_DATA_URL = (
    "data:image/jpeg;base64,"
    + base64.b64encode(b"\xff\xd8\xfftest-image").decode("ascii")
)


class ExamGuardServerTests(unittest.TestCase):
    def setUp(self):
        server.exam_state.update({
            "active": True,
            "exam_id": "exam-test",
            "mode": "web",
            "duration": 90,
            "started_at": "2026-06-09T10:00:00+00:00",
            "allowed_urls": ["https://example.test/exam"],
            "exam_code": "ABC123",
        })
        server.students.clear()
        server.student_sessions.clear()
        server.students["42"] = {
            "id": "42",
            "name": "Ada",
            "connectedAt": server.now_iso(),
            "lastSeen": server.now_iso(),
            "alertCount": 0,
            "status": "active",
        }
        server.student_sessions["student-token"] = "42"
        self.client = server.app.test_client()

    def test_student_code_is_case_insensitive(self):
        response = self.client.post(
            "/student/verify",
            json={"name": "Ada", "id": "42", "code": "abc123"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["success"])

    def test_screenshot_uses_server_event_id_and_exam_metadata(self):
        response = self.client.post(
            "/screenshot",
            headers={"Authorization": "Bearer student-token"},
            json={
                "eventId": "client-controlled-id",
                "screenshot": JPEG_DATA_URL,
                "reason": "tab_switch",
                "student": {"id": "42", "name": "Fake Name"},
                "timestamp": "2026-06-09T10:05:00+00:00",
                "tabUrl": "https://example.test/forbidden",
                "tabTitle": "Forbidden",
            },
        )

        self.assertEqual(response.status_code, 200)
        rows = server.state_store.list_evidence(exam_id="exam-test")
        matching = [
            row for row in rows
            if row["metadata"].get("timestamp") == "2026-06-09T10:05:00+00:00"
        ]
        self.assertEqual(len(matching), 1)
        self.assertNotEqual(matching[0]["eventId"], "client-controlled-id")
        self.assertEqual(matching[0]["metadata"]["examCode"], "ABC123")
        self.assertEqual(matching[0]["metadata"]["student"]["name"], "Ada")

    def test_alert_history_does_not_store_session_token(self):
        response = self.client.post(
            "/alert",
            json={
                "eventId": "client-controlled-alert",
                "sessionToken": "student-token",
                "type": "ai_extension_detected",
                "timestamp": "2026-06-09T10:06:00+00:00",
                "student": {"id": "42", "name": "Fake Name"},
                "extensions": [{"blacklistName": "Example AI"}],
            },
        )

        self.assertEqual(response.status_code, 200)
        rows = server.state_store.list_evidence(exam_id="exam-test")
        matching = [
            row for row in rows
            if row["metadata"].get("timestamp") == "2026-06-09T10:06:00+00:00"
        ]
        self.assertEqual(len(matching), 1)
        metadata = matching[0]["metadata"]
        self.assertNotEqual(matching[0]["eventId"], "client-controlled-alert")
        self.assertNotIn("sessionToken", metadata)
        self.assertEqual(metadata["student"]["name"], "Ada")

    def test_history_can_be_filtered_by_exam(self):
        response = self.client.get(
            "/history?examId=exam-test",
            headers={"Authorization": "Bearer test-admin-token"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["success"])

    def test_image_less_alert_does_not_expose_image_endpoint(self):
        self.client.post(
            "/alert",
            headers={"Authorization": "Bearer student-token"},
            json={
                "type": "ai_extension_detected",
                "timestamp": "2026-06-09T10:07:00+00:00",
                "student": {"id": "42"},
            },
        )

        response = self.client.get(
            "/history?examId=exam-test",
            headers={"Authorization": "Bearer test-admin-token"},
        )
        matching = [
            item for item in response.get_json()["items"]
            if item.get("timestamp") == "2026-06-09T10:07:00+00:00"
        ]

        self.assertEqual(len(matching), 1)
        self.assertFalse(matching[0]["hasImage"])
        self.assertNotIn("evidenceId", matching[0])

    def test_stale_student_is_marked_idle(self):
        now = datetime(2026, 6, 9, 12, 0, tzinfo=timezone.utc)
        server.students["42"]["lastSeen"] = (
            now - timedelta(seconds=server.STUDENT_STALE_SECONDS + 1)
        ).isoformat()

        changed = server.refresh_stale_students(now)

        self.assertTrue(changed)
        self.assertEqual(server.students["42"]["status"], "idle")

    def test_finalized_evidence_keeps_original_exam_metadata(self):
        event_id = "late-analysis-event"
        image_path = TEMP_DIR / "late-analysis.jpg"
        image_path.write_bytes(b"\xff\xd8\xfflate")
        payload = {
            "eventId": event_id,
            "timestamp": "2026-06-09T10:08:00+00:00",
            "examId": "exam-original",
            "examCode": "ORIGINAL",
            "examStartedAt": "2026-06-09T10:00:00+00:00",
            "analysisStatus": "suspicious",
        }
        server.exam_state["exam_id"] = "exam-new"
        server.exam_state["exam_code"] = "NEW"

        server.finalize_evidence(
            payload,
            str(image_path),
            "image/jpeg",
            keep=True,
        )

        rows = server.state_store.list_evidence(exam_id="exam-original")
        matching = [row for row in rows if row["eventId"] == event_id]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]["metadata"]["examCode"], "ORIGINAL")

    def test_single_evidence_download_is_authorized_zip(self):
        self.client.post(
            "/screenshot",
            headers={"Authorization": "Bearer student-token"},
            json={
                "screenshot": JPEG_DATA_URL,
                "reason": "tab_switch",
                "student": {"id": "42"},
                "timestamp": "2026-06-09T10:09:00+00:00",
            },
        )
        row = next(
            row for row in server.state_store.list_evidence(exam_id="exam-test")
            if row["metadata"].get("timestamp") == "2026-06-09T10:09:00+00:00"
        )

        unauthorized = self.client.get(
            f"/history/{row['eventId']}/download"
        )
        response = self.client.get(
            f"/history/{row['eventId']}/download",
            headers={"Authorization": "Bearer test-admin-token"},
        )

        self.assertEqual(unauthorized.status_code, 401)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "application/zip")
        with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
            names = archive.namelist()
            self.assertTrue(any(name.endswith(".json") for name in names))
            self.assertTrue(any(name.endswith(".jpg") for name in names))

    def test_exam_archive_contains_manifest_and_evidence(self):
        self.client.post(
            "/alert",
            headers={"Authorization": "Bearer student-token"},
            json={
                "type": "ai_extension_detected",
                "timestamp": "2026-06-09T10:10:00+00:00",
                "student": {"id": "42"},
            },
        )

        response = self.client.get(
            "/history/download?examId=exam-test",
            headers={"Authorization": "Bearer test-admin-token"},
        )

        self.assertEqual(response.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
            self.assertIn("manifest.json", archive.namelist())
            manifest = json.loads(archive.read("manifest.json"))
            self.assertEqual(manifest["examId"], "exam-test")
            self.assertGreaterEqual(manifest["eventCount"], 1)


if __name__ == "__main__":
    unittest.main()
