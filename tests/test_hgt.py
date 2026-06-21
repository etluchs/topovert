"""Tests for the pure HGT tiling geometry."""

import math

import pytest

from topovert.hgt import DEM_RESOLUTIONS, Tile, tiles_for_bounds


def test_tile_name_northern_eastern():
    assert Tile(lat=47, lon=8).name == "N47E008.hgt"


def test_tile_name_zero_padding():
    assert Tile(lat=7, lon=5).name == "N07E005.hgt"


def test_tile_name_southern_western():
    assert Tile(lat=-1, lon=-135).name == "S01W135.hgt"


def test_tiles_for_bounds_single_cell():
    # Wholly inside the N47E008 cell.
    tiles = tiles_for_bounds(8.2, 47.1, 8.6, 47.7)
    assert [t.name for t in tiles] == ["N47E008.hgt"]


def test_tiles_for_bounds_spans_multiple_cells():
    tiles = tiles_for_bounds(7.5, 46.5, 9.2, 47.3)
    names = {t.name for t in tiles}
    # lon 7,8,9 x lat 46,47
    assert names == {
        "N46E007.hgt", "N46E008.hgt", "N46E009.hgt",
        "N47E007.hgt", "N47E008.hgt", "N47E009.hgt",
    }


def test_tiles_for_bounds_max_on_integer_boundary():
    # max exactly on 48.0 must NOT add a spurious tile starting at 48.
    tiles = tiles_for_bounds(8.0, 47.0, 9.0, 48.0)
    assert [t.name for t in tiles] == ["N47E008.hgt"]


def test_tiles_for_bounds_rejects_degenerate():
    with pytest.raises(ValueError):
        tiles_for_bounds(9.0, 47.0, 8.0, 48.0)


@pytest.mark.parametrize("resolution,samples", DEM_RESOLUTIONS.items())
def test_warp_extent_yields_exact_pixel_size(resolution, samples):
    """N samples across the padded extent must give pixel size 1/(N-1) exactly,
    with the first/last sample centres landing on the tile boundaries."""
    tile = Tile(lat=47, lon=8)
    min_lon, min_lat, max_lon, max_lat = tile.warp_extent(samples)

    px = (max_lon - min_lon) / samples
    assert px == pytest.approx(1.0 / (samples - 1), rel=0, abs=1e-15)

    # First pixel centre sits on the west boundary (lon=8), last on the east (lon=9).
    first_centre = min_lon + px / 2
    last_centre = max_lon - px / 2
    assert first_centre == pytest.approx(8.0, abs=1e-12)
    assert last_centre == pytest.approx(9.0, abs=1e-12)


def test_warp_extent_is_symmetric_padding():
    tile = Tile(lat=0, lon=0)
    min_lon, min_lat, max_lon, max_lat = tile.warp_extent(3601)
    half = 0.5 / 3600
    assert min_lon == pytest.approx(-half)
    assert max_lon == pytest.approx(1 + half)
    assert min_lat == pytest.approx(-half)
    assert max_lat == pytest.approx(1 + half)
