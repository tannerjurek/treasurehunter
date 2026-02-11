# Justin Posey Treasure Hunt Tool

This repository includes a CLI tool that can continuously gather clues and generate guesses for Justin Posey's treasure hunt.

## What it does

- Creates a workspace with:
  - `clues.json` for clue content
  - `attempts.jsonl` for all generated guesses
  - `solution.json` when a guess is accepted
  - `ingest_log.jsonl` for treasure.quest import history
- Lets you add clues manually from the command line.
- Imports clue-like text directly from `treasure.quest` (including sitemap discovery via `robots.txt`).
- Runs repeated guess-generation rounds (`--rounds 0` means infinite/tireless mode).
- Optionally validates each guess through an external checker command.

## Quick start

```bash
python3 treasure_tool.py --workspace ./justin_hunt init
python3 treasure_tool.py --workspace ./justin_hunt ingest-treasure-quest --url https://treasure.quest/ --max-pages 30
python3 treasure_tool.py --workspace ./justin_hunt run --rounds 5 --per-clue 20
python3 treasure_tool.py --workspace ./justin_hunt status
```

## Import all information from treasure.quest

Use the built-in importer to pull from `treasure.quest` pages (same-domain only):

```bash
python3 treasure_tool.py --workspace ./justin_hunt ingest-treasure-quest \
  --url https://treasure.quest/ \
  --max-pages 100 \
  --timeout 15
```

How it works:
- Starts from the provided `treasure.quest` URL.
- Reads `robots.txt` and follows `Sitemap:` entries when available.
- Pulls text from each discovered page (up to `--max-pages`).
- Extracts candidate clue lines, derives hints/keywords, and appends new clues.

## Tireless mode

Run forever with small sleeps between rounds:

```bash
python3 treasure_tool.py --workspace ./justin_hunt run --rounds 0 --sleep 1.5
```

## Checker integration

You can wire in your own validator script/service by providing a command template:

```bash
python3 treasure_tool.py --workspace ./justin_hunt run \
  --rounds 0 \
  --checker "python3 my_checker.py {guess}"
```

A guess is treated as accepted when:
- the checker exits with status `0`, or
- checker output includes `found` or `correct`.

## Notes

- The importer is intentionally restricted to `treasure.quest` / `www.treasure.quest` domains.
- Generated guesses are heuristic-based and meant to bootstrap search, not replace domain-specific puzzle logic.
