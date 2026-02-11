import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from treasure_tool import (
    TreasureHuntEngine,
    derive_hints,
    derive_keywords,
    extract_candidate_clues,
    parse_sitemap_urls,
)


class TreasureToolTests(unittest.TestCase):
    def test_init_and_add_clue(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = TreasureHuntEngine(Path(tmp))
            engine.init_workspace()
            clue = engine.add_clue(
                text="Follow the stone road",
                hints=["path"],
                keywords=["stone", "road"],
            )
            self.assertEqual(clue["id"], "clue-2")
            clues = engine.load_clues()
            self.assertEqual(len(clues), 2)

    def test_run_writes_attempts(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = TreasureHuntEngine(Path(tmp))
            engine.init_workspace()
            found = engine.run(rounds=1, per_clue=5, sleep_seconds=0, checker_command=None)
            self.assertFalse(found)
            lines = engine.attempts_file.read_text(encoding="utf-8").strip().splitlines()
            self.assertGreater(len(lines), 0)
            payload = json.loads(lines[0])
            self.assertIn("guess", payload)

    @patch("treasure_tool.fetch_text")
    @patch.object(TreasureHuntEngine, "discover_treasure_quest_urls")
    def test_ingest_treasure_quest_creates_clues(self, mock_discover, mock_fetch_text):
        mock_discover.return_value = ["https://treasure.quest/"]
        mock_fetch_text.return_value = """
            <html><body>
            <h1>Treasure Quest</h1>
            <p>Clue: Find the stone bridge where shadows point north?</p>
            <p>Hint: Look near the river gate at dawn.</p>
            </body></html>
        """
        with tempfile.TemporaryDirectory() as tmp:
            engine = TreasureHuntEngine(Path(tmp))
            engine.init_workspace()
            result = engine.ingest_treasure_quest("https://treasure.quest/")
            self.assertEqual(result["pages_scanned"], 1)
            self.assertGreater(result["clues_created"], 0)
            clues = engine.load_clues()
            self.assertGreaterEqual(len(clues), 2)

    @patch("treasure_tool.fetch_text")
    def test_parse_sitemap_urls(self, mock_fetch_text):
        mock_fetch_text.return_value = (
            "<urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>"
            "<url><loc>https://treasure.quest/a</loc></url>"
            "<url><loc>https://treasure.quest/b</loc></url>"
            "</urlset>"
        )
        urls = parse_sitemap_urls("https://treasure.quest/sitemap.xml")
        self.assertEqual(urls, ["https://treasure.quest/a", "https://treasure.quest/b"])

    def test_text_derivation_helpers(self):
        text = "Find the stone bridge near the north gate and decode the old marker"
        keywords = derive_keywords(text)
        hints = derive_hints(text)
        clues = extract_candidate_clues("Clue: Find the old gate at sunrise?\nIgnore")
        self.assertIn("stone", keywords)
        self.assertIn("north", hints)
        self.assertGreater(len(clues), 0)


if __name__ == "__main__":
    unittest.main()
