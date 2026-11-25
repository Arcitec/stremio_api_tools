#!/usr/bin/env python3

"""
TODO: Implement multi-profile support? It's probably just a very simple additional
API argument based on the IDs returned by getUser. I don't have a premium account
and never will, so I can't implement it. Pull requests are welcome.
"""

import argparse
import base64
import copy
import json
import math
import os
import time
import zlib
from pathlib import Path
from typing import Any

import requests

# WARNING: Batch size is technically not limited on the server-side, but they may
# return errors if their response ends up being too big. The default 500 is safe.
API_BASE = "https://api.strem.io/api"
DEFAULT_OUTPUT_FILE = Path("output/stremio_data.json")
DEFAULT_BATCH_SIZE = 500
DEFAULT_SLEEP_SECONDS = 1.0


def save_atomic(path: Path, data: Any) -> None:
    """
    Write JSON data atomically by writing to a sibling `.tmp` file first and
    renaming it over the target path. Prevents corrupted JSON files if the
    process is interrupted with Ctrl-C or loses power mid-write.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".tmp")
    tmp_path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    tmp_path.replace(path)


def stremio_rpc(
    method: str,
    payload: dict[str, Any],
    debug_dir: Path | None = None,
) -> Any:
    """
    Execute a Stremio JSON-over-HTTPS RPC call and unwrap the `result` envelope.

    When `debug_dir` is provided, the raw JSON response is immediately written to:
        `<debug_dir>/api_<unixtime_ms>_<method>.json`
    Logging inside `stremio_rpc` ensures raw responses are captured immediately
    as each request completes, even if a multi-batch sync is aborted with Ctrl-C.
    """
    response = requests.post(f"{API_BASE}/{method}", json=payload, timeout=30)
    response.raise_for_status()
    raw_data = response.json()

    if debug_dir is not None:
        timestamp_ms = int(time.time() * 1000)
        sanitized = copy.deepcopy(raw_data)

        # Strip sensitive account identifiers from `getUser` before saving debug logs.
        if method == "getUser" and isinstance(sanitized.get("result"), dict):
            if "_id" in sanitized["result"]:
                sanitized["result"]["_id"] = "ANONYMOUS"
            if "email" in sanitized["result"]:
                sanitized["result"]["email"] = "anonymous@example.com"

        debug_path = debug_dir / f"api_{timestamp_ms}_{method}.json"
        save_atomic(debug_path, sanitized)

    if raw_data.get("error"):
        raise RuntimeError(f"Stremio API error on {method}: {raw_data['error']}")
    return raw_data.get("result")


def decode_watched_bitfield(watched_str: str) -> dict[str, Any]:
    """
    Decode Stremio's series `state.watched` string into 0-based episode indices.

    Wire format:
        `<last_video_id>:<total_indexed_items>:<base64_zlib_bitset>`

    Note: `<last_video_id>` itself contains colons (e.g. `tt0903747:1:1`),
    so the string MUST be split from the right (`rsplit(":", 2)`).
    The decompressed payload is an LSB-first bitfield where bit `i` indicates
    whether episode `i` (in Cinemeta's episode list order) has been watched.
    """
    if not watched_str or ":" not in watched_str:
        return {"last_video_id": None, "watched_episode_indices": []}

    parts = watched_str.rsplit(":", 2)
    if len(parts) != 3:
        return {"last_video_id": watched_str, "watched_episode_indices": []}

    last_video_id, item_count_str, b64_payload = parts
    try:
        item_count = int(item_count_str)
    except ValueError:
        return {"last_video_id": watched_str, "watched_episode_indices": []}

    try:
        raw_bytes = zlib.decompress(base64.b64decode(b64_payload))
    except Exception:  # noqa: BLE001
        return {"last_video_id": last_video_id, "watched_episode_indices": []}

    watched_indices = [
        bit_idx
        for bit_idx in range(item_count)
        if (bit_idx // 8) < len(raw_bytes)
        and (raw_bytes[bit_idx // 8] & (1 << (bit_idx % 8)))
    ]

    return {
        "last_video_id": last_video_id,
        "total_indexed_items": item_count,
        "watched_episode_indices": watched_indices,
    }


def normalize_item(item: dict[str, Any]) -> dict[str, Any]:
    """
    Convert a raw `libraryItem` document from `datastoreGet` into a clean record.

    Stremio stores both active library entries and watch-history-only entries in
    the `libraryItem` collection:
      - `removed == False`: Actively saved in the user's Library.
      - `removed == True`:  Removed from Library, retained solely for watch history/progress.
    """
    # NOTE: The `or` pattern defends against explicit `null` values in API responses.
    state = item.get("state") or {}
    duration = state.get("duration") or 0
    offset = state.get("timeOffset") or 0
    progress_pct = round((offset / duration) * 100, 1) if duration > 0 else 0.0

    entry: dict[str, Any] = {
        "id": item.get("_id"),  # Media ID (such as IMDB `ttX` or `kitsu:X`).
        "name": item.get("name"),
        "type": item.get("type"),
        "in_library": not item.get("removed", False),
        "added_at": item.get("_ctime"),  # Creation time.
        "modified_at": item.get("_mtime"),  # Modification time.
        "last_watched": state.get("lastWatched"),
        "times_watched": state.get("timesWatched", 0),
        "flagged_watched": state.get("flaggedWatched", 0),
        "current_video_id": state.get("video_id"),
        "progress_pct": progress_pct,
    }

    if item.get("type") == "series" and state.get("watched"):
        entry["series_watched_decoded"] = decode_watched_bitfield(state["watched"])

    return entry


def rebuild_views(
    items_by_id: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """
    Derive sorted `library` and `watch_activity` lists from the `items_by_id` map.
    Both lists are sorted newest-first by `last_watched` (with `added_at` as a
    secondary tie-breaker for unwatched or manually batch-marked "watched" items,
    which can happen when marking a whole TV show as watched, or when syncing
    watch status from an external service).
    """

    def sort_newest_first(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return sorted(
            items,
            key=lambda x: (x["last_watched"] or "", x["added_at"] or ""),
            reverse=True,
        )

    all_items = list(items_by_id.values())

    library = sort_newest_first([item for item in all_items if item["in_library"]])
    watch_activity = sort_newest_first(
        [
            item
            for item in all_items
            if item["last_watched"]
            or item["times_watched"] > 0
            or item["flagged_watched"] > 0
            or item["progress_pct"] > 0
        ]
    )

    return library, watch_activity


def load_existing_dump(path: Path) -> dict[str, Any]:
    """Load persisted sync state from disk, or initialize an empty schema."""
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))

    return {
        "sync_progress": {
            "datastore_meta_updated": 0,
            "fetch_items": [],
        },
        "user": {},
        "items_by_id": {},
        "library": [],
        "watch_activity": [],
    }


def sync_to_json(
    auth_key: str,
    output_path: Path = DEFAULT_OUTPUT_FILE,
    batch_size: int = DEFAULT_BATCH_SIZE,
    sleep_seconds: float = DEFAULT_SLEEP_SECONDS,
    debug: bool = False,
) -> dict[str, Any]:
    """
    Incrementally synchronize Stremio's `libraryItem` collection to a local JSON file.

    Sync & Resume State Machine:
    ----------------------------
    Stremio's `datastoreMeta` returns `[item_id, mtime_ms]` tuples for the entire
    collection in a single unpaginated call, where `mtime_ms` is a server-side
    Unix epoch timestamp in milliseconds.

    `sync_progress` persists two fields:
      - `datastore_meta_updated`: Highest `mtime_ms` seen in the `datastoreMeta`
        snapshot that initiated the current sync cycle.
      - `fetch_items`: Queue of item IDs still waiting to be fetched via `datastoreGet`.

    1. **Resume Mode (`fetch_items` is non-empty):**
       A previous run was interrupted (e.g., Ctrl-C or rate-limit pause). We skip
       `datastoreMeta`, leave `datastore_meta_updated` untouched, and drain the
       remaining `fetch_items` queue using whatever `batch_size` is configured now.
       Because `fetch_items` stores raw IDs rather than page offsets, changing
       `batch_size` between runs is completely safe.

    2. **Fresh Scan Mode (`fetch_items` is empty):**
       We call `datastoreMeta`, hard-prune any local IDs that disappeared from the
       server, and enqueue all IDs that are either new (`item_id not in items_by_id`)
       or modified after the previous meta-updated (`mtime_ms > prev_meta_updated_ms`).

    CRITICAL INVARIANT (`datastore_meta_updated`):
    `datastore_meta_updated` is ONLY updated at the START of a new sync (immediately
    after `datastoreMeta` returns), NEVER when batches finish. If a sync takes hours
    or days to drain across rate-limited runs, any item from an early batch that gets
    modified on the server mid-sync will have `mtime_ms > datastore_meta_updated` and
    is guaranteed to be picked up on the subsequent sync cycle.
    """
    if batch_size < 1:
        raise ValueError("batch_size must be at least 1")
    if sleep_seconds < 0:
        raise ValueError("sleep_seconds cannot be negative")

    debug_dir = output_path.resolve().parent / "api_responses" if debug else None
    dump = load_existing_dump(output_path)
    sync_progress: dict[str, Any] = dump["sync_progress"]
    items_by_id: dict[str, Any] = dump.get("items_by_id", {})

    def save_checkpoint(remaining_fetch_items: list[str]) -> None:
        """Rebuild derived views from `items_by_id` and atomically flush `dump` to disk."""
        library, watch_activity = rebuild_views(items_by_id)
        sync_progress["fetch_items"] = remaining_fetch_items
        dump["items_by_id"] = items_by_id
        dump["library"] = library
        dump["watch_activity"] = watch_activity
        save_atomic(output_path, dump)

    # 1. Verify token and refresh user profile.
    user = stremio_rpc("getUser", {"authKey": auth_key}, debug_dir=debug_dir)
    dump["user"] = {
        "id": user.get("_id"),
        "email": user.get("email"),
    }

    resumed = bool(sync_progress["fetch_items"])
    purged_count = 0

    if resumed:
        # Resume draining the persisted ID queue without touching `datastore_meta_updated`.
        print(
            f"Resuming interrupted sync: {len(sync_progress['fetch_items'])} items "
            f"remaining in queue (meta-updated: {sync_progress['datastore_meta_updated']})"
        )
    else:
        # Start a brand-new sync cycle by fetching the server's complete [id, mtime_ms] index.
        prev_meta_updated_ms: int = sync_progress.get("datastore_meta_updated", 0)
        meta_entries: list[list[Any]] = (
            stremio_rpc(
                "datastoreMeta",
                {"authKey": auth_key, "collection": "libraryItem"},
                debug_dir=debug_dir,
            )
            or []
        )

        remote_mtimes = {item_id: int(mtime) for item_id, mtime in meta_entries}
        newest_mtime = max(remote_mtimes.values(), default=0)

        print(
            f"Stremio's server database contains {len(remote_mtimes)} unique item IDs (last updated {newest_mtime})"
        )

        # Pruning can ONLY happen right after `datastoreMeta`, because that is the
        # only endpoint that returns the complete authoritative set of server IDs.
        # (Items soft-removed from the user's library stay in `datastoreMeta` with
        # `removed: true`; an ID disappearing completely means it was hard-purged.)
        purged_ids = [
            item_id for item_id in items_by_id if item_id not in remote_mtimes
        ]
        for item_id in purged_ids:
            items_by_id.pop(item_id, None)
        purged_count = len(purged_ids)

        # Determine which item IDs have been added/modified since our last sync.
        changed_ids = [
            item_id
            for item_id, mtime_ms in remote_mtimes.items()
            if item_id not in items_by_id or mtime_ms > prev_meta_updated_ms
        ]

        # IMPORTANT: Never modify `datastore_meta_updated` except right here at the START
        # of a new sync (immediately after the `datastoreMeta` call). Even if draining
        # `fetch_items` is paused or interrupted across days, preserving this snapshot
        # timestamp guarantees the next sync will never miss items modified during the delay.
        if remote_mtimes:
            sync_progress["datastore_meta_updated"] = newest_mtime

        # Persist the pruned state, newly discovered queue, and meta-updated before starting batch 1.
        save_checkpoint(changed_ids)

    # 2. Drain `fetch_items` in chunks of `batch_size`, checkpointing after every batch.
    fetched_this_run = 0
    remaining_ids: list[str] = list(sync_progress["fetch_items"])
    initial_queue_len = len(remaining_ids)

    if remaining_ids:
        total_batches = math.ceil(initial_queue_len / batch_size)
        for batch_num in range(1, total_batches + 1):
            batch = remaining_ids[:batch_size]
            batch_result = (
                stremio_rpc(
                    "datastoreGet",
                    {
                        "authKey": auth_key,
                        "collection": "libraryItem",
                        # NOTE: If an item was hard-purged on Stremio's backend
                        # while we request it here, they simply omit that ID
                        # in the response.
                        "ids": batch,
                        "all": False,
                    },
                    debug_dir=debug_dir,
                )
                or []
            )
            # NOTE: Stremio's backend very rarely contains duplicate records sharing the exact
            # same `_id`, so `len(batch_result)` (and `fetched_this_run`) can occasionally
            # exceed the number of requested IDs (e.g., 1364 records returned for 1363 IDs).
            fetched_this_run += len(batch_result)

            for raw_item in batch_result:
                normalized = normalize_item(raw_item)
                item_id = normalized["id"]
                existing = items_by_id.get(item_id)

                # When Stremio returns duplicate records for the same `_id` (or when
                # merging updates), keep whichever record has the newest `modified_at`
                # (`_mtime`) so an older duplicate never overwrites a newer entry.
                # We do a simple string comparison, since their ISO-8601 UTC strings
                # compare chronologically when zero-padded.
                if existing is None or (normalized["modified_at"] or "") >= (
                    existing.get("modified_at") or ""
                ):
                    items_by_id[item_id] = normalized

            # Pop completed batch IDs from the front of the queue and checkpoint immediately.
            # NOTE: It's important that we remove all queried items even if they weren't
            # in the response, since that just means Stremio's backend didn't have the data.
            remaining_ids = remaining_ids[len(batch) :]
            save_checkpoint(remaining_ids)

            print(
                f"Checkpoint saved: batch {batch_num}/{total_batches} "
                f"({fetched_this_run}/{initial_queue_len} fetched this run, "
                f"{len(remaining_ids)} remaining)"
            )

            if remaining_ids and sleep_seconds > 0:
                time.sleep(sleep_seconds)

    return {
        "resumed": resumed,
        "datastore_meta_updated": sync_progress["datastore_meta_updated"],
        "fetched_this_run": fetched_this_run,
        "remaining_count": len(sync_progress["fetch_items"]),
        "purged_count": purged_count,
        "library_count": len(dump["library"]),
        "activity_count": len(dump["watch_activity"]),
        "debug_dir": debug_dir,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Incrementally sync Stremio library and watch activity to JSON."
    )
    parser.add_argument(
        "auth_key",
        nargs="?",
        default=os.environ.get("STREMIO_AUTH_KEY"),
        help="Stremio web auth token (can also be set via STREMIO_AUTH_KEY env var).",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT_FILE,
        help=f"Path to the output JSON file (default: {DEFAULT_OUTPUT_FILE}).",
    )
    parser.add_argument(
        "-b",
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help=f"Number of items to fetch per datastoreGet call (default: {DEFAULT_BATCH_SIZE}).",
    )
    parser.add_argument(
        "-s",
        "--sleep",
        type=float,
        default=DEFAULT_SLEEP_SECONDS,
        help=f"Seconds to pause between batches (default: {DEFAULT_SLEEP_SECONDS}).",
    )
    parser.add_argument(
        "-d",
        "--debug",
        action="store_true",
        help="Write anonymized raw API responses to api_responses/api_<unixtime_ms>_<method>.json next to the output file.",
    )

    args = parser.parse_args()

    if not args.auth_key:
        parser.error(
            "auth_key is required (pass it as an argument or set STREMIO_AUTH_KEY)."
        )

    try:
        stats = sync_to_json(
            args.auth_key,
            output_path=args.output,
            batch_size=args.batch_size,
            sleep_seconds=args.sleep,
            debug=args.debug,
        )
    except KeyboardInterrupt:
        print(
            "\nInterrupted! Progress saved to checkpoint; re-run to resume remaining items."
        )
        raise SystemExit(130) from None

    print(f"\nResumed interrupted sync      : {stats['resumed']}")
    print(f"datastoreMeta last updated    : {stats['datastore_meta_updated']}")
    print(f"Fetched this run              : {stats['fetched_this_run']}")
    print(f"Remaining in queue            : {stats['remaining_count']}")
    print(f"Purged items                  : {stats['purged_count']}")
    print(f"Total library items           : {stats['library_count']}")
    print(f"Total watch activity          : {stats['activity_count']}")
    if stats["debug_dir"]:
        print(f"Raw debug logs directory      : {stats['debug_dir']}")
