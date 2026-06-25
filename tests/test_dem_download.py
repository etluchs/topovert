"""Tests for the Copernicus GLO-30 DEM auto-download (no network).

The fetch itself is monkeypatched, so these exercise tile enumeration, URL/name
construction, area parsing, caching/skip logic and error paths without touching
the toolchain or the AWS mirror.
"""

import urllib.error

import pytest

from topovert import TopovertError, dem_download
from topovert.hgt import Tile


def test_tile_basename_and_url_match_copernicus_layout():
    tile = Tile(lat=47, lon=7)
    assert dem_download._tile_basename(tile) == "Copernicus_DSM_COG_10_N47_00_E007_00_DEM"
    assert dem_download.tile_url(tile) == (
        "https://copernicus-dem-30m.s3.amazonaws.com/"
        "Copernicus_DSM_COG_10_N47_00_E007_00_DEM/"
        "Copernicus_DSM_COG_10_N47_00_E007_00_DEM.tif"
    )


def test_tile_basename_southern_western_hemisphere():
    assert (
        dem_download._tile_basename(Tile(lat=-1, lon=-135))
        == "Copernicus_DSM_COG_10_S01_00_W135_00_DEM"
    )


def test_parse_area_named_is_case_insensitive():
    assert dem_download.parse_area("Switzerland") == dem_download.NAMED_AREAS["switzerland"]
    assert dem_download.parse_area("CH") == dem_download.NAMED_AREAS["ch"]


def test_parse_area_accepts_bbox_string():
    assert dem_download.parse_area("6.0,45.8,10.5,47.9") == (6.0, 45.8, 10.5, 47.9)


def test_parse_area_rejects_degenerate_bbox():
    with pytest.raises(TopovertError, match="degenerate"):
        dem_download.parse_area("10,47,6,45")


def test_parse_area_rejects_unknown():
    with pytest.raises(TopovertError, match="unknown --dem-area"):
        dem_download.parse_area("narnia")


def test_switzerland_resolves_to_the_18_country_tiles():
    tiles = dem_download.tiles_for_area("switzerland")
    # lon 5..10 (6) x lat 45..47 (3) = 18 one-degree cells (README's count)
    assert len(tiles) == 18
    assert Tile(lat=47, lon=7) in tiles
    assert {t.lon for t in tiles} == {5, 6, 7, 8, 9, 10}
    assert {t.lat for t in tiles} == {45, 46, 47}


def test_download_area_caches_and_skips_voids(tmp_path, monkeypatch):
    """Existing tiles are reused; a 404/void cell is skipped, not fatal."""
    # Pre-seed one tile as already cached.
    cached = tmp_path / f"{dem_download._tile_basename(Tile(lat=45, lon=5))}.tif"
    cached.write_bytes(b"\x00")

    fetched: list[str] = []

    def fake_fetch(url, dst):
        # Pretend the western-most remaining cells are ocean voids (404 -> False).
        if "W" in url or "_E005_" in url:
            return False
        fetched.append(url)
        dst.write_bytes(b"\x00")
        return True

    monkeypatch.setattr(dem_download, "_fetch", fake_fetch)

    out = dem_download.download_area("6.0,45.0,7.9,46.9", dst_dir=tmp_path)
    assert out == tmp_path
    # 2x2 cells (lon 6,7 x lat 45,46); none is a void here, all fetched fresh.
    assert len(fetched) == 4
    # The pre-seeded tile was not re-downloaded.
    assert cached.read_bytes() == b"\x00"


def test_download_area_raises_when_no_tiles_available(tmp_path, monkeypatch):
    monkeypatch.setattr(dem_download, "_fetch", lambda url, dst: False)
    with pytest.raises(TopovertError, match="no Copernicus DEM tiles available"):
        dem_download.download_area("6.0,45.0,6.9,45.9", dst_dir=tmp_path)


def test_fetch_retries_transient_failure_then_succeeds(tmp_path, monkeypatch):
    """A flaky/incomplete read is retried; a later success still lands the tile."""
    monkeypatch.setattr(dem_download.time, "sleep", lambda _s: None)
    calls = {"n": 0}

    def flaky_urlretrieve(url, dst):
        calls["n"] += 1
        if calls["n"] == 1:
            raise urllib.error.ContentTooShortError("incomplete read", None)
        from pathlib import Path
        Path(dst).write_bytes(b"\x00")

    monkeypatch.setattr(dem_download.urllib.request, "urlretrieve", flaky_urlretrieve)
    dst = tmp_path / "tile.tif"
    assert dem_download._fetch("http://example/tile.tif", dst) is True
    assert calls["n"] == 2
    assert dst.read_bytes() == b"\x00"
    assert not dst.with_suffix(".tif.part").exists()  # no leftover partial


def test_fetch_does_not_retry_404(tmp_path, monkeypatch):
    """A 404 is a definitive 'no such tile' (ocean/void), returned without retry."""
    monkeypatch.setattr(dem_download.time, "sleep", lambda _s: None)
    calls = {"n": 0}

    def not_found(url, dst):
        calls["n"] += 1
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

    monkeypatch.setattr(dem_download.urllib.request, "urlretrieve", not_found)
    assert dem_download._fetch("http://example/x.tif", tmp_path / "x.tif") is False
    assert calls["n"] == 1


def test_fetch_gives_up_after_retries(tmp_path, monkeypatch):
    monkeypatch.setattr(dem_download.time, "sleep", lambda _s: None)

    def always_fail(url, dst):
        raise urllib.error.ContentTooShortError("incomplete read", None)

    monkeypatch.setattr(dem_download.urllib.request, "urlretrieve", always_fail)
    with pytest.raises(TopovertError, match="after 4 attempts"):
        dem_download._fetch("http://example/x.tif", tmp_path / "x.tif")
