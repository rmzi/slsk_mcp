# The Brownsville Save — 2026-04-18

**Location:** jazz / strip club, Brownsville, NY
**Duration:** ~3h active build → ~24h by the time the last retry finished
**Outcome:** 21 of 22 playlist tracks on disk (18 FLACs + 3 MP3s), one peer-unreachable track genuinely missing

## The situation

Got to the club. Full DJ rig set up. Cables routed, monitors positioned, headphones on the ready. Then the realization: **I left all my music at home.** No USB drives. No local library. Nothing queued up. I hadn't finished the Tidal-to-Soulseek mirror MCP either — and I hadn't pre-downloaded the playlist I'd planned to spin.

The last jazz set of the night was running. I had that set's duration to make this work.

## What we did during the jazz set

Built and shipped `mirror_tidal_playlist` end-to-end from a standing start:

1. **Wired up the MCP for Claude Desktop** — SSH-to-private-fork URL resolution, Soulseek creds (had to cycle through one taken username before `habibitron_123456` registered), set `SLSK_DOWNLOAD_DIR` target, confirmed the pinned commit hash actually resolves.
2. **Built the Tidal integration** — `tidalapi` OAuth device flow, session caching at `~/.config/slsk-mcp/tidal.json`, playlist UUID parsing, track enumeration returning `{artist, title, duration, isrc}`.
3. **Built the orchestration pipeline** — `start_mirror(url) → job_id`, background asyncio task that searches Soulseek for each track (FLAC preferred, 320 CBR MP3 fallback, strict on quality), queues downloads, polls to completion, writes a `_failures.json`.
4. **Wired three new MCP tools** — `tidal_login_status`, `mirror_tidal_playlist`, `playlist_job_status`.
5. **Committed → pushed → bumped the hash** in Claude Desktop's config so the feature was live.
6. **Ran it** against the playlist `https://tidal.com/playlist/9e15ea77-2b39-49ee-88d9-0f91b75b4fe0` (a 22-track hip-hop / neo-soul mix titled `718`).

## What went sideways

- **External drive unmounted mid-run.** First target was `/Volumes/LEX/LOOSE MUSIC`. LEX got disconnected between runs; pipeline hit `FileNotFoundError` in `playlist_dir.mkdir`. Redirected to `~/Downloads/soulseek/`.
- **TLS cert bundle was zero-byte root-owned** in the venv (an earlier sandbox artifact). Skipped reinstall, pointed `REQUESTS_CA_BUNDLE` at the macOS system cert store and moved on.
- **Soulseek username collision.** First chosen handle `habibitron` was taken — server returned `INVALIDPASS`. Picked `habibitron_123456`, auto-registered on login.
- **Progress reporter lied.** `processed=0/22` for the entire download phase because the fire-and-forget match phase returned fast but the aggregate counter only ticked after `asyncio.gather` completed. Real downloads were landing the whole time.
- **Eviction race.** `SoulseekWrapper._FINISHED_TTL = 60s` evicted finished downloads before `_wait_for_downloads` started polling — the poller saw `not_found` and marked 13 successful transfers as failures. Real successes were on disk; the report was wrong.
- **Shallow missing-tracks analysis.** First validation just substring-matched lowercase-artist + lowercase-title in filenames. Caller caught the weakness. Rewrote it to read FLAC Vorbis tags with `mutagen` and fuzzy-match on actual embedded `ARTIST` / `TITLE` fields. That moved the "real missing" count from 5 down to 4.
- **1500 lines of peer-connection noise.** Other Soulseek peers kept dialing us; our listener binds loopback-only (security hardening), so aioslsk logged every rejection. Log hygiene goes on the TODO list.

## What was actually delivered

**18 of 22 tracks verified on disk** via tag-based reconciliation by the time the club needed them:

