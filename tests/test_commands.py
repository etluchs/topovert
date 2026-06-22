"""Tests for GDAL/mkgmap argv construction (no subprocess execution)."""

from pathlib import Path

from topovert import gdal_tools, mkgmap, splitter
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


def test_warp_tile_cmd_autodetects_source_crs_when_epsg_none():
    """source_epsg=None drops -s_srs so gdalwarp reads the file's own CRS."""
    tile = Tile(lat=47, lon=8)
    cmd = gdal_tools.warp_tile_cmd(
        Path("mosaic.vrt"), Path("out.tif"), tile.warp_extent(3601), 3601,
        source_epsg=None, resampling="bilinear",
    )
    assert "-s_srs" not in cmd
    assert cmd[cmd.index("-t_srs") + 1] == "EPSG:4326"  # target still forced


def test_translate_hgt_cmd_uses_srtmhgt():
    cmd = gdal_tools.translate_hgt_cmd(Path("t.tif"), Path("N47E008.hgt"))
    assert "-of" in cmd and "SRTMHGT" in cmd
    assert cmd[-2:] == ["t.tif", "N47E008.hgt"]


def test_build_img_cmd_single_tile_with_dem():
    cmd = mkgmap.build_img_cmd(
        "java", Path("mkgmap.jar"), [Path("bounds.osm")], Path("out"),
        map_name="swiss", hgt_dir=Path("hgt"),
    )
    assert cmd[:3] == ["java", "-jar", "mkgmap.jar"]
    assert "--dem=hgt" in cmd
    assert "--output-dir=out" in cmd
    # must build a self-contained gmapsupp.img (not a bare detail tile)
    assert "--gmapsupp" in cmd
    assert f"--family-id={mkgmap.FAMILY_ID}" in cmd
    assert f"--mapname={mkgmap.MAP_NUMBER}" in cmd  # single tile is named
    # Swiss style applied, and the TYP follows the osm input (last arg).
    assert f"--style-file={mkgmap.STYLE_DIR}" in cmd
    assert cmd[-1] == str(mkgmap.TYP_FILE)
    assert cmd[-2] == "bounds.osm"


def test_build_img_cmd_style_can_be_disabled():
    cmd = mkgmap.build_img_cmd(
        "java", Path("mkgmap.jar"), [Path("features.osm")], Path("out"),
        map_name="swiss", style_dir=None, typ_file=None,
    )
    assert not any(a.startswith("--style-file=") for a in cmd)
    assert cmd[-1] == "features.osm"  # no TYP appended


def test_build_img_cmd_vector_only_has_no_dem():
    cmd = mkgmap.build_img_cmd(
        "java", Path("mkgmap.jar"), [Path("features.osm")], Path("out"),
        map_name="swiss",  # hgt_dir defaults to None
    )
    assert not any(a.startswith("--dem=") for a in cmd)
    assert "--gmapsupp" in cmd


def test_build_img_cmd_split_tiles_drop_mapname():
    inputs = [Path("63240001.osm.pbf"), Path("63240002.osm.pbf")]
    cmd = mkgmap.build_img_cmd(
        "java", Path("mkgmap.jar"), inputs, Path("out"),
        map_name="swiss", mapname=None,  # tiles carry their own numbers
    )
    assert not any(a.startswith("--mapname=") for a in cmd)
    # tiles precede the TYP, which mkgmap binds last
    assert cmd[-3:] == ["63240001.osm.pbf", "63240002.osm.pbf", str(mkgmap.TYP_FILE)]


def test_split_cmd_outputs_pbf_tiles():
    cmd = splitter.split_cmd(
        "java", Path("splitter.jar"), Path("features.osm"), Path("split"),
        max_nodes=1_600_000, mapid="63240001",
    )
    assert cmd[:3] == ["java", "-jar", "splitter.jar"]
    assert "--output=pbf" in cmd
    assert "--output-dir=split" in cmd
    assert "--max-nodes=1600000" in cmd
    assert "--mapid=63240001" in cmd
    assert cmd[-1] == "features.osm"
