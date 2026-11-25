#!/usr/bin/env python3

"""
Utility: Analyzes Stremio API responses (saved via the sync script's `--debug` flag)
to find duplicate database IDs. Useful for comparing exactly what has been duplicated.
"""

import argparse
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

DEFAULT_API_PATH = Path("output/api_responses")
FILENAME_RE = re.compile(r"^api_(\d+)_([A-Za-z0-9_]+)\.json$")


def find_newest_contiguous_gets(responses_dir: Path) -> list[Path]:
    """
    Sort all `api_<timestamp_ms>_<method>.json` files chronologically and extract
    the newest sync cycle's `datastoreGet` calls.

    Algorithm (reverse scan from newest to oldest):
      1. Skip any trailing `datastoreMeta` or `getUser` calls from newer incremental
         checks that had 0 items to fetch.
      2. Once the newest `datastoreGet` is reached, collect every `datastoreGet`
         (ignoring `getUser` calls that happen if a sync was resumed across runs)
         until hitting the `datastoreMeta` call that initiated that sync cycle,
         then stop.
    """
    parsed_files: list[tuple[int, str, Path]] = []

    for path in responses_dir.iterdir():
        if not path.is_file():
            continue
        match = FILENAME_RE.match(path.name)
        if match:
            timestamp_ms = int(match.group(1))
            method = match.group(2)
            parsed_files.append((timestamp_ms, method, path))

    # Sort chronologically by timestamp (with filename as deterministic tie-breaker).
    parsed_files.sort(key=lambda entry: (entry[0], entry[2].name))

    collected_gets: list[Path] = []
    for _, method, path in reversed(parsed_files):
        if method == "datastoreGet":
            collected_gets.append(path)
        elif method == "datastoreMeta" and collected_gets:
            # Reached the `datastoreMeta` boundary that started this sync batch sequence.
            break

    collected_gets.reverse()
    return collected_gets


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Find duplicate `_id` records in the newest sync cycle's "
            "Stremio `datastoreGet` debug responses."
        )
    )
    parser.add_argument(
        "responses_dir",
        nargs="?",
        type=Path,
        default=DEFAULT_API_PATH,
        help=f"Path to the api_responses directory (default: {DEFAULT_API_PATH}).",
    )
    args = parser.parse_args()

    if not args.responses_dir.is_dir():
        parser.error(f"Directory not found: {args.responses_dir}")

    get_files = find_newest_contiguous_gets(args.responses_dir)
    if not get_files:
        print(f"No `datastoreGet` response files found in {args.responses_dir}.")
        return

    print(f"Scanning {len(get_files)} `datastoreGet` file(s) from newest active sync:")
    for path in get_files:
        print(f"  - {path.name}")

    records_by_id: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
    total_records = 0

    for path in get_files:
        payload = json.loads(path.read_text(encoding="utf-8"))
        items = payload.get("result") or []
        for item in items:
            total_records += 1
            item_id = item.get("_id", "<missing _id>")
            records_by_id[item_id].append((path.name, item))

    duplicates = {
        item_id: entries
        for item_id, entries in records_by_id.items()
        if len(entries) > 1
    }

    print(
        f"\nScanned {total_records} total records across {len(records_by_id)} unique IDs."
    )

    if not duplicates:
        print("No duplicate IDs found.")
        return

    print(f"Found {len(duplicates)} duplicate ID(s):\n")
    for item_id, entries in duplicates.items():
        print("=" * 72)
        print(f"Duplicate ID: {item_id} ({len(entries)} occurrences)")
        print("=" * 72)
        for idx, (source_file, raw_item) in enumerate(entries, start=1):
            print(f"\n--- Occurrence {idx}/{len(entries)} (from {source_file}) ---")
            print(json.dumps(raw_item, indent=2, ensure_ascii=False))
        print()


if __name__ == "__main__":
    main()
