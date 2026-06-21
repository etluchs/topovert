"""Tests for GDAL/mkgmap argv construction (no subprocess execution)."""

from pathlib import Path

from topovert import gdal_tools, mkgmap
from topovert.hgt import Tile


def test_build_vrt_cmd():
    cmd = gdal_tools.build_vrt_cmd([Path("a.tif"), Path("b.tif")], Path("m.vrt"))
    assert cmd == ["gdalbuildvrt", "-overwrite", "m.vrt", "a.tif", "b.tif"]


def test_warp_tile_cmd_has_reprojection_and_grid():
    tile = Tile(lat=47, lon=8)
    cmd = gdal_tools.warp_tile_cmd(
        Path("mosaic.vrt"), Path("out.tif"), tile.warp_extent(3601), 3601,
        source_epsg=2056, resampling="bilinear",
    )
    assert "gdalwarp" == cmd[0]
    assert "EPSG:2056" in cmd  # source CRS
    assert "EPSG:4326" in cmd  # target CRS (WGS84 for Garmin)
    # exact tile size requested
    ts_idx = cmd.index("-ts")
    assert cmd[ts_idx + 1 : ts_idx + 3] == ["3601", "3601"]
    # out-of-source pixels become SRTM voids, not fake sea-level 0
    assert cmd[cmd.index("-dstnodata") + 1] == "-32768"
    assert cmd[-2:] == ["mosaic.vrt", "out.tif"]


def test_translate_hgt_cmd_uses_srtmhgt():
    cmd = gdal_tools.translate_hgt_cmd(Path("t.tif"), Path("N47E008.hgt"))
    assert "-of" in cmd and "SRTMHGT" in cmd
    assert cmd[-2:] == ["t.tif", "N47E008.hgt"]


def test_build_img_cmd_points_dem_at_directory():
    cmd = mkgmap.build_img_cmd(
        "java", Path("mkgmap.jar"), Path("bounds.osm"), Path("hgt"), Path("out"),
        map_name="swiss",
    )
    assert cmd[:3] == ["java", "-jar", "mkgmap.jar"]
    assert "--dem=hgt" in cmd
    assert "--output-dir=out" in cmd
    assert cmd[-1] == "bounds.osm"
