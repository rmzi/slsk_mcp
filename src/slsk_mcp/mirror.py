"""Tidal → Soulseek mirror pipeline.

Takes a Tidal playlist URL, enumerates tracks, searches Soulseek for each,
picks the best candidate matching a strict quality policy (FLAC, falling
back to 320 CBR MP3; overridable via ``formats``), downloads into the
playlist directory through the existing ``SoulseekWrapper``, and writes a
failures log there.

Resilience:
- Empty searches are retried with backoff (``SLSK_MIRROR_SEARCH_ATTEMPTS``),
  since Soulseek sometimes returns nothing transiently.
- Each track keeps a ranked list of candidates. If a download fails, sits
  in a remote queue with no bytes for ``SLSK_MIRROR_QUEUE_TIMEOUT`` seconds,
  or stalls for ``SLSK_MIRROR_STALL_TIMEOUT`` seconds, it is cancelled and
  the next candidate (up to ``SLSK_MIRROR_MAX_CANDIDATES``) is tried.
- Each download is watched by its own track task from the moment it
  starts, so finished records can't be purged before they're observed.

Design notes:
- Jobs are tracked in an in-memory dict keyed by a short random id. On MCP
  restart they're lost; that's deliberate for V1 — simpler than persisting
  state, and the Soulseek wrapper already expires its own download records
  on restart anyway.
- Per-track work runs under a small semaphore (default 3). The download
  phase inside ``SoulseekWrapper.download`` has its own semaphore
  (``SLSK_MAX_CONCURRENT_DL``), so downloads serialize naturally even when
  many matches land at once.
- The matcher is intentionally conservative: strict 320 for MP3 (unknown
  bitrate rejected), ±5s duration tolerance when both sides report it,
  and a fuzzy filename ratio. Better to log a miss than accept junk.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid as uuid_lib
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, List, Optional

from .models import SearchResultItem
from .slsk_client import SoulseekWrapper
from .tidal import TidalTrack, fetch_playlist_tracks, load_session, parse_playlist_url

logger = logging.getLogger("slsk_mcp.mirror")

_DURATION_TOLERANCE_SEC = 5
_FILENAME_FUZZY_THRESHOLD = 0.55
_MAX_CONCURRENT_MATCH = int(os.environ.get("SLSK_MIRROR_CONCURRENCY", "3"))
_MIN_FILESIZE = 1_000_000  # 1 MiB — reject obvious garbage
_SEARCH_ATTEMPTS = int(os.environ.get("SLSK_MIRROR_SEARCH_ATTEMPTS", "3"))
# Wait before the 2nd, 3rd... search attempt. Observed Soulseek search
# outages last minutes, not seconds, so the second wait is long.
_SEARCH_BACKOFF_SEC = (30, 120)
_MAX_CANDIDATES = int(os.environ.get("SLSK_MIRROR_MAX_CANDIDATES", "3"))
_QUEUE_TIMEOUT_SEC = int(os.environ.get("SLSK_MIRROR_QUEUE_TIMEOUT", "300"))
_STALL_TIMEOUT_SEC = int(os.environ.get("SLSK_MIRROR_STALL_TIMEOUT", "120"))
_TRACK_POLL_SEC = 5
_SUPPORTED_FORMATS = ("flac", "mp3")
DEFAULT_FORMATS = ("flac", "mp3")


def normalize_formats(formats: Optional[List[str]]) -> List[str]:
    """Validate and de-duplicate a format preference list (order preserved).

    ``None`` means the default policy (FLAC, then 320 MP3). Raises
    ``ValueError`` on empty lists or unsupported formats.
    """
    if formats is None:
        return list(DEFAULT_FORMATS)
    out: List[str] = []
    for f in formats:
        norm = str(f).strip().lower().lstrip(".")
        if norm not in _SUPPORTED_FORMATS:
            raise ValueError(
                f"Unsupported format {f!r}; allowed: {', '.join(_SUPPORTED_FORMATS)}"
            )
        if norm not in out:
            out.append(norm)
    if not out:
        raise ValueError("formats must contain at least one of: flac, mp3")
    return out

# Featured-artist noise that shows up in Tidal titles but rarely in filenames.
_FEAT_RE = re.compile(
    r"\s*[\(\[]\s*(feat\.?|ft\.?|featuring)[^)\]]*[\)\]]|\s+(feat\.?|ft\.?|featuring)\s+.*$",
    re.IGNORECASE,
)
# Characters that are dodgy in macOS filenames.
_UNSAFE_CHARS = re.compile(r"[\x00-\x1f/:\\]+")


@dataclass
class TrackOutcome:
    artist: str
    title: str
    isrc: Optional[str]
    status: str  # queued (waiting) | downloading | finished | failed | skipped
    reason: Optional[str] = None
    download_id: Optional[str] = None
    local_path: Optional[str] = None


@dataclass
class JobState:
    id: str
    playlist_name: str
    playlist_dir: str
    total_tracks: int
    status: str = "running"  # running | complete | error
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    tracks: List[TrackOutcome] = field(default_factory=list)

    def summary(self) -> Dict[str, Any]:
        counts: Dict[str, int] = {}
        for t in self.tracks:
            counts[t.status] = counts.get(t.status, 0) + 1
        return {
            "job_id": self.id,
            "playlist_name": self.playlist_name,
            "playlist_dir": self.playlist_dir,
            "status": self.status,
            "total_tracks": self.total_tracks,
            "processed": sum(
                1 for t in self.tracks if t.status in ("finished", "failed", "skipped")
            ),
            "counts": counts,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "tracks": [asdict(t) for t in self.tracks],
        }


_JOBS: Dict[str, JobState] = {}


def _safe_folder_name(name: str) -> str:
    cleaned = _UNSAFE_CHARS.sub("_", name).strip()
    return cleaned[:120] or "tidal_playlist"


def _strip_feat(text: str) -> str:
    return _FEAT_RE.sub("", text).strip()


def _basename(path: str) -> str:
    return path.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]


def _score_candidate(
    candidate: SearchResultItem, track: TidalTrack
) -> Optional[float]:
    """Return a match score in [0, 1], or None to reject."""
    if (
        candidate.duration_sec is not None
        and track.duration_sec is not None
        and abs(candidate.duration_sec - track.duration_sec) > _DURATION_TOLERANCE_SEC
    ):
        return None

    artist = _strip_feat(track.artist).lower()
    title = _strip_feat(track.title).lower()
    ref = f"{artist} {title}".strip()
    if not ref:
        return None

    basename = _basename(candidate.filename).lower()
    full_path = candidate.filename.lower()

    # Max ratio across basename and full-path — full path matters when the
    # artist name lives in a parent directory rather than the filename.
    ratio = max(
        SequenceMatcher(None, ref, basename).ratio(),
        SequenceMatcher(None, ref, full_path).ratio(),
    )
    if ratio < _FILENAME_FUZZY_THRESHOLD:
        return None
    return ratio


def _rank_candidates(
    candidates: List[SearchResultItem], track: TidalTrack, fmt: str
) -> List[SearchResultItem]:
    """Return candidates of ``fmt`` that pass the quality policy, best first.

    MP3 must be exactly 320 kbps (unknown bitrate and VBR rejected).
    Duplicate ids are collapsed.
    """
    scored: List[tuple] = []
    seen: set = set()
    for c in candidates:
        if c.extension != fmt or c.id in seen:
            continue
        # Strict: exact 320. Unknown bitrate rejected (per user spec).
        if fmt == "mp3" and c.bitrate != 320:
            continue
        score = _score_candidate(c, track)
        if score is None:
            continue
        seen.add(c.id)
        scored.append((score, c))
    scored.sort(key=lambda sc: sc[0], reverse=True)
    return [c for _, c in scored]


def _pick_flac(
    candidates: List[SearchResultItem], track: TidalTrack
) -> Optional[SearchResultItem]:
    ranked = _rank_candidates(candidates, track, "flac")
    return ranked[0] if ranked else None


def _pick_mp3_320(
    candidates: List[SearchResultItem], track: TidalTrack
) -> Optional[SearchResultItem]:
    ranked = _rank_candidates(candidates, track, "mp3")
    return ranked[0] if ranked else None


_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_BRACKETED = re.compile(r"\s*[\(\[][^)\]]*[\)\]]")


def _norm(text: str) -> str:
    """Lowercase and drop everything but letters/digits, for loose matching."""
    return _NON_ALNUM.sub("", text.lower())


def _already_downloaded(
    playlist_dir: Path, track: TidalTrack, formats: Optional[List[str]] = None
) -> Optional[Path]:
    """Return path to an existing file that looks like this track, else None.

    Only files whose extension is in ``formats`` count — an existing FLAC
    does not satisfy an MP3-only mirror. Matching is on the normalised
    title alone (filenames often omit the artist, e.g. "03 Skkrtt.mp3");
    the search is scoped to this playlist's folder, so collisions are rare.
    """
    allowed = {f".{f}" for f in (formats or DEFAULT_FORMATS)}
    if not playlist_dir.exists():
        return None
    base = _strip_feat(track.title)
    # Also accept the title without bracketed suffixes, e.g. "(Original Mix)".
    variants = {_norm(base), _norm(_BRACKETED.sub("", base))}
    variants = {v for v in variants if len(v) >= 3}
    if not variants:
        return None
    for p in playlist_dir.iterdir():
        if p.is_file() and p.suffix.lower() in allowed:
            stem_n = _norm(p.stem)
            if any(v in stem_n for v in variants):
                return p
    return None


def _search_kwargs(fmt: str) -> Dict[str, Any]:
    kw: Dict[str, Any] = dict(
        extensions=[fmt],
        min_filesize=_MIN_FILESIZE,
        free_slots_only=True,
        max_queue_size=50,
    )
    if fmt == "mp3":
        kw["min_bitrate"] = 320
    return kw


async def _search_with_retry(
    slsk: SoulseekWrapper, query: str, fmt: str
) -> List[SearchResultItem]:
    """Search, retrying with backoff when Soulseek returns nothing.

    An empty result is ambiguous — the file may not exist, or the network
    may be briefly throttling/dropping search responses. Retrying a couple
    of times is cheap compared with permanently failing a track.
    """
    results: List[SearchResultItem] = []
    for attempt in range(_SEARCH_ATTEMPTS):
        results = await slsk.search(query=query, **_search_kwargs(fmt))
        if results:
            return results
        if attempt < _SEARCH_ATTEMPTS - 1:
            delay = _SEARCH_BACKOFF_SEC[min(attempt, len(_SEARCH_BACKOFF_SEC) - 1)]
            logger.info(
                "Empty search for %r (%s), retry %d/%d in %ss",
                query, fmt, attempt + 1, _SEARCH_ATTEMPTS - 1, delay,
            )
            await asyncio.sleep(delay)
    return results


async def _await_transfer(
    slsk: SoulseekWrapper, file_id: str, local_path: Optional[str]
) -> str:
    """Watch one transfer. Returns "finished" or a short failure reason.

    Gives up with "queued too long" if no bytes arrive within
    ``_QUEUE_TIMEOUT_SEC`` and "stalled" if bytes stop moving for
    ``_STALL_TIMEOUT_SEC``. A purged/unknown record is resolved by checking
    whether the final file exists on disk.
    """
    start = time.monotonic()
    last_bytes = 0
    last_progress = start
    while True:
        await asyncio.sleep(_TRACK_POLL_SEC)
        ds = slsk.download_status(file_id)
        if ds.status == "finished":
            return "finished"
        if ds.status in ("not_found", "session_expired"):
            if local_path and Path(local_path).exists():
                return "finished"
            return f"download {ds.status}"
        if ds.status in ("failed", "cancelled"):
            return f"download {ds.status}"
        now = time.monotonic()
        received = ds.received_bytes or 0
        if received > last_bytes:
            last_bytes, last_progress = received, now
        if last_bytes == 0 and now - start > _QUEUE_TIMEOUT_SEC:
            return "queued too long"
        if last_bytes > 0 and now - last_progress > _STALL_TIMEOUT_SEC:
            return "stalled"


async def _abandon(slsk: SoulseekWrapper, file_id: str, local_path: Optional[str]) -> None:
    """Cancel a dead transfer and remove its partial file."""
    try:
        await slsk.cancel_download(file_id)
    except Exception:
        logger.exception("cancel_download failed for %s", file_id)
    if local_path:
        part = Path(local_path + ".part")
        try:
            if part.exists():
                part.unlink()
        except OSError:
            pass


async def _process_track(
    track: TidalTrack,
    slsk: SoulseekWrapper,
    playlist_dir: Path,
    formats: Optional[List[str]] = None,
    search_sem: Optional[asyncio.Semaphore] = None,
    outcome: Optional[TrackOutcome] = None,
) -> TrackOutcome:
    """Search for, download and verify one track. Mutates/returns ``outcome``.

    The search phase runs under ``search_sem`` (if given) so only a few
    tracks hit the network search at once; the download phase is throttled
    by ``SoulseekWrapper``'s own download semaphore.
    """
    formats = list(formats or DEFAULT_FORMATS)
    if outcome is None:
        outcome = TrackOutcome(
            artist=track.artist, title=track.title, isrc=track.isrc, status="queued"
        )

    if not track.artist or not track.title:
        outcome.status, outcome.reason = "failed", "missing artist or title from Tidal"
        return outcome

    existing = _already_downloaded(playlist_dir, track, formats)
    if existing is not None:
        outcome.status = "skipped"
        outcome.reason = "already present in playlist dir"
        outcome.local_path = str(existing)
        return outcome

    query = f"{_strip_feat(track.artist)} {_strip_feat(track.title)}".strip()

    candidates: List[SearchResultItem] = []
    if search_sem is not None:
        await search_sem.acquire()
    try:
        for fmt in formats:
            results = await _search_with_retry(slsk, query, fmt)
            candidates = _rank_candidates(results, track, fmt)
            if candidates:
                break
    finally:
        if search_sem is not None:
            search_sem.release()

    if not candidates:
        labels = {"flac": "FLAC", "mp3": "320 MP3"}
        wanted = " or ".join(labels[f] for f in formats)
        outcome.status = "failed"
        outcome.reason = f"no {wanted} candidate passed filters"
        return outcome

    attempts: List[str] = []
    for cand in candidates[:_MAX_CANDIDATES]:
        ok, message, local_path, _filesize = await slsk.download(
            cand.id, dest_dir=playlist_dir
        )
        if not ok:
            attempts.append(f"{cand.username}: setup failed ({message})")
            continue
        outcome.status = "downloading"
        outcome.download_id = cand.id
        outcome.local_path = local_path
        result = await _await_transfer(slsk, cand.id, local_path)
        if result == "finished":
            outcome.status, outcome.reason = "finished", None
            return outcome
        attempts.append(f"{cand.username}: {result}")
        logger.info("Candidate failed for %s - %s: %s", track.artist, track.title, result)
        await _abandon(slsk, cand.id, local_path)

    outcome.status = "failed"
    outcome.download_id = None
    outcome.local_path = None
    outcome.reason = f"all {len(attempts)} candidate(s) failed: " + "; ".join(attempts)
    return outcome


def _write_failures_log(playlist_dir: Path, outcomes: List[TrackOutcome]) -> None:
    failures = [
        {
            "artist": o.artist,
            "title": o.title,
            "isrc": o.isrc,
            "reason": o.reason,
        }
        for o in outcomes
        if o.status == "failed"
    ]
    try:
        (playlist_dir / "_failures.json").write_text(
            json.dumps(failures, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError:
        logger.exception("Failed to write _failures.json")


async def _run_job(
    job: JobState,
    tracks: List[TidalTrack],
    slsk: SoulseekWrapper,
    formats: Optional[List[str]] = None,
) -> None:
    try:
        playlist_dir = Path(job.playlist_dir)
        playlist_dir.mkdir(parents=True, exist_ok=True)

        sem = asyncio.Semaphore(_MAX_CONCURRENT_MATCH)
        # Pre-populate so playlist_job_status shows every track from the start.
        job.tracks = [
            TrackOutcome(artist=t.artist, title=t.title, isrc=t.isrc, status="queued")
            for t in tracks
        ]

        async def _one(track: TidalTrack, outcome: TrackOutcome) -> None:
            try:
                await _process_track(
                    track, slsk, playlist_dir, formats, search_sem=sem, outcome=outcome
                )
            except Exception as exc:
                logger.exception(
                    "Track processing crashed: %s - %s", track.artist, track.title
                )
                outcome.status = "failed"
                outcome.reason = f"crash: {type(exc).__name__}"

        await asyncio.gather(*(_one(t, o) for t, o in zip(tracks, job.tracks)))
        _write_failures_log(playlist_dir, job.tracks)

        job.status = "complete"
    except Exception:
        logger.exception("Mirror job crashed")
        job.status = "error"
    finally:
        job.finished_at = time.time()


async def start_mirror(
    url: str,
    slsk: SoulseekWrapper,
    download_root: Path,
    formats: Optional[List[str]] = None,
) -> str:
    """Kick off a playlist mirror job. Returns a short job_id.

    ``formats`` is an ordered preference list drawn from {"flac", "mp3"};
    default is FLAC then 320 MP3. Pass ["mp3"] for MP3-only.
    """
    formats = normalize_formats(formats)
    session = load_session()
    if session is None:
        raise RuntimeError(
            "No cached Tidal session. Run the `slsk-mcp-tidal-login` CLI on the "
            "host machine once to complete OAuth; subsequent calls will reuse "
            "the refresh token saved under ~/.config/slsk-mcp/tidal.json."
        )

    playlist_id = parse_playlist_url(url)
    playlist_obj, tracks = fetch_playlist_tracks(session, playlist_id)
    playlist_name = getattr(playlist_obj, "name", None) or "tidal_playlist"

    job_id = uuid_lib.uuid4().hex[:12]
    playlist_dir = download_root / _safe_folder_name(playlist_name)
    job = JobState(
        id=job_id,
        playlist_name=playlist_name,
        playlist_dir=str(playlist_dir),
        total_tracks=len(tracks),
    )
    _JOBS[job_id] = job

    asyncio.create_task(_run_job(job, tracks, slsk, formats))
    return job_id


def get_job(job_id: str) -> Optional[Dict[str, Any]]:
    job = _JOBS.get(job_id)
    return job.summary() if job else None


def list_jobs() -> List[Dict[str, Any]]:
    return [job.summary() for job in _JOBS.values()]
