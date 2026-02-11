#!/usr/bin/env python3
"""CLI assistant for iterating through treasure-hunt clues and guesses."""

from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import time
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse
from urllib.error import URLError
from urllib.request import Request, urlopen
from xml.etree import ElementTree
from typing import Iterable


DEFAULT_CLUES = [
    {
        "id": "clue-1",
        "text": "Add your first clue text here.",
        "hints": ["location", "phrase", "pattern"],
        "keywords": ["north", "gate", "stone"],
    }
]


@dataclass
class Guess:
    clue_id: str
    guess: str
    score: float
    rationale: str
    created_at: str
    accepted: bool = False


class TreasureHuntEngine:
    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.clues_file = workspace / "clues.json"
        self.attempts_file = workspace / "attempts.jsonl"
        self.solution_file = workspace / "solution.json"

    def init_workspace(self) -> None:
        self.workspace.mkdir(parents=True, exist_ok=True)
        if not self.clues_file.exists():
            self.clues_file.write_text(json.dumps(DEFAULT_CLUES, indent=2) + "\n", encoding="utf-8")
        if not self.attempts_file.exists():
            self.attempts_file.write_text("", encoding="utf-8")
        if not self.solution_file.exists():
            self.solution_file.write_text(json.dumps({"found": False}, indent=2) + "\n", encoding="utf-8")

    def load_clues(self) -> list[dict]:
        if not self.clues_file.exists():
            raise FileNotFoundError(f"Missing clues file: {self.clues_file}")
        clues = json.loads(self.clues_file.read_text(encoding="utf-8"))
        if not isinstance(clues, list):
            raise ValueError("clues.json must be a JSON list")
        return clues

    def add_clue(self, text: str, hints: list[str], keywords: list[str]) -> dict:
        clues = self.load_clues()
        clue_id = f"clue-{len(clues) + 1}"
        clue = {"id": clue_id, "text": text, "hints": hints, "keywords": keywords}
        clues.append(clue)
        self.clues_file.write_text(json.dumps(clues, indent=2) + "\n", encoding="utf-8")
        return clue

    def known_guesses(self) -> set[str]:
        guesses: set[str] = set()
        if not self.attempts_file.exists():
            return guesses
        for line in self.attempts_file.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            payload = json.loads(line)
            guesses.add(payload["guess"])
        return guesses

    def generate_guesses(self, clue: dict, cap: int) -> Iterable[Guess]:
        clue_id = clue["id"]
        text = clue.get("text", "")
        hints = [h.lower().strip() for h in clue.get("hints", []) if h.strip()]
        keywords = [k.lower().strip() for k in clue.get("keywords", []) if k.strip()]

        base_tokens = [token.strip(".,!?;:()[]{}\"'").lower() for token in text.split()]
        base_tokens = [token for token in base_tokens if token]

        candidates: list[tuple[str, str]] = []

        for token in base_tokens:
            candidates.append((token, "direct token from clue text"))
            if token[::-1] != token:
                candidates.append((token[::-1], "reverse token hypothesis"))

        if keywords:
            candidates.append(("".join(keywords), "joined keyword string"))
            candidates.append(("-".join(keywords), "dash-separated keywords"))

        if hints and keywords:
            for hint in hints:
                for keyword in keywords:
                    candidates.append((f"{hint}-{keyword}", "hint+keyword synthesis"))

        if len(base_tokens) >= 2:
            candidates.append((" ".join(base_tokens[:2]), "first two tokens as phrase"))
            candidates.append((" ".join(base_tokens[-2:]), "last two tokens as phrase"))

        yielded = 0
        for guess_text, reason in candidates:
            if yielded >= cap:
                break
            score = self.score_guess(guess_text, hints, keywords)
            yield Guess(
                clue_id=clue_id,
                guess=guess_text,
                score=score,
                rationale=reason,
                created_at=now_iso(),
            )
            yielded += 1

    def score_guess(self, guess: str, hints: list[str], keywords: list[str]) -> float:
        guess_lower = guess.lower()
        score = 0.0
        for hint in hints:
            if hint and hint in guess_lower:
                score += 1.0
        for keyword in keywords:
            if keyword and keyword in guess_lower:
                score += 1.5
        score += max(0.0, 2.0 - abs(len(guess) - 12) / 12)
        return round(score, 2)

    def validate_guess(self, guess: str, checker_command: str | None) -> bool:
        if not checker_command:
            return False
        command = checker_command.format(guess=shlex.quote(guess))
        completed = subprocess.run(command, shell=True, check=False, capture_output=True, text=True)
        stdout = completed.stdout.lower()
        stderr = completed.stderr.lower()
        return completed.returncode == 0 or "found" in stdout or "correct" in stdout or "correct" in stderr

    def write_attempt(self, attempt: Guess) -> None:
        with self.attempts_file.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(asdict(attempt)) + "\n")

    def write_solution(self, attempt: Guess) -> None:
        payload = {
            "found": True,
            "solution": asdict(attempt),
            "resolved_at": now_iso(),
        }
        self.solution_file.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def status(self) -> dict:
        attempts = 0
        if self.attempts_file.exists():
            attempts = sum(1 for _ in self.attempts_file.read_text(encoding="utf-8").splitlines() if _.strip())
        solution = json.loads(self.solution_file.read_text(encoding="utf-8")) if self.solution_file.exists() else {"found": False}
        clues = self.load_clues() if self.clues_file.exists() else []
        return {"workspace": str(self.workspace), "clues": len(clues), "attempts": attempts, "found": solution.get("found", False)}

    def run(self, rounds: int, per_clue: int, sleep_seconds: float, checker_command: str | None) -> bool:
        known = self.known_guesses()
        loop = 0
        while rounds == 0 or loop < rounds:
            loop += 1
            for clue in self.load_clues():
                for guess in self.generate_guesses(clue, cap=per_clue):
                    if guess.guess in known:
                        continue
                    guess.accepted = self.validate_guess(guess.guess, checker_command)
                    self.write_attempt(guess)
                    known.add(guess.guess)
                    print(f"[{guess.clue_id}] {guess.guess} (score={guess.score}, accepted={guess.accepted})")
                    if guess.accepted:
                        self.write_solution(guess)
                        return True
            if sleep_seconds > 0:
                time.sleep(sleep_seconds)
        return False

    def ingest_treasure_quest(self, start_url: str, max_pages: int = 25, timeout: float = 10.0) -> dict:
        urls = self.discover_treasure_quest_urls(start_url, max_pages=max_pages, timeout=timeout)
        page_texts: list[str] = []
        fetch_errors: list[dict[str, str]] = []
        for url in urls:
            try:
                html = fetch_text(url, timeout=timeout)
                page_texts.append(html_to_text(html))
            except URLError as exc:
                fetch_errors.append({"url": url, "error": str(exc)})

        combined = "\n".join(page_texts)
        clue_texts = extract_candidate_clues(combined, limit=8)
        if not clue_texts:
            clue_texts = [combined[:220].strip()] if combined.strip() else ["Imported treasure.quest content"]

        created: list[dict] = []
        existing_texts = {clue.get("text", "") for clue in self.load_clues()}
        for clue_text in clue_texts:
            if clue_text in existing_texts:
                continue
            keywords = derive_keywords(clue_text)
            hints = derive_hints(clue_text)
            created.append(self.add_clue(clue_text, hints=hints, keywords=keywords))
            existing_texts.add(clue_text)

        ingest_record = {
            "created_at": now_iso(),
            "source": start_url,
            "pages_scanned": len(urls),
            "pages_fetched": len(page_texts),
            "clues_created": len(created),
            "urls": urls,
            "errors": fetch_errors,
        }
        ingest_file = self.workspace / "ingest_log.jsonl"
        with ingest_file.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(ingest_record) + "\n")

        return ingest_record

    def discover_treasure_quest_urls(self, start_url: str, max_pages: int, timeout: float) -> list[str]:
        normalized = normalize_url(start_url)
        parsed = urlparse(normalized)
        if parsed.netloc not in {"treasure.quest", "www.treasure.quest"}:
            raise ValueError("For safety this importer only supports treasure.quest URLs")

        robot_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
        candidate_urls = [normalized]

        try:
            robots_content = fetch_text(robot_url, timeout=timeout)
            for line in robots_content.splitlines():
                entry = line.strip()
                if entry.lower().startswith("sitemap:"):
                    sitemap_url = entry.split(":", 1)[1].strip()
                    candidate_urls.extend(parse_sitemap_urls(sitemap_url, timeout=timeout))
        except Exception:
            pass

        same_site = []
        seen: set[str] = set()
        for url in candidate_urls:
            normalized_url = normalize_url(url)
            parsed_url = urlparse(normalized_url)
            if parsed_url.netloc not in {"treasure.quest", "www.treasure.quest"}:
                continue
            if normalized_url in seen:
                continue
            same_site.append(normalized_url)
            seen.add(normalized_url)
            if len(same_site) >= max_pages:
                break
        return same_site


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        text = data.strip()
        if text:
            self.parts.append(text)


