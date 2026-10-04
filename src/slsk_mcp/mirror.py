"""Tidal → Soulseek mirror pipeline.

Takes a Tidal playlist URL, enumerates tracks, searches Soulseek for each,
picks the best candidate matching a strict quality policy (FLAC, falling
back to 320 CBR MP3), queues downloads through the existing
``SoulseekWrapper``, polls to completion, and writes a failures log to the
playlist directory.

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
_POLL_INTERVAL_SEC = 15
_MIN_FILESIZE = 1_000_000  # 1 MiB — reject obvious garbage
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
    status: str  # queued | downloading | finished | failed | skipped
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
            "processed": len(self.tracks),
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


def _pick_flac(
    candidates: List[SearchResultItem], track: TidalTrack
) -> Optional[SearchResultItem]:
    best: Optional[SearchResultItem] = None
    best_score = -1.0
    for c in candidates:
        if c.extension != "flac":
            continue
        score = _score_candidate(c, track)
        if score is None:
            continue
        if score > best_score:
            best, best_score = c, score
    return best


def _pick_mp3_320(
    candidates: List[SearchResultItem], track: TidalTrack
) -> Optional[SearchResultItem]:
    best: Optional[SearchResultItem] = None
    best_score = -1.0
    for c in candidates:
        if c.extension != "mp3":
            continue
        # Strict: exact 320. Unknown bitrate rejected (per user spec).
        if c.bitrate != 320:
            continue
        score = _score_candidate(c, track)
        if score is None:
            continue
        if score > best_score:
            best, best_score = c, score
    return best


def _already_downloaded(
    playlist_dir: Path, track: TidalTrack, formats: Optional[List[str]] = None
) -> Optional[Path]:
    """Return path to an existing file that looks like this track, else None.

    Only files whose extension is in ``formats`` count — an existing FLAC
    does not satisfy an MP3-only mirror.
    """
    allowed = {f".{f}" for f in (formats or DEFAULT_FORMATS)}
    if not playlist_dir.exists():
        return None
    artist_low = _strip_feat(track.artist).lower()
    title_low = _strip_feat(track.title).lower()
    if not artist_low or not title_low:
        return None
    for p in playlist_dir.iterdir():
        if p.is_file() and p.suffix.lower() in allowed:
            stem_low = p.stem.lower()
            if artist_low in stem_low and title_low in stem_low:
                return p
    return None


async def _process_track(
    track: TidalTrack,
    slsk: SoulseekWrapper,
    playlist_dir: Path,
    formats: Optional[List[str]] = None,
) -> TrackOutcome:
    formats = list(formats or DEFAULT_FORMATS)
    if not track.artist or not track.title:
        return TrackOutcome(
            artist=track.artist,
            title=track.title,
            isrc=track.isrc,
            status="failed",
            reason="missing artist or title from Tidal",
        )

    existing = _already_downloaded(playlist_dir, track, formats)
    if existing is not None:
        return TrackOutcome(
            artist=track.artist,
            title=track.title,
            isrc=track.isrc,
            status="skipped",
            reason="already present in playlist dir",
            local_path=str(existing),
        )

    query = f"{_strip_feat(track.artist)} {_strip_feat(track.title)}".strip()

    pick: Optional[SearchResultItem] = None
    for fmt in formats:
        if fmt == "flac":
            flac_results = await slsk.search(
                query=query,
                extensions=["flac"],
                min_filesize=_MIN_FILESIZE,
                free_slots_only=True,
                max_queue_size=50,
            )
            pick = _pick_flac(flac_results, track)
        elif fmt == "mp3":
            mp3_results = await slsk.search(
                query=query,
                extensions=["mp3"],
                min_bitrate=320,
                min_filesize=_MIN_FILESIZE,
                free_slots_only=True,
                max_queue_size=50,
            )
            pick = _pick_mp3_320(mp3_results, track)
        if pick is not None:
            break

    if pick is None:
        labels = {"flac": "FLAC", "mp3": "320 MP3"}
        wanted = " or ".join(labels[f] for f in formats)
        return TrackOutcome(
            artist=track.artist,
            title=track.title,
            isrc=track.isrc,
            status="failed",
            reason=f"no {wanted} candidate passed filters",
        )

    ok, message, local_path, _filesize = await slsk.download(pick.id)
    if not ok:
        return TrackOutcome(
            artist=track.artist,
            title=track.title,
            isrc=track.isrc,
            status="failed",
            reason=f"download setup failed: {message}",
        )

    return TrackOutcome(
        artist=track.artist,
        title=track.title,
        isrc=track.isrc,
        status="queued",
        download_id=pick.id,
        local_path=local_path,
    )


async def _wait_for_downloads(
    outcomes: List[TrackOutcome], slsk: SoulseekWrapper
) -> None:
    """Poll every queued download until it resolves to finished or failed."""
    pending = [o for o in outcomes if o.status == "queued" and o.download_id]
    while pending:
        await asyncio.sleep(_POLL_INTERVAL_SEC)
        still_pending: List[TrackOutcome] = []
        for o in pending:
            assert o.download_id is not None
            ds = slsk.download_status(o.download_id)
            if ds.status == "finished":
                o.status = "finished"
                o.local_path = ds.local_path
            elif ds.status in ("failed", "cancelled", "not_found", "session_expired"):
                o.status = "failed"
                o.reason = f"download {ds.status}"
            else:
                o.status = "downloading"
                still_pending.append(o)
        pending = still_pending


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

        async def _one(track: TidalTrack) -> TrackOutcome:
            async with sem:
                try:
                    return await _process_track(track, slsk, playlist_dir, formats)
                except Exception as exc:
                    logger.exception(
                        "Track processing crashed: %s - %s",
                        track.artist, track.title,
                    )
                    return TrackOutcome(
                        artist=track.artist,
                        title=track.title,
                        isrc=track.isrc,
                        status="failed",
                        reason=f"crash: {type(exc).__name__}",
                    )

        outcomes = await asyncio.gather(*(_one(t) for t in tracks))
        job.tracks = outcomes

        await _wait_for_downloads(outcomes, slsk)
        _write_failures_log(playlist_dir, outcomes)

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
