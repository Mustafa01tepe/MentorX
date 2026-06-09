import sys
import unittest
from pathlib import Path


BACKEND_DIR = (
    Path(__file__).resolve().parents[1] / "apps" / "examguard" / "backend"
)
sys.path.insert(0, str(BACKEND_DIR))

from agent import _parse_response


class AgentResponseTests(unittest.TestCase):
    def test_parses_suspicious_response(self):
        result = _parse_response(
            "KARAR: ŞÜPHELİ\nGEREKÇE: Ekranda bir yapay zeka aracı açık."
        )
        self.assertTrue(result["suspicious"])
        self.assertEqual(result["verdict"], "ŞÜPHELİ")

    def test_parses_clean_response(self):
        result = _parse_response(
            "KARAR: TEMİZ\nGEREKÇE: Yalnızca sınav sayfası açık."
        )
        self.assertFalse(result["suspicious"])
        self.assertEqual(result["verdict"], "TEMİZ")

    def test_invalid_response_is_not_silently_clean(self):
        result = _parse_response("Beklenmeyen model yanıtı")
        self.assertFalse(result["suspicious"])
        self.assertEqual(result["verdict"], "HATA")


if __name__ == "__main__":
    unittest.main()