def html_to_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    return "\n".join(parser.parts)


def normalize_url(raw: str) -> str:
    parsed = urlparse(raw)
    scheme = parsed.scheme or "https"
    netloc = parsed.netloc or parsed.path
    path = parsed.path if parsed.netloc else ""
    if not path:
        path = "/"
    return f"{scheme}://{netloc}{path}"


def fetch_text(url: str, timeout: float = 10.0) -> str:
    request = Request(url, headers={"User-Agent": "treasure-tool/1.0"})
    with urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="ignore")


def parse_sitemap_urls(sitemap_url: str, timeout: float = 10.0) -> list[str]:
    content = fetch_text(sitemap_url, timeout=timeout)
    root = ElementTree.fromstring(content)
    namespace = ""
    if root.tag.startswith("{"):
        namespace = root.tag.split("}", 1)[0].strip("{")

    def tag(name: str) -> str:
        return f"{{{namespace}}}{name}" if namespace else name

    urls: list[str] = []
    if root.tag.endswith("sitemapindex"):
        for sitemap in root.findall(tag("sitemap")):
            loc = sitemap.find(tag("loc"))
            if loc is not None and loc.text:
                urls.extend(parse_sitemap_urls(loc.text.strip(), timeout=timeout))
    else:
        for url in root.findall(tag("url")):
            loc = url.find(tag("loc"))
            if loc is not None and loc.text:
                urls.append(loc.text.strip())
    return urls


