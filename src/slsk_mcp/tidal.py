"""Tidal integration: OAuth session persistence and playlist enumeration.

Tidal has no public API. This module uses `tidalapi` (community-maintained,
reverse-engineered) with its device-code OAuth flow. The refresh token is
cached to ``~/.config/slsk-mcp/tidal.json`` on the first run, and subsequent
calls load silently.

The ``tidalapi`` import is deferred so that a user who doesn't exercise Tidal
features never pays its import cost — and a missing install produces a clear
runtime error instead of a server startup crash.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional, Tuple

logger = logging.getLogger("slsk_mcp.tidal")

_CONFIG_DIR = Path.home() / ".config" / "slsk-mcp"
_SESSION_FILE = _CONFIG_DIR / "tidal.json"

# Tidal playlist IDs are standard UUIDs in URLs like:
#   https://tidal.com/browse/playlist/<uuid>
#   https://tidal.com/playlist/<uuid>
_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)


@dataclass
class TidalTrack:
    artist: str
    title: str
    album: Optional[str]
    duration_sec: Optional[int]
    isrc: Optional[str]


def get_session_path() -> Path:
    return _SESSION_FILE


def ensure_config_dir() -> None:
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def load_session() -> Optional[Any]:
    """Return a logged-in ``tidalapi.Session`` or None if no valid cache.

    Returns None (rather than raising) on any of:
    - ``tidalapi`` not installed
    - no cached session file
    - cached refresh token rejected by Tidal (expired/revoked)
    """
    try:
        import tidalapi
    except ImportError:
        logger.error("tidalapi not installed")
        return None

    if not _SESSION_FILE.exists():
        return None

    session = tidalapi.Session()
    try:
        loaded = session.load_session_from_file(_SESSION_FILE)
    except Exception:
        logger.exception("Failed to load Tidal session")
        return None

    if not loaded or not session.check_login():
        return None
    return session


def parse_playlist_url(url_or_id: str) -> str:
    """Extract a Tidal playlist UUID from a URL or raw ID."""
    m = _UUID_RE.search(url_or_id.strip())
    if not m:
        raise ValueError(f"no playlist UUID found in: {url_or_id!r}")
    return m.group(0)


def fetch_playlist_tracks(
    session: Any, playlist_id: str
) -> Tuple[Any, List[TidalTrack]]:
    """Return ``(playlist_object, [TidalTrack, ...])`` for a playlist UUID."""
    playlist = session.playlist(playlist_id)
    tracks: List[TidalTrack] = []
    for t in playlist.tracks():
        artist_obj = getattr(t, "artist", None)
        primary_artist = getattr(artist_obj, "name", None) if artist_obj else None
        if not primary_artist:
            artists = getattr(t, "artists", None) or ()
            if artists:
                primary_artist = getattr(artists[0], "name", "") or ""
        album_obj = getattr(t, "album", None)
        album_name = getattr(album_obj, "name", None) if album_obj else None
        tracks.append(
            TidalTrack(
                artist=(primary_artist or "").strip(),
                title=(getattr(t, "name", "") or "").strip(),
                album=album_name,
                duration_sec=getattr(t, "duration", None),
                isrc=getattr(t, "isrc", None),
            )
        )
    return playlist, tracks
