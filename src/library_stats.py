#!/usr/bin/env python3

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

DEFAULT_DATA_FILE = Path("output/stremio_data.json")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Count library items and fully watched movies, TV shows, and episodes in your Stremio database."
    )
    parser.add_argument(
        "input_file",
        nargs="?",
        type=Path,
        default=DEFAULT_DATA_FILE,
        help=f"Path to the Stremio JSON export file (default: {DEFAULT_DATA_FILE}).",
    )
    parser.add_argument(
        "-n",
        "--top",
        type=int,
        default=10,
        help="Number of top TV shows to display by watched episode count (default: 10).",
    )
    args = parser.parse_args()

    if not args.input_file.is_file():
        parser.error(f"File not found: {args.input_file}")
    if args.top < 0:
        parser.error("--top must be 0 or greater.")

    data = json.loads(args.input_file.read_text(encoding="utf-8"))

    # 1. Library count.
    library = [item for item in (data.get("library") or [])]
    lib_counts = Counter(item.get("type") or "unknown" for item in library)
    lib_movies = lib_counts.pop("movie", 0)
    lib_series = lib_counts.pop("series", 0)

    print("=== Library ===")
    print(f"Total library items   : {len(library)}")
    print(f"Movies                : {lib_movies}")
    print(f"TV Shows (series)     : {lib_series}")
    for media_type, count in sorted(lib_counts.items()):
        print(f"Other ({media_type:<14}): {count}")

    # 2. Watch activity count (fully watched movies, TV shows, and episodes).
    watch_activity = data.get("watch_activity") or []

    fully_watched_movies = 0
    fully_watched_series = 0
    watched_episodes_total = 0
    series_episode_counts: list[dict[str, Any]] = []

    for item in watch_activity:
        item_type = item.get("type")
        times_watched = item.get("times_watched") or 0
        flagged_watched = item.get("flagged_watched") or 0

        if item_type == "movie":
            if times_watched > 0 or flagged_watched > 0:
                fully_watched_movies += 1

        elif item_type == "series":
            decoded = item.get("series_watched_decoded") or {}
            watched_indices = decoded.get("watched_episode_indices") or []
            total_indexed = decoded.get("total_indexed_items") or 0
            watched_count = len(watched_indices)

            watched_episodes_total += watched_count

            if watched_count > 0:
                series_episode_counts.append(
                    {
                        "name": item.get("name") or item.get("id") or "Unknown",
                        "id": item.get("id") or "",
                        "watched_count": watched_count,
                        "total_indexed": total_indexed,
                    }
                )

            # A series is fully watched if manually flagged as watched OR if every
            # indexed item in its bitfield has been watched.
            if flagged_watched > 0 or (
                total_indexed > 0 and watched_count >= total_indexed
            ):
                fully_watched_series += 1

    print("\n=== Watch Activity (Fully Watched) ===")
    print(f"Fully watched movies  : {fully_watched_movies}")
    print(f"Fully watched shows   : {fully_watched_series}")
    print(f"Watched TV episodes   : {watched_episodes_total}")

    # 3. Top N TV shows by watched episode count.
    if args.top > 0 and series_episode_counts:
        series_episode_counts.sort(
            key=lambda x: (x["watched_count"], x["name"]),
            reverse=True,
        )
        top_shows = series_episode_counts[: args.top]

        print(f"\n=== Top {len(top_shows)} TV Shows by Watched Episodes ===")
        rank_width = len(str(len(top_shows)))
        for rank, show in enumerate(top_shows, start=1):
            total_str = f"/{show['total_indexed']}" if show["total_indexed"] > 0 else ""
            print(
                f"{rank:>{rank_width}}. {show['name']} ({show['id']}) — "
                f"{show['watched_count']}{total_str} episodes"
            )


if __name__ == "__main__":
    main()
