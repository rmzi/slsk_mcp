"""Tests for the mirror pipeline: format policy, search retry, candidate
fallback, stall detection, and already-downloaded detection."""

import asyncio
from pathlib import Path
from typing import Dict, List, Optional

import pytest

from slsk_mcp import mirror
from slsk_mcp.models import DownloadStatusResponse, SearchResultItem
from slsk_mcp.tidal import TidalTrack


@pytest.fixture(autouse=True)
def fast_timers(monkeypatch):
    monkeypatch.setattr(mirror, "_TRACK_POLL_SEC", 0)
    monkeypatch.setattr(mirror, "_SEARCH_BACKOFF_SEC", (0,))
    monkeypatch.setattr(mirror, "_QUEUE_TIMEOUT_SEC", 0.05)
    monkeypatch.setattr(mirror, "_STALL_TIMEOUT_SEC", 0.05)


def _track(title: str = "So What") -> TidalTrack:
    return TidalTrack(
        artist="Miles Davis", title=title, album=None, duration_sec=None, isrc=None
    )


def _item(item_id: str, ext: str, bitrate=None, user: str = "u") -> SearchResultItem:
    return SearchResultItem(
        id=item_id,
        username=user,
        filename=f"Miles Davis\\Kind of Blue\\Miles Davis - So What.{ext}",
        filesize=10_000_000,
        extension=ext,
        bitrate=bitrate,
    )


class FakeSlsk:
    """Minimal SoulseekWrapper stand-in.

    ``results_by_ext`` maps extension -> list of result lists, one per
    search call (the last list repeats). ``behaviour`` maps download id ->
    sequence of (status, received_bytes) returned by successive
    download_status calls (the last entry repeats).
    """

    def __init__(self, results_by_ext, behaviour=None, dest: Optional[Path] = None):
        self.results_by_ext = results_by_ext
        self.behaviour: Dict[str, list] = behaviour or {}
        self.searched: List[str] = []
        self.downloaded: List[str] = []
        self.cancelled: List[str] = []
        self.dest_dirs: List[Path] = []
        self._polls: Dict[str, int] = {}

    async def search_counted(self, query, extensions, **kw):
        """Results are pre-filter here; mimic the client's 320 filter so the
        raw count can be non-zero while the filtered list is empty."""
        ext = extensions[0]
        n = sum(1 for e in self.searched if e == ext)
        self.searched.append(ext)
        seq = self.results_by_ext.get(ext, [[]])
        raw = seq[min(n, len(seq) - 1)]
        min_br = kw.get("min_bitrate")
        kept = [r for r in raw if not min_br or (r.bitrate or 0) >= min_br]
        return kept, len(raw)

    async def download(self, item_id, dest_dir=None):
        self.downloaded.append(item_id)
        self.dest_dirs.append(dest_dir)
        path = Path(dest_dir) / f"{item_id}.mp3"
        return True, "ok", str(path), 0

    def download_status(self, item_id):
        seq = self.behaviour.get(item_id, [("finished", 10)])
        i = self._polls.get(item_id, 0)
        self._polls[item_id] = i + 1
        status, received = seq[min(i, len(seq) - 1)]
        return DownloadStatusResponse(status=status, received_bytes=received)

    async def cancel_download(self, item_id):
        self.cancelled.append(item_id)
        return {"status": "cancelled"}


def _run(coro):
    return asyncio.run(coro)


# ── format policy ────────────────────────────────────────────────────────


def test_normalize_formats_default():
    assert mirror.normalize_formats(None) == ["flac", "mp3"]


def test_normalize_formats_dedup_and_case():
    assert mirror.normalize_formats(["MP3", ".mp3", "flac"]) == ["mp3", "flac"]


@pytest.mark.parametrize("bad", [[], ["wav"], ["ogg", "mp3"]])
def test_normalize_formats_rejects(bad):
    with pytest.raises(ValueError):
        mirror.normalize_formats(bad)


def test_mp3_only_skips_flac_search(tmp_path: Path):
    slsk = FakeSlsk({"flac": [[_item("f1", "flac")]], "mp3": [[_item("m1", "mp3", 320)]]})
    out = _run(mirror._process_track(_track(), slsk, tmp_path, ["mp3"]))
    assert slsk.searched == ["mp3"]
    assert out.download_id == "m1"
    assert out.status == "finished"