def extract_candidate_clues(raw_text: str, limit: int = 8) -> list[str]:
    lines = [line.strip() for line in raw_text.splitlines()]
    lines = [line for line in lines if 20 <= len(line) <= 280]
    prompty = [line for line in lines if "?" in line or any(x in line.lower() for x in ["clue", "riddle", "hint", "find"]) ]
    output = prompty[:limit]
    if len(output) < limit:
        for line in lines:
            if line not in output:
                output.append(line)
            if len(output) >= limit:
                break
    return output


def derive_keywords(text: str, limit: int = 6) -> list[str]:
    stopwords = {
        "the", "and", "for", "that", "with", "from", "this", "your", "have", "into", "there", "where",
        "what", "when", "will", "just", "posey", "treasure", "quest", "about", "then", "than", "they",
    }
    tokens = [token.lower() for token in re.findall(r"[a-zA-Z]{4,}", text)]
    counts = Counter(token for token in tokens if token not in stopwords)
    return [token for token, _ in counts.most_common(limit)]


def derive_hints(text: str, limit: int = 4) -> list[str]:
    hints: list[str] = []
    lowered = text.lower()
    for marker in ["north", "south", "east", "west", "river", "bridge", "gate", "stone", "map", "code"]:
        if marker in lowered:
            hints.append(marker)
        if len(hints) >= limit:
            break
    if not hints:
        hints = derive_keywords(text, limit=limit)
    return hints[:limit]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Tireless helper for Justin Posey's treasure hunt")
    parser.add_argument("--workspace", default="./hunt_workspace", help="Directory for clues and attempt logs")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init", help="Initialize workspace files")

    add = subparsers.add_parser("add-clue", help="Add a clue")
    add.add_argument("--text", required=True, help="Clue text")
    add.add_argument("--hints", default="", help="Comma-separated hints")
    add.add_argument("--keywords", default="", help="Comma-separated keywords")

    run = subparsers.add_parser("run", help="Run rounds of automated guessing")
    run.add_argument("--rounds", type=int, default=1, help="How many rounds to run; 0 for infinite")
    run.add_argument("--per-clue", type=int, default=25, help="Max guesses to generate per clue per round")
    run.add_argument("--sleep", type=float, default=0.0, help="Seconds to sleep between rounds")
    run.add_argument("--checker", default=None, help="Command template used to validate guesses; use {guess}")

    subparsers.add_parser("status", help="Show current workspace status")

    ingest = subparsers.add_parser("ingest-treasure-quest", help="Import clues directly from treasure.quest")
    ingest.add_argument("--url", default="https://treasure.quest/", help="treasure.quest URL to start from")
    ingest.add_argument("--max-pages", type=int, default=25, help="Max pages to ingest from sitemap/start URL")
    ingest.add_argument("--timeout", type=float, default=10.0, help="Network timeout in seconds")

    return parser


def parse_csv(value: str) -> list[str]:
    return [segment.strip() for segment in value.split(",") if segment.strip()]


def main() -> int:
    args = build_parser().parse_args()
    engine = TreasureHuntEngine(Path(args.workspace))

    if args.command == "init":
        engine.init_workspace()
        print(f"Initialized workspace at {engine.workspace}")
        return 0

    if args.command == "add-clue":
        engine.init_workspace()
        clue = engine.add_clue(text=args.text, hints=parse_csv(args.hints), keywords=parse_csv(args.keywords))
        print(json.dumps(clue, indent=2))
        return 0

    if args.command == "run":
        engine.init_workspace()
        found = engine.run(
            rounds=args.rounds,
            per_clue=args.per_clue,
            sleep_seconds=args.sleep,
            checker_command=args.checker,
        )
        if found:
            print("Solution candidate accepted; stopping.")
            return 0
        print("Completed rounds with no accepted solution.")
        return 1

    if args.command == "status":
        engine.init_workspace()
        print(json.dumps(engine.status(), indent=2))
        return 0

    if args.command == "ingest-treasure-quest":
        engine.init_workspace()
        result = engine.ingest_treasure_quest(start_url=args.url, max_pages=args.max_pages, timeout=args.timeout)
        print(json.dumps(result, indent=2))
        return 0

    raise ValueError(f"Unknown command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
