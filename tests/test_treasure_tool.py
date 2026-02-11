import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from treasure_tool import (
    TreasureHuntEngine,
    build_checker_argv,
    derive_hints,
    derive_keywords,
    extract_candidate_clues,
    extract_text_from_pdf_bytes,
    normalize_url,
)


class TreasureToolTests(unittest.TestCase):
    def test_init_and_add_clue_with_provenance_and_fingerprint(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = TreasureHuntEngine(Path(tmp))
            engine.init_workspace()
            clue1 = engine.add_clue("Follow the stone road", ["path"], ["stone", "road"], source_kind="manual", source_ref="cli")
            clue2 = engine.add_clue("Follow   the stone road", ["path"], ["stone", "road"], source_kind="manual", source_ref="cli")
            self.assertEqual(clue1["id"], "clue-1")
            self.assertEqual(clue2["id"], "clue-1")
            self.assertIn("fingerprint", clue1)
            self.assertEqual(clue1["source"]["kind"], "manual")

    def test_checker_argv_handles_spaces_and_quotes(self):
        guess = 'stone "gate" north'
        argv = build_checker_argv('python3 -c "print(1)" {guess}', guess)
        self.assertEqual(argv[-1], guess)

    def test_run_writes_attempts(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = TreasureHuntEngine(Path(tmp))
            engine.init_workspace()
            engine.add_clue("Look near the old river gate", ["river"], ["gate"])
            found = engine.run(rounds=1, per_clue=5, sleep_seconds=0, checker_command=None)
            self.assertFalse(found)
            lines = engine.attempts_file.read_text(encoding="utf-8").strip().splitlines()
            self.assertGreater(len(lines), 0)

    def test_allowlist_blocking_for_internet_ingest(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = TreasureHuntEngine(Path(tmp))
            engine.init_workspace()
            with patch("treasure_tool.search_duckduckgo", return_value=["https://blocked.example/path"]):
                result = engine.ingest_internet("query", allow_domains=["allowed.example"], max_results=1)
            self.assertEqual(result["pages_fetched"], 0)
            self.assertTrue(any(err.get("reason") == "blocked by allowlist" for err in result["errors"]))

    def test_ingest_internet_requires_allow_domains(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = TreasureHuntEngine(Path(tmp))
            engine.init_workspace()
            with self.assertRaises(ValueError):
                engine.ingest_internet("query", allow_domains=[])

    def test_lead_promotion(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = TreasureHuntEngine(Path(tmp))
            engine.init_workspace()
            lead = engine.add_lead("Hint: find the gate near the river?", source_kind="internet", source_ref="https://example.com")
            promoted = engine.promote_lead(lead_id=lead["id"], fingerprint=None)
            self.assertEqual(promoted["lead"]["promoted_to"], promoted["clue"]["id"])
            self.assertEqual(len(engine.load_clues()), 1)

    def test_ingest_poem(self):
        with tempfile.TemporaryDirectory() as tmp:
            poem_file = Path(tmp) / "poem.txt"
            poem_file.write_text("line one\n\nline two\n", encoding="utf-8")
            engine = TreasureHuntEngine(Path(tmp))
            engine.init_workspace()
            poem = engine.ingest_poem(str(poem_file))
            self.assertEqual(len(poem["lines"]), 2)
            self.assertEqual(poem["lines"][0]["line"], 1)

    def test_solve_workspace_flow(self):
        with tempfile.TemporaryDirectory() as tmp:
            poem_file = Path(tmp) / "poem.txt"
            poem_file.write_text("line one\nline two\n", encoding="utf-8")
            engine = TreasureHuntEngine(Path(tmp))
            engine.init_workspace()
            engine.ingest_poem(str(poem_file))
            engine.add_constraint("Not underwater", "hard")
            solve = engine.new_solve("Solve A", "Trailhead", 40.0, -105.0)
            engine.map_line(solve["id"], 1, "Trail marker", "strong", ["clue-1"])
            score = engine.score_solve(solve["id"])
            self.assertEqual(score["total_lines"], 2)
            self.assertIn(2, score["unmapped_lines"])
            exported = engine.export_solve(solve["id"], "md")
            self.assertIn("Constraint checklist", exported)

    def test_pdf_extraction_fallback(self):
        text = extract_text_from_pdf_bytes(b"%PDF-1.4\n(Find the stone bridge at dawn?)")
        self.assertIn("Find the stone bridge at dawn?", text)

    def test_normalize_url_cases(self):
        self.assertEqual(normalize_url("https://example.com/path?x=1"), "https://example.com/path?x=1")
        self.assertEqual(normalize_url("example.com/path"), "https://example.com/path")
        self.assertEqual(normalize_url("https://example.com"), "https://example.com/")

    def test_helpers(self):
        text = "Find the stone bridge near the north gate and decode the old marker"
        self.assertIn("stone", derive_keywords(text))
        self.assertIn("north", derive_hints(text))
        self.assertGreater(len(extract_candidate_clues("Clue: Find the old gate at sunrise?\nIgnore")), 0)


if __name__ == "__main__":
    unittest.main()