def test_default_prefers_flac(tmp_path: Path):
    slsk = FakeSlsk({"flac": [[_item("f1", "flac")]], "mp3": [[_item("m1", "mp3", 320)]]})
    out = _run(mirror._process_track(_track(), slsk, tmp_path))
    assert slsk.searched == ["flac"]
    assert out.download_id == "f1"


def test_downloads_go_to_playlist_dir(tmp_path: Path):
    slsk = FakeSlsk({"mp3": [[_item("m1", "mp3", 320)]]})
    _run(mirror._process_track(_track(), slsk, tmp_path, ["mp3"]))
    assert slsk.dest_dirs == [tmp_path]


def test_mp3_only_failure_reason(tmp_path: Path):
    slsk = FakeSlsk({"mp3": [[_item("m1", "mp3", 256)]]})
    out = _run(mirror._process_track(_track(), slsk, tmp_path, ["mp3"]))
    assert out.status == "failed"
    assert out.reason == "no 320 MP3 candidate passed filters"


# ── search retry ─────────────────────────────────────────────────────────


def test_empty_search_is_retried(tmp_path: Path):
    slsk = FakeSlsk({"mp3": [[], [], [_item("m1", "mp3", 320)]]})
    out = _run(mirror._process_track(_track(), slsk, tmp_path, ["mp3"]))
    assert slsk.searched == ["mp3", "mp3", "mp3"]
    assert out.status == "finished"


