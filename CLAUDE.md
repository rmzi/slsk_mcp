# slsk-mcp

Soulseek MCP server for Claude Desktop / Claude Code.

## Development workflow

- After any code change, you MUST: commit, push to GitHub, and update the pinned commit hash in `~/Library/Application Support/Claude/claude_desktop_config.json` under `mcpServers.local_music_finder.args` (the `git+ssh://...@<hash>` value). Claude Desktop must be fully quit (`Cmd+Q`) and relaunched to pick up the new version.
- The MCP is installed via `uvx --from git+ssh://git@github.com/rmzi/slsk_mcp.git@<commit> slsk-mcp` — there is no PyPI release. SSH form is required because `rmzi/slsk_mcp` (where `security-hardening` and other local branches live) is private; Claude Desktop inherits the user's SSH agent via launchd, so no extra auth setup is needed on this machine. The original public upstream is `voidtype/slsk_mcp`, but it does not carry the hardening commits.
- The commit hash must exist on a branch pushed to `origin` (i.e. `rmzi/slsk_mcp`). `uvx` will refuse to resolve a SHA that is only local.

## Key files

- `src/slsk_mcp/server.py` — MCP tool definitions (search, download, download_status, mirror_tidal_playlist, etc.)
- `src/slsk_mcp/slsk_client.py` — Soulseek wrapper (login, download management, connection health)
- `src/slsk_mcp/models.py` — Pydantic response models
- `src/slsk_mcp/tidal.py` — Tidal OAuth session load/save, playlist enumeration
- `src/slsk_mcp/tidal_login_cli.py` — one-time `slsk-mcp-tidal-login` bootstrap CLI
- `src/slsk_mcp/mirror.py` — Tidal → Soulseek pipeline, match rules, job state

## MCP tools for the AI

### `get_config`
Returns runtime settings (download directory, listen port, concurrency limits, username). Call this when you need to know where files are saved or what the current configuration is. No arguments required. Does not require a connection.

### `.part` file convention
Downloads are written as `filename.flac.part` during transfer. The `.part` suffix is removed only on successful completion. If a file still has `.part`, the download failed or is still in progress — do not treat it as a finished file.

### Tidal playlist mirroring

Three tools work together:

- `tidal_login_status()` — reports whether the cached OAuth token at `~/.config/slsk-mcp/tidal.json` is valid. If `logged_in=false`, tell the user to run the `slsk-mcp-tidal-login` CLI once on their host machine; the MCP subprocess can't prompt for OAuth interactively.
- `mirror_tidal_playlist(url, formats=None)` — accepts a Tidal playlist URL (e.g. `https://tidal.com/browse/playlist/<uuid>`) or bare UUID. Returns immediately with a `job_id` while the pipeline runs in the background. Downloads are written to a playlist-named subfolder of `SLSK_DOWNLOAD_DIR`.
- `playlist_job_status(job_id)` — poll for progress. Each track has status `queued | downloading | finished | failed | skipped`. When the job completes, a `_failures.json` file is written under the playlist folder listing tracks that couldn't be matched.

Quality policy (strict): FLAC (any bit depth/sample rate) preferred. Fallback is MP3 at exactly 320 kbps CBR — unknown bitrate and V0/V2 are rejected. Override with `formats` (ordered preference from `"flac"`, `"mp3"`): `["mp3"]` = 320 MP3 only, `["flac"]` = FLAC only. With `["mp3"]`, an existing FLAC in the playlist folder does not count as already downloaded. Anything that can't meet this bar lands in the failures log rather than being silently accepted. Loosen via `_FILENAME_FUZZY_THRESHOLD` in `mirror.py` if too many real matches are being rejected.

Concurrency: up to `SLSK_MIRROR_CONCURRENCY` (default 3) per-track match phases run in parallel; the download phase is throttled by the existing `SLSK_MAX_CONCURRENT_DL`.

Retries and fallback (all env-tunable):
- Searches that return zero raw results are retried (`SLSK_MIRROR_SEARCH_ATTEMPTS`, default 3; waits 30s then 120s). Soulseek has transient search outages lasting minutes where every query returns nothing. If results come back but none pass the quality filters, there is no retry — it would return the same thing.
- Each track keeps a ranked candidate list. A download that fails, receives no bytes for `SLSK_MIRROR_QUEUE_TIMEOUT` (300s), or stalls for `SLSK_MIRROR_STALL_TIMEOUT` (120s) is cancelled (its `.part` removed) and the next candidate is tried, up to `SLSK_MIRROR_MAX_CANDIDATES` (3).
- Downloads are written into the playlist folder itself, and each track task watches its own transfer from the start, so the client's 60s finished-record TTL can't cause false `not_found` failures.
- Search outages: after `SLSK_MIRROR_OUTAGE_THRESHOLD` (5) consecutive zero-result searches across the job, the mirror forces a Soulseek re-login (`SoulseekWrapper.force_relogin`; at most once per 120s, capped by `SLSK_MIRROR_MAX_RELOGINS`, default 5). Observed: a session's searches can die for many minutes while a fresh login works at once. Downloads killed by the re-login retry the same peer once.
- "Already downloaded" = a file in the playlist folder whose name contains the normalised track title (with or without bracketed suffixes). Re-running a playlist only fetches what's missing.

### One-time Tidal OAuth bootstrap

Run once on the host machine (not from inside Claude Desktop):

```bash
uvx --from 'git+ssh://git@github.com/rmzi/slsk_mcp.git@<hash>' slsk-mcp-tidal-login
```

Follow the printed URL, authorize the device on tidal.com, and the refresh token is saved. This only needs to happen once per machine until the token is revoked.

## Testing

- Run `uv sync --extra dev && .venv/bin/python -m pytest tests/ -q` from the project root. Unit tests cover model schemas, the Soulseek wrapper's pure helpers (`_parse_id`, `_extract_attrs`, `_sanitize_for_llm`), security hardening, and the no-exception-leak `_generic_error_message`.
- Integration verification: restart Claude Desktop and call `connection_health`.
- Tidal mirror is not unit-tested yet — smoke-check by creating a short test playlist on tidal.com and calling `mirror_tidal_playlist(url)` + `playlist_job_status(job_id)` end-to-end.
