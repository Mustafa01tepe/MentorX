import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DesktopRequirementsTests(unittest.TestCase):
    def test_socketio_and_engineio_are_compatible_pins(self):
        requirements = (
            ROOT / "apps" / "examguard" / "desktop" / "requirements.txt"
        ).read_text(encoding="utf-8")

        self.assertIn("python-socketio[client]==5.11.2", requirements)
        self.assertIn("python-engineio==4.9.1", requirements)


if __name__ == "__main__":
    unittest.main()
