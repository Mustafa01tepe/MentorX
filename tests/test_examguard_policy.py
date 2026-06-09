import sys
import unittest
from collections import deque
from pathlib import Path


BACKEND_DIR = (
    Path(__file__).resolve().parents[1] / "apps" / "examguard" / "backend"
)
sys.path.insert(0, str(BACKEND_DIR))

from policy import record_coding_vote, resolve_student_session


class SessionPolicyTests(unittest.TestCase):
    def test_valid_token_and_matching_student_are_accepted(self):
        sessions = {"token": "student-1"}
        self.assertEqual(
            resolve_student_session("token", "student-1", sessions),
            "student-1",
        )

    def test_claimed_student_cannot_use_another_students_token(self):
        sessions = {"token": "student-1"}
        self.assertIsNone(
            resolve_student_session("token", "student-2", sessions)
        )

    def test_missing_token_is_rejected(self):
        self.assertIsNone(resolve_student_session("", "student-1", {}))


class CodingVotePolicyTests(unittest.TestCase):
    def test_suspicion_requires_two_votes_in_three_samples(self):
        votes = deque(maxlen=3)
        self.assertFalse(record_coding_vote(votes, True))
        self.assertFalse(record_coding_vote(votes, False))
        self.assertTrue(record_coding_vote(votes, True))

    def test_old_votes_fall_out_of_the_window(self):
        votes = deque([True, True, False], maxlen=3)
        self.assertFalse(record_coding_vote(votes, False))
        self.assertEqual(list(votes), [True, False, False])


if __name__ == "__main__":
    unittest.main()
