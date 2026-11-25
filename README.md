# Stremio API Reference

A robust reference implementation for syncing your Stremio library and watch history, featuring batching, rate-limiting, crash-safe resumability, and a total guarantee that no items are ever missed. See `src/stremio_sync.py` for the heavily documented implementation, ready to run as-is or port to other languages.

- Author: [Arcitec](https://github.com/Arcitec/stremio_api_tools)
- License: [The Unlicense](LICENSE)

## Usage:

- Ensure that the [uv Package Manager](https://docs.astral.sh/uv/getting-started/installation/) is installed on your system.

- Get your Stremio `authKey` by logging into your [account settings](https://www.stremio.com/acc-settings), pressing F12 to open the Developer Tools, and then running this console command:

```js
JSON.parse(localStorage.getItem("profile")).authKey;
```

- Fetch the entire Stremio library (saved items + watch history), with incremental updates and resume support (by running it again later):

```bash
uv run ./src/stremio_sync.py --batch-size 500 --sleep 5 --debug 'your authKey'
```

- Look at some fun library statistics:

```bash
uv run ./src/library_stats.py
```

- Developers: Check for duplicate item IDs in Stremio's backend database (requires `--debug` flag above, and is only accurate after a full, fresh non-incremental sync):

```bash
uv run ./src/find_api_dupes.py
```