| Track | Status |
|---|---|
| A$AP Rocky – Electric Body | ✓ |
| Megan Thee Stallion – Pimpin | ✓ |
| JACKBOYS – GATTI | ✓ (duplicate) |
| ICYTWAT – Bandulu Lover | ✓ |
| Duke Deuce – Crunk Ain't Dead | ✓ |
| Blood Orange – Gold Teeth | ✗ (peer accepted queue but never transferred — two retries) |
| Mike Jones – Back Then | ✓ |
| Dem Franchize Boyz – White Tee | ✓ |
| Lil Scrappy – Head Bussa | ✓ |
| Ying Yang Twins – Salt Shaker | ✓ |
| Trillville – Some Cut | ✓ |
| Novelist – Dun Know (feat. Prem) | ✓ MP3 (via title-only search in ultra-loose retry) |
| The Cool Kids – ALL OR NOTHING | ✓ |
| Larry June – Watering My Plants | ✓ |
| SoGone SoFlexy – Big Wide Body | ✓ MP3 (ultra-loose retry) |
| Rochelle Jordan – Already | ✓ |
| Sharon Forrester – Love Don't Live Here Any More | ✓ |
| Wizkid – All For Love (feat. Bucie) | ✓ MP3 (ultra-loose retry) |
| Sango – Quanto Tempo | ✓ |
| TEE MANGO – This Is Where I'll Stay | ✓ |
| Donald Byrd – Think Twice | ✓ (the 1974 original, which sampled…) |
| Erykah Badu – Think Twice | ✓ (…which Erykah Badu flipped in 2019) |

**~450 MB of FLACs + a few MP3s in `~/Downloads/soulseek/718/`.** 21 of 22. Only Blood Orange – Gold Teeth held out — Soulseek peers acknowledged the queue twice but never sent a byte, probably a NAT-on-both-sides collision that no retry of ours would solve without proper inbound connectivity.

## Iteration loop that got us there

1. First full run → reported 7 finished / 15 failed, but tag-based validation showed 18 really on disk (eviction race bug).
2. Loose retry (FLAC any / MP3 ≥200 kbps) → 1 new success, 2 still-missing had no candidates even at loose bar.
3. Ultra-loose retry (FLAC any / MP3 any bitrate / title-only fallback / `max_queue_size=80` / `min_filesize=500 KB`) → picked up the three remaining real fails as MP3s.

## Why this moment matters

This is the first time the tool saved a gig it was barely finished in time to save. Built it, broke it, fixed it, validated it, all during a single jazz set in a strip club in Brownsville. The constraints imposed by the room and the clock kept the work honest — no bikeshedding, no premature optimization, just ship the thing that makes the night work.

## Commits shipped tonight

- `dee0e97` — Security hardening: network defaults, prompt injection, size cap, error leakage (earlier today, pre-gig)
- `3e00f72` — feat: Tidal playlist mirror tools (the one that saved the set)

## Known bugs to fix later (not tonight)

1. **Eviction race in `_wait_for_downloads`.** Fix: have `_process_track` await its own download completion instead of fire-and-forget, so the match phase semaphore naturally gates track lifetimes.
2. **Downloads land in `SLSK_DOWNLOAD_DIR` not in the playlist subfolder.** `slsk_client.download` doesn't know about the per-job target dir my orchestrator creates.
3. **Progress counter stays at 0/22 during the download phase.** Same root cause as #1.
4. **Peer-connect logging noise.** Suppress `aioslsk` `ConnectToPeer` rejection logs, or at least rate-limit them.
5. **Filename-substring validation.** Replace with tag-based reconciliation everywhere, not just in the post-hoc check.
6. **No retry on mid-transfer failure.** Wizkid failed mid-transfer on a FLAC candidate; pipeline didn't re-search and fall back to MP3.

## Preference captured

Saved to memory (`feedback_skip_sandbox.md`): default to `dangerouslyDisableSandbox: true` for local ops in this project. The sandbox allowlist blocks nearly every real operation and the retry loop wastes time. Not an override of security posture — just an acknowledgement that for *this* project on *this* machine, the friction isn't earning its keep.
