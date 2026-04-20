# Changelog

All notable changes to slsk-mcp are documented in this file. Format is loosely
based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and versioning
is [SemVer](https://semver.org/spec/v2.0.0.html).

## [0.2.0] — 2026-04-20

### Added
- **Tidal playlist mirror.** Three new MCP tools — `tidal_login_status`,
  `mirror_tidal_playlist(url)`, and `playlist_job_status(job_id)` — that take
  a Tidal playlist URL, fetch its tracks, search Soulseek for each, and queue
  downloads under a playlist-named subfolder of `SLSK_DOWNLOAD_DIR`. FLAC is
  preferred; strict 320 CBR MP3 is the only fallback. Unmatched tracks are
  written to `_failures.json`.
- **Tidal OAuth bootstrap CLI** (`slsk-mcp-tidal-login`). Runs the device-code
  flow once on the host; the refresh token is cached at
  `~/.config/slsk-mcp/tidal.json` and reused silently thereafter.
- **`tidalapi>=0.8,<1.0`** added to dependencies.
- `docs/diary/` — narrative session logs, starting with the Brownsville gig
  save where this feature shipped under pressure.

### Changed
- `CLAUDE.md` documents the new tools, the one-time OAuth bootstrap, and
  accurate testing instructions (the prior "no test suite" note was stale).
- Development workflow in `CLAUDE.md` now reflects that the active fork lives
  at private `rmzi/slsk_mcp`, so the `uvx` pin uses
  `git+ssh://git@github.com/rmzi/slsk_mcp.git@<hash>` rather than the original
  public upstream URL template.

### Known issues
The mirror pipeline has three rough edges identified during the first real
run and scheduled for a follow-up release:
1. Status reporter lies. `SoulseekWrapper._FINISHED_TTL=60s` evicts finished
   downloads before the mirror's poll loop sees them, surfacing successes as
   `not_found` failures. Fix path: make `_process_track` await its own
   download rather than fire-and-forget.
2. Downloads land flat in `SLSK_DOWNLOAD_DIR`, not in the playlist subfolder
   the mirror creates. `slsk_client.download` doesn't accept a per-call target.
3. `processed=N/total` stays at `0` during the download phase because the
   aggregate counter only increments after `asyncio.gather` completes.

## [0.1.0] — initial

- Soulseek MCP tools: `search`, `download`, `download_status`, `cancel_download`,
  `list_downloads`, `connection_health`, `peer_status`, `get_config`.
- `.part` suffix during transfer, renamed only on successful completion.
- Security hardening: loopback-only listener by default, explicit UPnP opt-in,
  10 GiB default filesize cap, peer-string sanitization before reaching the LLM,
  typed-only error messages (no exception body leakage).
