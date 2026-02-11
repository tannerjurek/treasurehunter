#!/usr/bin/env python3
"""CLI assistant for organizing treasure-hunt evidence and consistency checks."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
import string
import subprocess
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable
from urllib.error import URLError
from urllib.parse import parse_qs, unquote, urlparse
from urllib.request import Request, urlopen
from xml.etree import ElementTree


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
        self.leads_file = workspace / "leads.json"
        self.poem_file = workspace / "poem.json"
        self.solves_file = workspace / "solves.json"
        self.constraints_file = workspace / "constraints.json"
        self.attempts_file = workspace / "attempts.jsonl"
        self.solution_file = workspace / "solution.json"
        self.ingest_log_file = workspace / "ingest_log.jsonl"

    def init_workspace(self) -> None:
        self.workspace.mkdir(parents=True, exist_ok=True)
        defaults = {
            self.clues_file: [],
            self.leads_file: [],
            self.poem_file: {"lines": []},
            self.solves_file: [],
            self.constraints_file: {"hard": [], "safety": []},
            self.solution_file: {"found": False},
        }
        for file_path, payload in defaults.items():
            if not file_path.exists():
                file_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        if not self.attempts_file.exists():
            self.attempts_file.write_text("", encoding="utf-8")
        if not self.ingest_log_file.exists():
            self.ingest_log_file.write_text("", encoding="utf-8")

    def load_clues(self) -> list[dict]:
        clues = json.loads(self.clues_file.read_text(encoding="utf-8"))
        if not isinstance(clues, list):
            raise ValueError("clues.json must be a JSON list")
        return clues

    def save_clues(self, clues: list[dict]) -> None:
        self.clues_file.write_text(json.dumps(clues, indent=2) + "\n", encoding="utf-8")

    def load_leads(self) -> list[dict]:
        leads = json.loads(self.leads_file.read_text(encoding="utf-8"))
        if not isinstance(leads, list):
            raise ValueError("leads.json must be a JSON list")
        return leads

    def save_leads(self, leads: list[dict]) -> None:
        self.leads_file.write_text(json.dumps(leads, indent=2) + "\n", encoding="utf-8")

    def add_clue(self, text: str, hints: list[str], keywords: list[str], source_kind: str = "manual", source_ref: str = "cli") -> dict:
        cleaned_text = normalize_whitespace(text)
        if not cleaned_text:
            raise ValueError("clue text cannot be empty")
        fingerprint = clue_fingerprint(cleaned_text)

        clues = self.load_clues()
        for clue in clues:
            if clue.get("fingerprint") == fingerprint:
                return clue

        clue_id = f"clue-{len(clues) + 1}"
        clue = {
            "id": clue_id,
            "text": cleaned_text,
            "hints": [h.strip() for h in hints if h.strip()],
            "keywords": [k.strip() for k in keywords if k.strip()],
            "fingerprint": fingerprint,
            "source": {
                "kind": source_kind,
                "ref": source_ref,
                "imported_at": now_iso(),
            },
        }
        clues.append(clue)
        self.save_clues(clues)
        return clue

    def add_lead(self, text: str, source_kind: str, source_ref: str) -> dict:
        cleaned_text = normalize_whitespace(text)
        if not cleaned_text:
            raise ValueError("lead text cannot be empty")

        leads = self.load_leads()
        fingerprint = clue_fingerprint(cleaned_text)
        for lead in leads:
            if lead.get("fingerprint") == fingerprint:
                return lead

        lead = {
            "id": f"lead-{len(leads) + 1}",
            "text": cleaned_text,
            "fingerprint": fingerprint,
            "source": {
                "kind": source_kind,
                "ref": source_ref,
                "imported_at": now_iso(),
            },
            "promoted_to": None,
        }
        leads.append(lead)
        self.save_leads(leads)
        return lead

    def promote_lead(self, lead_id: str | None, fingerprint: str | None) -> dict:
        leads = self.load_leads()
        selected = None
        for lead in leads:
            if (lead_id and lead.get("id") == lead_id) or (fingerprint and lead.get("fingerprint") == fingerprint):
                selected = lead
                break
        if not selected:
            raise ValueError("lead not found")

        clue = self.add_clue(
            text=selected["text"],
            hints=derive_hints(selected["text"]),
            keywords=derive_keywords(selected["text"]),
            source_kind=selected["source"]["kind"],
            source_ref=selected["source"]["ref"],
        )
        selected["promoted_to"] = clue["id"]
        self.save_leads(leads)
        return {"lead": selected, "clue": clue}

    def known_guesses(self) -> set[str]:
        guesses: set[str] = set()
        for line in self.attempts_file.read_text(encoding="utf-8").splitlines():
            if line.strip():
                guesses.add(json.loads(line)["guess"])
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

        for guess_text, reason in candidates[:cap]:
            yield Guess(
                clue_id=clue_id,
                guess=guess_text,
                score=self.score_guess(guess_text, hints, keywords),
                rationale=reason,
                created_at=now_iso(),
            )

    def score_guess(self, guess: str, hints: list[str], keywords: list[str]) -> float:
        guess_lower = guess.lower()
        score = 0.0
        score += sum(1.0 for hint in hints if hint and hint in guess_lower)
        score += sum(1.5 for keyword in keywords if keyword and keyword in guess_lower)
        score += max(0.0, 2.0 - abs(len(guess) - 12) / 12)
        return round(score, 2)

    def validate_guess(self, guess: str, checker_command: str | None) -> bool:
        if not checker_command:
            return False
        argv = build_checker_argv(checker_command=checker_command, guess=guess)
        completed = subprocess.run(argv, check=False, capture_output=True, text=True)
        stdout = completed.stdout.lower()
        stderr = completed.stderr.lower()
        return completed.returncode == 0 or "found" in stdout or "correct" in stdout or "correct" in stderr

    def write_attempt(self, attempt: Guess) -> None:
        with self.attempts_file.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(asdict(attempt)) + "\n")

    def write_solution(self, attempt: Guess) -> None:
        self.solution_file.write_text(
            json.dumps({"found": True, "solution": asdict(attempt), "resolved_at": now_iso()}, indent=2) + "\n",
            encoding="utf-8",
        )

    def status(self) -> dict:
        attempts = sum(1 for line in self.attempts_file.read_text(encoding="utf-8").splitlines() if line.strip())
        solution = json.loads(self.solution_file.read_text(encoding="utf-8"))
        return {
            "workspace": str(self.workspace),
            "clues": len(self.load_clues()),
            "leads": len(self.load_leads()),
            "attempts": attempts,
            "found": solution.get("found", False),
        }

    def run(self, rounds: int, per_clue: int, sleep_seconds: float, checker_command: str | None) -> bool:
        known = self.known_guesses()
        loop = 0
        while rounds == 0 or loop < rounds:
            loop += 1
            clues = self.load_clues()
            if not clues:
                print("No clues available. Ingest data or add/promote clues first.")
                return False
            for clue in clues:
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
        return self.ingest_text_sources(source_label=start_url, urls=urls, timeout=timeout, max_items=32, kind="treasure.quest", target="clues")

    def discover_treasure_quest_urls(self, start_url: str, max_pages: int, timeout: float) -> list[str]:
        normalized = normalize_url(start_url)
        parsed = urlparse(normalized)
        if parsed.netloc not in {"treasure.quest", "www.treasure.quest"}:
            raise ValueError("For safety this importer only supports treasure.quest URLs")

        candidate_urls = [normalized]
        robot_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
        try:
            robots_content = fetch_text(robot_url, timeout=timeout)
            for line in robots_content.splitlines():
                entry = line.strip()
                if entry.lower().startswith("sitemap:"):
                    candidate_urls.extend(parse_sitemap_urls(entry.split(":", 1)[1].strip(), timeout=timeout))
        except Exception:
            pass

        same_site: list[str] = []
        seen: set[str] = set()
        for url in candidate_urls:
            urln = normalize_url(url)
            netloc = urlparse(urln).netloc
            if netloc in {"treasure.quest", "www.treasure.quest"} and urln not in seen:
                same_site.append(urln)
                seen.add(urln)
            if len(same_site) >= max_pages:
                break
        return same_site

    def ingest_book_pdf(self, pdf_source: str, timeout: float = 20.0, max_clues: int = 24) -> dict:
        pdf_bytes = load_pdf_bytes(pdf_source, timeout=timeout)
        extracted_text = extract_text_from_pdf_bytes(pdf_bytes)
        items = extract_candidate_clues(extracted_text, limit=max_clues)
        clues_added = [self.add_clue(text=item, hints=derive_hints(item), keywords=derive_keywords(item), source_kind="pdf", source_ref=pdf_source) for item in items]
        return self._write_ingest_record({
            "created_at": now_iso(),
            "source": pdf_source,
            "type": "book-pdf",
            "bytes": len(pdf_bytes),
            "text_characters": len(extracted_text),
            "candidate_clues": len(items),
            "clues_created": len(clues_added),
            "errors": [],
        })

    def ingest_internet(self, query: str, allow_domains: list[str], max_results: int = 12, timeout: float = 12.0, max_leads: int = 40) -> dict:
        if not allow_domains:
            raise ValueError("ingest-internet requires --allow-domains and refuses to run without it")

        try:
            raw_urls = search_duckduckgo(query=query, max_results=max_results, timeout=timeout)
        except Exception as exc:
            return self._write_ingest_record({
                "created_at": now_iso(),
                "source": f"internet:{query}",
                "type": "internet",
                "urls": [],
                "pages_scanned": 0,
                "pages_fetched": 0,
                "candidate_items": 0,
                "items_created": 0,
                "errors": [{"url": "search", "reason": f"{type(exc).__name__}: {exc}"}],
            })

        allowed: list[str] = []
        errors: list[dict[str, str]] = []
        allowset = {d.strip().lower() for d in allow_domains if d.strip()}
        for url in raw_urls:
            domain = urlparse(url).netloc.lower()
            if domain in allowset or any(domain.endswith(f".{root}") for root in allowset):
                allowed.append(url)
            else:
                errors.append({"url": url, "reason": "blocked by allowlist"})

        result = self.ingest_text_sources(
            source_label=f"internet:{query}",
            urls=allowed,
            timeout=timeout,
            max_items=max_leads,
            kind="internet",
            target="leads",
        )
        result["errors"].extend(errors)
        self._write_ingest_record(result)
        return result

    def ingest_text_sources(self, source_label: str, urls: list[str], timeout: float, max_items: int, kind: str, target: str) -> dict:
        texts: list[str] = []
        errors: list[dict[str, str]] = []
        for url in urls:
            try:
                texts.append(html_to_text(fetch_text(url, timeout=timeout)))
            except Exception as exc:
                errors.append({"url": url, "reason": f"{type(exc).__name__}: {exc}"})

        items = extract_candidate_clues("\n".join(texts), limit=max_items)
        created_count = 0
        for item in items:
            if target == "clues":
                self.add_clue(item, hints=derive_hints(item), keywords=derive_keywords(item), source_kind=kind, source_ref=source_label)
                created_count += 1
            else:
                self.add_lead(item, source_kind=kind, source_ref=source_label)
                created_count += 1

        return {
            "created_at": now_iso(),
            "source": source_label,
            "type": kind,
            "target": target,
            "urls": urls,
            "pages_scanned": len(urls),
            "pages_fetched": len(texts),
            "candidate_items": len(items),
            "items_created": created_count,
            "errors": errors,
        }

    def ingest_poem(self, file_path: str) -> dict:
        text = Path(file_path).read_text(encoding="utf-8")
        lines = [normalize_whitespace(line) for line in text.splitlines() if normalize_whitespace(line)]
        payload = {"lines": [{"line": i + 1, "text": line} for i, line in enumerate(lines)]}
        self.poem_file.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        return payload

    def add_constraint(self, text: str, constraint_type: str) -> dict:
        constraints = json.loads(self.constraints_file.read_text(encoding="utf-8"))
        constraints[constraint_type].append(normalize_whitespace(text))
        self.constraints_file.write_text(json.dumps(constraints, indent=2) + "\n", encoding="utf-8")
        return constraints

    def new_solve(self, name: str, start: str, lat: float | None, lon: float | None) -> dict:
        solves = json.loads(self.solves_file.read_text(encoding="utf-8"))
        solve = {
            "id": f"solve-{len(solves) + 1}",
            "name": name,
            "start": start,
            "lat": lat,
            "lon": lon,
            "mappings": [],
            "created_at": now_iso(),
        }
        solves.append(solve)
        self.solves_file.write_text(json.dumps(solves, indent=2) + "\n", encoding="utf-8")
        return solve

    def map_line(self, solve_id: str, line_number: int, maps_to: str, support: str, evidence: list[str]) -> dict:
        solves = json.loads(self.solves_file.read_text(encoding="utf-8"))
        solve = next((s for s in solves if s["id"] == solve_id), None)
        if not solve:
            raise ValueError("solve not found")
        solve["mappings"] = [m for m in solve["mappings"] if m["line"] != line_number]
        solve["mappings"].append({
            "line": line_number,
            "maps_to": maps_to,
            "support": support,
            "evidence": [e.strip() for e in evidence if e.strip()],
            "updated_at": now_iso(),
        })
        self.solves_file.write_text(json.dumps(solves, indent=2) + "\n", encoding="utf-8")
        return solve

    def score_solve(self, solve_id: str) -> dict:
        solves = json.loads(self.solves_file.read_text(encoding="utf-8"))
        solve = next((s for s in solves if s["id"] == solve_id), None)
        if not solve:
            raise ValueError("solve not found")
        poem = json.loads(self.poem_file.read_text(encoding="utf-8"))
        total_lines = len(poem.get("lines", []))

        by_line = {m["line"]: m for m in solve.get("mappings", [])}
        support_counts = {"strong": 0, "moderate": 0, "weak": 0, "unmapped": 0}
        unmapped_lines: list[int] = []
        for line_number in range(1, total_lines + 1):
            entry = by_line.get(line_number)
            if not entry:
                support_counts["unmapped"] += 1
                unmapped_lines.append(line_number)
            else:
                support = entry["support"]
                if support not in support_counts:
                    support_counts[support] = 0
                support_counts[support] += 1

        return {
            "solve_id": solve_id,
            "solve_name": solve["name"],
            "total_lines": total_lines,
            "support_counts": support_counts,
            "unmapped_lines": unmapped_lines,
        }

    def export_solve(self, solve_id: str, fmt: str) -> str:
        solves = json.loads(self.solves_file.read_text(encoding="utf-8"))
        solve = next((s for s in solves if s["id"] == solve_id), None)
        if not solve:
            raise ValueError("solve not found")
        poem = json.loads(self.poem_file.read_text(encoding="utf-8"))
        constraints = json.loads(self.constraints_file.read_text(encoding="utf-8"))
        score = self.score_solve(solve_id)

        payload = {"solve": solve, "poem": poem, "constraints": constraints, "score": score}
        if fmt == "json":
            return json.dumps(payload, indent=2)

        lines = [
            f"# {solve['name']} ({solve['id']})",
            "",
            f"Start: {solve['start']}",
            f"Coordinates: {solve.get('lat')}, {solve.get('lon')}",
            "",
            "## Constraint checklist",
            f"- Hard: {', '.join(constraints.get('hard', [])) or '(none)'}",
            f"- Safety: {', '.join(constraints.get('safety', [])) or '(none)'}",
            "",
            "## Line mappings",
        ]
        by_line = {m["line"]: m for m in solve.get("mappings", [])}
        for poem_line in poem.get("lines", []):
            entry = by_line.get(poem_line["line"])
            if not entry:
                lines.append(f"- L{poem_line['line']}: {poem_line['text']} -> UNMAPPED")
            else:
                evidence = ", ".join(entry.get("evidence", [])) or "(none)"
                lines.append(f"- L{poem_line['line']}: {poem_line['text']} -> {entry['maps_to']} [{entry['support']}] evidence={evidence}")
        lines.append("")
        lines.append("## Score")
        lines.append(json.dumps(score, indent=2))
        return "\n".join(lines)

    def _write_ingest_record(self, payload: dict) -> dict:
        with self.ingest_log_file.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload) + "\n")
        return payload

    def run_forever(
        self,
        per_clue: int,
        sleep_seconds: float,
        checker_command: str | None,
        pdf_source: str | None,
        refresh_pdf_each_cycle: bool,
        treasure_url: str | None,
        refresh_treasure_each_cycle: bool,
        internet_query: str | None,
        refresh_internet_each_cycle: bool,
        allow_domains: list[str],
    ) -> bool:
        loaded_pdf = False
        loaded_treasure = False
        loaded_internet = False
        while True:
            if pdf_source and (refresh_pdf_each_cycle or not loaded_pdf):
                self.ingest_book_pdf(pdf_source)
                loaded_pdf = True
            if treasure_url and (refresh_treasure_each_cycle or not loaded_treasure):
                self.ingest_treasure_quest(treasure_url)
                loaded_treasure = True
            if internet_query and (refresh_internet_each_cycle or not loaded_internet):
                self.ingest_internet(query=internet_query, allow_domains=allow_domains)
                loaded_internet = True

            found = self.run(rounds=1, per_clue=per_clue, sleep_seconds=0.0, checker_command=checker_command)
            if found:
                return True
            if sleep_seconds > 0:
                time.sleep(sleep_seconds)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def clue_fingerprint(text: str) -> str:
    return hashlib.sha256(normalize_whitespace(text).lower().encode("utf-8")).hexdigest()[:16]


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        cleaned = normalize_whitespace(data)
        if cleaned:
            self.parts.append(cleaned)


def html_to_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    return "\n".join(parser.parts)


def normalize_url(raw: str) -> str:
    value = raw.strip()
    if not value:
        return ""
    parsed = urlparse(value if "://" in value else f"https://{value}")
    scheme = parsed.scheme or "https"
    netloc = parsed.netloc
    path = parsed.path or "/"
    query = f"?{parsed.query}" if parsed.query else ""
    return f"{scheme}://{netloc}{path}{query}"


def normalize_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def fetch_text(url: str, timeout: float = 10.0) -> str:
    request = Request(url, headers={"User-Agent": "treasure-tool/1.0"})
    with urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="ignore")


def parse_sitemap_urls(sitemap_url: str, timeout: float = 10.0) -> list[str]:
    root = ElementTree.fromstring(fetch_text(sitemap_url, timeout=timeout))
    namespace = root.tag.split("}", 1)[0].strip("{") if root.tag.startswith("{") else ""

    def tag(name: str) -> str:
        return f"{{{namespace}}}{name}" if namespace else name

    urls: list[str] = []
    if root.tag.endswith("sitemapindex"):
        for sitemap in root.findall(tag("sitemap")):
            loc = sitemap.find(tag("loc"))
            if loc is not None and loc.text:
                urls.extend(parse_sitemap_urls(loc.text.strip(), timeout=timeout))
    else:
        for entry in root.findall(tag("url")):
            loc = entry.find(tag("loc"))
            if loc is not None and loc.text:
                urls.append(normalize_url(loc.text.strip()))
    return urls


def extract_candidate_clues(raw_text: str, limit: int = 8) -> list[str]:
    lines = [normalize_whitespace(line) for line in raw_text.splitlines()]
    lines = [line for line in lines if 20 <= len(line) <= 280]
    prompty = [line for line in lines if "?" in line or any(x in line.lower() for x in ["clue", "riddle", "hint", "find"])]
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
    return hints[:limit] if hints else derive_keywords(text, limit=limit)


def load_pdf_bytes(pdf_source: str, timeout: float = 20.0) -> bytes:
    parsed = urlparse(pdf_source)
    if parsed.scheme in {"http", "https"}:
        request = Request(pdf_source, headers={"User-Agent": "treasure-tool/1.0"})
        with urlopen(request, timeout=timeout) as response:
            return response.read()
    return Path(pdf_source).read_bytes()


def extract_text_from_pdf_bytes(pdf_bytes: bytes) -> str:
    try:
        from pypdf import PdfReader  # type: ignore
        from io import BytesIO

        reader = PdfReader(BytesIO(pdf_bytes))
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
        if normalize_whitespace(text):
            return text
    except Exception:
        pass

    raw = pdf_bytes.decode("latin-1", errors="ignore")
    literal_chunks: list[str] = []
    for match in re.finditer(r"\(([^\)]{6,400})\)", raw):
        chunk = normalize_whitespace(match.group(1).replace(r"\n", " ").replace(r"\r", " ").replace(r"\t", " "))
        if chunk and any(char.isalpha() for char in chunk):
            literal_chunks.append(chunk)
    if literal_chunks:
        return "\n".join(literal_chunks)

    printable = "".join(ch if ch in string.printable else " " for ch in raw)
    words = normalize_whitespace(printable).split()
    return "\n".join(" ".join(words[i:i + 18]) for i in range(0, len(words), 18))


def search_duckduckgo(query: str, max_results: int = 12, timeout: float = 12.0) -> list[str]:
    html = fetch_text(f"https://duckduckgo.com/html/?q={query.replace(' ', '+')}", timeout=timeout)
    matches = re.findall(r'href="([^"]+)"', html)

    results: list[str] = []
    seen: set[str] = set()
    for href in matches:
        if "/l/?" in href:
            target = parse_qs(urlparse(href).query).get("uddg", [""])[0]
            if target:
                href = unquote(target)
        if href.startswith("http") and "duckduckgo.com" not in href:
            normalized = normalize_url(href)
            if normalized and normalized not in seen:
                results.append(normalized)
                seen.add(normalized)
        if len(results) >= max_results:
            break
    return results


def build_checker_argv(checker_command: str, guess: str) -> list[str]:
    parts = shlex.split(checker_command)
    if not parts:
        raise ValueError("checker command is empty")
    replaced = [guess if token == "{guess}" else token.replace("{guess}", guess) for token in parts]
    return replaced


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Provenance-first hunt assistant",
        epilog="Safety: no final-location output, no trespass, obey closures/weather.",
    )
    parser.add_argument("--workspace", default="./hunt_workspace", help="Directory for workspace JSON files")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init", help="Initialize workspace files")
    subparsers.add_parser("status", help="Show workspace status")

    add = subparsers.add_parser("add-clue", help="Add a clue manually")
    add.add_argument("--text", required=True)
    add.add_argument("--hints", default="")
    add.add_argument("--keywords", default="")

    run = subparsers.add_parser("run", help="EXPERIMENTAL: heuristic guess generation")
    run.add_argument("--rounds", type=int, default=1)
    run.add_argument("--per-clue", type=int, default=25)
    run.add_argument("--sleep", type=float, default=0.0)
    run.add_argument("--checker", default=None, help="Command tokens; include literal {guess} token")

    tq = subparsers.add_parser("ingest-treasure-quest", help="Import clues from treasure.quest allowlisted domain")
    tq.add_argument("--url", default="https://treasure.quest/")
    tq.add_argument("--max-pages", type=int, default=25)
    tq.add_argument("--timeout", type=float, default=10.0)

    pdf = subparsers.add_parser("ingest-book-pdf", help="Import clues from local/remote PDF")
    pdf.add_argument("--pdf", required=True)
    pdf.add_argument("--timeout", type=float, default=20.0)
    pdf.add_argument("--max-clues", type=int, default=24)

    web = subparsers.add_parser("ingest-internet", help="Ingest INTERNET LEADS (not clues) with strict allowlist")
    web.add_argument("--query", required=True)
    web.add_argument("--allow-domains", required=True, help="Comma-separated allowlist domains")
    web.add_argument("--max-results", type=int, default=12)
    web.add_argument("--timeout", type=float, default=12.0)
    web.add_argument("--max-leads", type=int, default=40)

    promote = subparsers.add_parser("promote-lead", help="Promote one lead into a clue")
    promote.add_argument("--lead-id", default=None)
    promote.add_argument("--fingerprint", default=None)

    poem = subparsers.add_parser("ingest-poem", help="Load poem lines into poem.json")
    poem.add_argument("--file", required=True)

    constraint = subparsers.add_parser("add-constraint", help="Add hard/safety constraint")
    constraint.add_argument("--text", required=True)
    constraint.add_argument("--type", required=True, choices=["hard", "safety"])

    solve = subparsers.add_parser("new-solve", help="Create a solve workspace entry")
    solve.add_argument("--name", required=True)
    solve.add_argument("--start", required=True)
    solve.add_argument("--lat", type=float, default=None)
    solve.add_argument("--lon", type=float, default=None)

    line = subparsers.add_parser("map-line", help="Map poem line to interpretation")
    line.add_argument("--solve-id", required=True)
    line.add_argument("--line", required=True, type=int)
    line.add_argument("--maps-to", required=True)
    line.add_argument("--support", required=True, choices=["strong", "moderate", "weak", "unmapped"])
    line.add_argument("--evidence", default="")

    score = subparsers.add_parser("score-solve", help="Score solve mapping coverage")
    score.add_argument("--solve-id", required=True)
    score.add_argument("--json", action="store_true")

    export = subparsers.add_parser("export-solve", help="Export solve as markdown or json")
    export.add_argument("--solve-id", required=True)
    export.add_argument("--format", required=True, choices=["md", "json"])

    forever = subparsers.add_parser("run-forever", help="Continuous ingest + experimental run loop")
    forever.add_argument("--per-clue", type=int, default=25)
    forever.add_argument("--sleep", type=float, default=3.0)
    forever.add_argument("--checker", default=None)
    forever.add_argument("--pdf", default=None)
    forever.add_argument("--refresh-pdf", action="store_true")
    forever.add_argument("--treasure-url", default=None)
    forever.add_argument("--refresh-treasure", action="store_true")
    forever.add_argument("--internet-query", default=None)
    forever.add_argument("--refresh-internet", action="store_true")
    forever.add_argument("--allow-domains", default="")

    return parser


def parse_csv(value: str) -> list[str]:
    return [segment.strip() for segment in value.split(",") if segment.strip()]


def main() -> int:
    args = build_parser().parse_args()
    engine = TreasureHuntEngine(Path(args.workspace))
    engine.init_workspace()

    if args.command == "init":
        print(f"Initialized workspace at {engine.workspace}")
        return 0
    if args.command == "status":
        print(json.dumps(engine.status(), indent=2))
        return 0
    if args.command == "add-clue":
        clue = engine.add_clue(text=args.text, hints=parse_csv(args.hints), keywords=parse_csv(args.keywords))
        print(json.dumps(clue, indent=2))
        return 0
    if args.command == "run":
        found = engine.run(rounds=args.rounds, per_clue=args.per_clue, sleep_seconds=args.sleep, checker_command=args.checker)
        print("Solution candidate accepted; stopping." if found else "Completed rounds with no accepted solution.")
        return 0 if found else 1
    if args.command == "ingest-treasure-quest":
        print(json.dumps(engine.ingest_treasure_quest(start_url=args.url, max_pages=args.max_pages, timeout=args.timeout), indent=2))
        return 0
    if args.command == "ingest-book-pdf":
        print(json.dumps(engine.ingest_book_pdf(pdf_source=args.pdf, timeout=args.timeout, max_clues=args.max_clues), indent=2))
        return 0
    if args.command == "ingest-internet":
        allow_domains = parse_csv(args.allow_domains)
        if not allow_domains:
            print("Error: ingest-internet requires non-empty --allow-domains", file=sys.stderr)
            return 2
        print(json.dumps(engine.ingest_internet(query=args.query, allow_domains=allow_domains, max_results=args.max_results, timeout=args.timeout, max_leads=args.max_leads), indent=2))
        return 0
    if args.command == "promote-lead":
        if not args.lead_id and not args.fingerprint:
            print("Error: provide --lead-id or --fingerprint", file=sys.stderr)
            return 2
        print(json.dumps(engine.promote_lead(lead_id=args.lead_id, fingerprint=args.fingerprint), indent=2))
        return 0
    if args.command == "ingest-poem":
        print(json.dumps(engine.ingest_poem(args.file), indent=2))
        return 0
    if args.command == "add-constraint":
        print(json.dumps(engine.add_constraint(text=args.text, constraint_type=args.type), indent=2))
        return 0
    if args.command == "new-solve":
        print(json.dumps(engine.new_solve(name=args.name, start=args.start, lat=args.lat, lon=args.lon), indent=2))
        return 0
    if args.command == "map-line":
        print(json.dumps(engine.map_line(solve_id=args.solve_id, line_number=args.line, maps_to=args.maps_to, support=args.support, evidence=parse_csv(args.evidence)), indent=2))
        return 0
    if args.command == "score-solve":
        score = engine.score_solve(args.solve_id)
        if args.json:
            print(json.dumps(score, indent=2))
        else:
            print(f"Solve {score['solve_name']} ({score['solve_id']})")
            print(f"Total lines: {score['total_lines']}")
            print(f"Support counts: {score['support_counts']}")
            print(f"Unmapped lines: {score['unmapped_lines']}")
        return 0
    if args.command == "export-solve":
        print(engine.export_solve(solve_id=args.solve_id, fmt=args.format))
        return 0
    if args.command == "run-forever":
        allow_domains = parse_csv(args.allow_domains)
        found = engine.run_forever(
            per_clue=args.per_clue,
            sleep_seconds=args.sleep,
            checker_command=args.checker,
            pdf_source=args.pdf,
            refresh_pdf_each_cycle=args.refresh_pdf,
            treasure_url=args.treasure_url,
            refresh_treasure_each_cycle=args.refresh_treasure,
            internet_query=args.internet_query,
            refresh_internet_each_cycle=args.refresh_internet,
            allow_domains=allow_domains,
        )
        return 0 if found else 1

    raise ValueError(f"Unknown command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