def test_search_retry_gives_up(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(mirror, "_SEARCH_ATTEMPTS", 2)
    slsk = FakeSlsk({"mp3": [[]]})
    out = _run(mirror._process_track(_track(), slsk, tmp_path, ["mp3"]))
    assert slsk.searched == ["mp3", "mp3"]
    assert out.status == "failed"


def test_nonempty_search_not_retried(tmp_path: Path):
    # Results exist but none pass the 320 filter: no point re-searching.
    slsk = FakeSlsk({"mp3": [[_item("m1", "mp3", 192)]]})
    _run(mirror._process_track(_track(), slsk, tmp_path, ["mp3"]))
    assert slsk.searched == ["mp3"]


def test_raw_results_filtered_to_nothing_not_retried(tmp_path: Path):
    # Regression: retries used to fire whenever the *filtered* list was
    # empty, re-running identical searches for 2.5 minutes per track.
    slsk = FakeSlsk({"mp3": [[_item("m1", "mp3", 128), _item("m2", "mp3", 192)]]})
    out = _run(mirror._process_track(_track(), slsk, tmp_path, ["mp3"]))
    assert out.status == "failed"
    assert slsk.searched == ["mp3"]


# ── candidate fallback / stall detection ─────────────────────────────────


def _three():
    return [[_item("a", "mp3", 320, "ua"), _item("b", "mp3", 320, "ub"), _item("c", "mp3", 320, "uc")]]


def test_failed_download_falls_back_to_next_candidate(tmp_path: Path):
    slsk = FakeSlsk({"mp3": _three()}, behaviour={"a": [("failed", 0)]})
    out = _run(mirror._process_track(_track(), slsk, tmp_path, ["mp3"]))
    assert slsk.downloaded == ["a", "b"]
    assert out.status == "finished"
    assert out.download_id == "b"


def test_queued_forever_is_cancelled_and_falls_back(tmp_path: Path):
    slsk = FakeSlsk({"mp3": _three()}, behaviour={"a": [("queued", 0)]})
    out = _run(mirror._process_track(_track(), slsk, tmp_path, ["mp3"]))
    assert "a" in slsk.cancelled
    assert out.download_id == "b"
    assert out.status == "finished"


def test_stalled_transfer_falls_back(tmp_path: Path):
    slsk = FakeSlsk({"mp3": _three()}, behaviour={"a": [("downloading", 100)]})
    out = _run(mirror._process_track(_track(), slsk, tmp_path, ["mp3"]))
    assert "a" in slsk.cancelled
    assert out.download_id == "b"


def test_all_candidates_fail(tmp_path: Path):
    slsk = FakeSlsk(
        {"mp3": _three()},
        behaviour={k: [("failed", 0)] for k in "abc"},
    )
    out = _run(mirror._process_track(_track(), slsk, tmp_path, ["mp3"]))
    assert slsk.downloaded == ["a", "b", "c"]
    assert out.status == "failed"
    assert out.reason.startswith("all 3 candidate(s) failed")


def test_max_candidates_respected(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(mirror, "_MAX_CANDIDATES", 2)
    slsk = FakeSlsk({"mp3": _three()}, behaviour={k: [("failed", 0)] for k in "abc"})
    _run(mirror._process_track(_track(), slsk, tmp_path, ["mp3"]))
    assert slsk.downloaded == ["a", "b"]


def test_purged_record_with_file_on_disk_counts_as_finished(tmp_path: Path):
    # Regression: the client forgets finished downloads after 60s; the mirror
    # used to report those as "download not_found" even though the file existed.
    (tmp_path / "a.mp3").write_bytes(b"x")
    slsk = FakeSlsk({"mp3": _three()}, behaviour={"a": [("not_found", None)]})
    out = _run(mirror._process_track(_track(), slsk, tmp_path, ["mp3"]))
    assert out.status == "finished"
    assert slsk.downloaded == ["a"]


def test_purged_record_without_file_falls_back(tmp_path: Path):
    slsk = FakeSlsk({"mp3": _three()}, behaviour={"a": [("not_found", None)]})
    out = _run(mirror._process_track(_track(), slsk, tmp_path, ["mp3"]))
    assert out.download_id == "b"


# ── already-downloaded detection ─────────────────────────────────────────


def test_existing_flac_does_not_satisfy_mp3_only(tmp_path: Path):
    (tmp_path / "Miles Davis - So What.flac").write_bytes(b"x")
    assert mirror._already_downloaded(tmp_path, _track(), ["mp3"]) is None
    assert mirror._already_downloaded(tmp_path, _track(), ["flac"]) is not None


def test_existing_match_by_title_only(tmp_path: Path):
    (tmp_path / "03 Skkrtt.mp3").write_bytes(b"x")
    t = TidalTrack(artist="DJ Orange Julius", title="Skkrtt", album=None, duration_sec=None, isrc=None)
    assert mirror._already_downloaded(tmp_path, t, ["mp3"]) is not None


def test_existing_match_ignores_punctuation(tmp_path: Path):
    (tmp_path / "Duke Deuce - I Ain_t Worried Bout It (Dirty).mp3").write_bytes(b"x")
    t = TidalTrack(artist="Duke Deuce", title="I AIN'T WORRIED BOUT IT", album=None, duration_sec=None, isrc=None)
    assert mirror._already_downloaded(tmp_path, t, ["mp3"]) is not None


def test_part_file_is_not_already_downloaded(tmp_path: Path):
    (tmp_path / "Miles Davis - So What.mp3.part").write_bytes(b"x")
    assert mirror._already_downloaded(tmp_path, _track(), ["mp3"]) is None


def test_skip_short_circuits_search(tmp_path: Path):
    (tmp_path / "So What.mp3").write_bytes(b"x")
    slsk = FakeSlsk({"mp3": _three()})
    out = _run(mirror._process_track(_track(), slsk, tmp_path, ["mp3"]))
    assert out.status == "skipped"
    assert slsk.searched == []


# ── job level ────────────────────────────────────────────────────────────


def test_run_job_counts_and_failures_log(tmp_path: Path):
    tracks = [_track("So What"), _track("Blue in Green")]
    results = {"mp3": [[_item("a", "mp3", 320)]]}
    slsk = FakeSlsk(results)
    job = mirror.JobState(id="j", playlist_name="p", playlist_dir=str(tmp_path), total_tracks=2)
    _run(mirror._run_job(job, tracks, slsk, ["mp3"]))
    summary = job.summary()
    assert summary["status"] == "complete"
    assert summary["processed"] == 2
    assert (tmp_path / "_failures.json").exists()


def test_existing_match_without_bracketed_suffix(tmp_path: Path):
    (tmp_path / "Breaka - Get Your Sweat On.mp3").write_bytes(b"x")
    t = TidalTrack(artist="Breaka", title="Get Your Sweat On (Original Mix)", album=None, duration_sec=None, isrc=None)
    assert mirror._already_downloaded(tmp_path, t, ["mp3"]) is not None
