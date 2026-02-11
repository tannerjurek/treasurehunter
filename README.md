# Treasure Hunt Assistant (Provenance-First)

This tool is a **safe, provenance-driven hunt assistant** for organizing evidence and checking consistency.
It is **not** a “final treasure location” generator.

## Safety + legal reminders

- Do not trespass.
- Obey closures, posted restrictions, and private property boundaries.
- Avoid dangerous conditions (weather, snow, unstable terrain, water hazards).
- Follow local laws and land-management rules.

## Core model: clues vs leads

- **Clues** (`clues.json`): promoted/accepted evidence with provenance metadata.
- **Leads** (`leads.json`): unverified web snippets (default output of `ingest-internet`).
- Promote only vetted leads using `promote-lead`.

Each clue includes:
- `fingerprint` (sha256 prefix of normalized text)
- `source` object: `{kind, ref, imported_at}`

Duplicates are deduped by fingerprint.

## Optional dependency (PDF quality)

The tool works with stdlib only, but for better PDF extraction install optional `pypdf`:

```bash
pip install pypdf
```

If unavailable, the tool falls back to heuristic extraction.

## Workspace files

- `clues.json`
- `leads.json`
- `poem.json`
- `solves.json`
- `constraints.json`
- `attempts.jsonl`
- `solution.json`
- `ingest_log.jsonl`

## Local-only workflow (recommended)

```bash
python3 treasure_tool.py --workspace ./hunt init
python3 treasure_tool.py --workspace ./hunt add-clue --text "Bridge near canyon bend" --hints "bridge" --keywords "canyon,bend"
python3 treasure_tool.py --workspace ./hunt ingest-poem --file ./poem.txt
python3 treasure_tool.py --workspace ./hunt add-constraint --type safety --text "Avoid avalanche terrain"
python3 treasure_tool.py --workspace ./hunt new-solve --name "Solve A" --start "Main trailhead"
python3 treasure_tool.py --workspace ./hunt map-line --solve-id solve-1 --line 1 --maps-to "Trailhead marker" --support moderate --evidence clue-1
python3 treasure_tool.py --workspace ./hunt score-solve --solve-id solve-1
python3 treasure_tool.py --workspace ./hunt export-solve --solve-id solve-1 --format md
```

## Optional network workflow (fail-closed)

### treasure.quest ingestion (allowlisted internally)

```bash
python3 treasure_tool.py --workspace ./hunt ingest-treasure-quest --url https://treasure.quest/ --max-pages 30
```

### Internet ingestion into leads (allowlist REQUIRED)

```bash
python3 treasure_tool.py --workspace ./hunt ingest-internet \
  --query "Justin Posey treasure hunt" \
  --allow-domains "treasure.quest,youtube.com" \
  --max-results 12
```

Without `--allow-domains`, internet ingestion refuses to run.
Blocked URLs are logged as `"blocked by allowlist"`.

Promote vetted leads:

```bash
python3 treasure_tool.py --workspace ./hunt promote-lead --lead-id lead-3
```

## Experimental guess mode

`run` and `run-forever` are **EXPERIMENTAL** heuristic helpers.
They should not be treated as authoritative conclusions.

Checker command uses tokenized execution (no shell) and literal `{guess}` replacement:

```bash
python3 treasure_tool.py --workspace ./hunt run --checker "python3 checker.py {guess}"
```
