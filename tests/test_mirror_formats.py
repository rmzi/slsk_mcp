"""Tests for the mirror format-preference policy."""

import asyncio
from pathlib import Path
from typing import List

import pytest

from slsk_mcp import mirror
from slsk_mcp.models import SearchResultItem
from slsk_mcp.tidal import TidalTrack


def _track() -> TidalTrack:
    return TidalTrack(
        artist="Miles Davis", title="So What", album=None, duration_sec=None, isrc=None
    )


class FakeSlsk:
    def __init__(self, results_by_ext):
        self.results_by_ext = results_by_ext
        self.searched: List[str] = []
        self.downloaded: List[str] = []

    async def search(self, query, extensions, **kw):
        ext = extensions[0]
        self.searched.append(ext)
        return self.results_by_ext.get(ext, [])

    async def download(self, item_id):
        self.downloaded.append(item_id)
        return True, "ok", f"/tmp/{item_id}", 0


def _item(item_id: str, ext: str, bitrate=None) -> SearchResultItem:
    return SearchResultItem(
        id=item_id,
        username="u",
        filename=f"Miles Davis\\Kind of Blue\\Miles Davis - So What.{ext}",
        filesize=10_000_000,
        extension=ext,
        bitrate=bitrate,
    )


def test_normalize_formats_default():
    assert mirror.normalize_formats(None) == ["flac", "mp3"]


def test_normalize_formats_dedup_and_case():
    assert mirror.normalize_formats(["MP3", ".mp3", "flac"]) == ["mp3", "flac"]


@pytest.mark.parametrize("bad", [[], ["wav"], ["ogg", "mp3"]])
def test_normalize_formats_rejects(bad):
    with pytest.raises(ValueError):
        mirror.normalize_formats(bad)


def test_mp3_only_skips_flac_search(tmp_path: Path):
    slsk = FakeSlsk({
        "flac": [_item("f1", "flac")],
        "mp3": [_item("m1", "mp3", 320)],
    })
    out = asyncio.run(mirror._process_track(_track(), slsk, tmp_path, ["mp3"]))
    assert slsk.searched == ["mp3"]
    assert out.download_id == "m1"
    assert out.status == "queued"


def test_default_prefers_flac(tmp_path: Path):
    slsk = FakeSlsk({
        "flac": [_item("f1", "flac")],
        "mp3": [_item("m1", "mp3", 320)],
    })
    out = asyncio.run(mirror._process_track(_track(), slsk, tmp_path))
    assert slsk.searched == ["flac"]
    assert out.download_id == "f1"


def test_mp3_only_failure_reason(tmp_path: Path):
    slsk = FakeSlsk({"mp3": [_item("m1", "mp3", 256)]})
    out = asyncio.run(mirror._process_track(_track(), slsk, tmp_path, ["mp3"]))
    assert out.status == "failed"
    assert out.reason == "no 320 MP3 candidate passed filters"


def test_existing_flac_does_not_satisfy_mp3_only(tmp_path: Path):
    (tmp_path / "Miles Davis - So What.flac").write_bytes(b"x")
    assert mirror._already_downloaded(tmp_path, _track(), ["mp3"]) is None
    assert mirror._already_downloaded(tmp_path, _track(), ["flac"]) is not None
