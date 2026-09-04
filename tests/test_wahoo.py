"""Tests for the Wahoo (mapsforge) backend: tile geometry, argv, packaging.

None of these need Java, osmosis or the network — the pieces that talk to the
toolchain are argv builders, and the packaging steps are pure stdlib.
"""

import lzma
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from topovert import osm, wahoo


# --- zoom-8 tile geometry ----------------------------------------------------

def test_tile_bbox_matches_slippy_grid():
    # z8 tile 133/89 is one of the tiles covering central Switzerland.
    tile = wahoo.MapTile(x=133, y=89)
    assert tile.west == pytest.approx(7.03125)
    assert tile.east == pytest.approx(8.4375)
    assert tile.north == pytest.approx(47.989922, abs=1e-5)
    assert tile.south == pytest.approx(47.040182, abs=1e-5)
    # map-writer wants minLat,minLon,maxLat,maxLon — not the GDAL order.
    south, west, north, east = (float(v) for v in tile.bbox_arg().split(","))
    assert (south, west) == pytest.approx((tile.south, tile.west), abs=1e-6)
    assert (north, east) == pytest.approx((tile.north, tile.east), abs=1e-6)


def test_tile_rel_path_is_the_on_device_layout():
    assert wahoo.MapTile(x=134, y=90).rel_path() == Path("134/90.map.lzma")
    assert wahoo.MapTile(x=134, y=90).rel_path(".map") == Path("134/90.map")


def test_tiles_for_bounds_covers_switzerland():
    tiles = wahoo.tiles_for_bounds((5.9, 45.8, 10.5, 47.8))
    assert len(tiles) == 12  # 4 columns x 3 rows at z8
    assert {t.x for t in tiles} == {132, 133, 134, 135}
    assert {t.y for t in tiles} == {89, 90, 91}


def test_tiles_for_bounds_small_patch_is_one_tile():
    assert wahoo.tiles_for_bounds((8.0, 46.55, 8.1, 46.65)) == [
        wahoo.MapTile(x=133, y=90)
    ]


def test_tiles_for_bounds_does_not_add_an_empty_tile_at_an_exact_edge():
    """Bounds ending exactly on a tile edge must not pull in the next tile.

    An empty tile is not harmless: it replaces the device's own map for that
    square with nothing.
    """
    west = wahoo.MapTile(x=133, y=90).west
    east = wahoo.MapTile(x=133, y=90).east
    tiles = wahoo.tiles_for_bounds((west, 46.2, east, 46.5))
    assert [(t.x, t.y) for t in tiles] == [(133, 90)]


# --- the OSM osmosis will accept ---------------------------------------------

def test_osmosis_xml_adds_the_version_attribute(tmp_path):
    src = tmp_path / "map.osm"
    src.write_text(
        osm.OSM_HEADER
        + osm.bounds_element((8.0, 46.5, 8.1, 46.6))
        + osm.node_xml(1, 46.55, 8.05)
        + osm.node_xml(2, 46.56, 8.06, {"natural": "spring"})
        + osm.way_xml(3, [1, 2], {"contour": "elevation", "ele": "2000"})
        + osm.OSM_FOOTER,
        encoding="utf-8",
    )
    out, occupied = wahoo.osmosis_xml(src, tmp_path / "osmosis.osm")
    text = out.read_text(encoding="utf-8")

    # the same pass reports which zoom-8 tiles actually hold nodes
    assert occupied == {(133, 90)}

    # osmosis aborts on OSM 0.6 entities without a version attribute.
    root = ET.fromstring(text)
    entities = root.findall("node") + root.findall("way")
    assert entities and all(e.get("version") == "1" for e in entities)
    # tags and geometry survive untouched
    assert "<tag k='natural' v='spring'/>" in text
    assert text.count("<nd ref='") == 2
    # the bounds element is not an entity and stays as it was
    assert root.find("bounds").get("minlat") == "46.5000000"


def test_osmosis_xml_reports_no_tiles_for_a_nodeless_osm(tmp_path):
    src = tmp_path / "empty.osm"
    src.write_text(
        osm.OSM_HEADER + osm.bounds_element((8.0, 46.5, 8.1, 46.6)) + osm.OSM_FOOTER,
        encoding="utf-8",
    )
    _, occupied = wahoo.osmosis_xml(src, tmp_path / "o.osm")
    assert occupied == set()


def test_osmosis_xml_leaves_negative_ids_alone(tmp_path):
    src = tmp_path / "map.osm"
    src.write_text(
        osm.OSM_HEADER + osm.node_xml(-7, 46.5, 8.0) + osm.OSM_FOOTER, encoding="utf-8"
    )
    out, _ = wahoo.osmosis_xml(src, tmp_path / "o.osm")
    node = ET.fromstring(out.read_text(encoding="utf-8")).find("node")
    assert node.get("id") == "-7" and node.get("version") == "1"


# --- the osmosis / map-writer invocation -------------------------------------

def _cmd(**kw):
    return wahoo.map_writer_cmd(
        "java", Path("/c/osmosis"), Path("/c/mw.jar"), Path("map.osm"),
        wahoo.MapTile(x=133, y=90), Path("out/133-90.map"), **kw,
    )


def test_map_writer_cmd_runs_osmosis_with_the_plugin_on_the_classpath():
    cmd = _cmd()
    assert cmd[0] == "java"
    cp = cmd[cmd.index("-cp") + 1]
    # the plugin registers itself off the classpath (osmosis-plugins.conf)
    assert "mw.jar" in cp and "osmosis" in cp and "lib" in cp
    assert cmd[cmd.index("-cp") + 2] == wahoo.OSMOSIS_MAIN
    assert "--read-xml" in cmd and "file=map.osm" in cmd
    # we emit no timestamps, so date parsing must be off
    assert "enableDateParsing=false" in cmd
    assert "--mapfile-writer" in cmd
    assert "file=out/133-90.map" in cmd
    assert f"bbox={wahoo.MapTile(x=133, y=90).bbox_arg()}" in cmd
    assert f"zoom-interval-conf={wahoo.ZOOM_INTERVAL_CONF}" in cmd
    assert f"tag-conf-file={wahoo.TAG_CONF_FILE}" in cmd
    # ram mode unless asked otherwise
    assert "type=hd" not in cmd


def test_map_writer_cmd_hd_mode_and_heap():
    cmd = _cmd(hd=True, max_heap="8g", threads=4)
    assert "type=hd" in cmd
    assert "threads=4" in cmd
    # -Xmx must precede the class path / main class
    assert cmd[1] == "-Xmx8g"
    assert cmd.index("-Xmx8g") < cmd.index("-cp")


def test_map_writer_cmd_without_tag_conf_falls_back_to_the_internal_default():
    assert not any(a.startswith("tag-conf-file=") for a in _cmd(tag_conf=None))


# --- packaging ---------------------------------------------------------------

def test_compress_map_writes_the_legacy_lzma_container(tmp_path):
    """Wahoo reads `.lzma` (the "alone" container), not `.xz`."""
    src = tmp_path / "t.map"
    payload = b"mapsforge binary OSM" + bytes(4096)
    src.write_bytes(payload)
    dst = wahoo.compress_map(src, tmp_path / "out" / "90.map.lzma")

    raw = dst.read_bytes()
    assert lzma.decompress(raw, format=lzma.FORMAT_ALONE) == payload
    # an .xz file would start with the xz magic; the alone container does not
    assert not raw.startswith(b"\xfd7zXZ")


@pytest.mark.skipif(
    subprocess.run(["which", "lzma"], capture_output=True).returncode != 0,
    reason="no lzma CLI to cross-check the container against",
)
def test_compress_map_matches_the_lzma_cli_container(tmp_path):
    src = tmp_path / "t.map"
    src.write_bytes(b"contour" * 500)
    ours = wahoo.compress_map(src, tmp_path / "ours.lzma").read_bytes()
    subprocess.run(["lzma", "--keep", "--force", str(src)], check=True)
    theirs = (tmp_path / "t.map.lzma").read_bytes()
    # same container: properties byte + dictionary size + size field
    assert ours[:13] == theirs[:13]


# --- the bundled style data --------------------------------------------------

def test_tag_mapping_and_theme_ship_and_parse():
    for path in (wahoo.TAG_CONF_FILE, wahoo.THEME_FILE):
        assert path.is_file(), path
        ET.parse(path)  # raises if malformed


def test_map_writer_cmd_puts_the_jvm_temp_dir_where_we_ask(tmp_path):
    """hd mode spills to java.io.tmpdir, and the JVM hardcodes that to /tmp."""
    cmd = _cmd(hd=True, max_heap="8g", tmpdir=tmp_path)
    assert f"-Djava.io.tmpdir={tmp_path}" in cmd
    # JVM flags must precede the class path / main class
    assert cmd.index(f"-Djava.io.tmpdir={tmp_path}") < cmd.index("-cp")
    # unset by default: a small build has no reason to move it
    assert not any(a.startswith("-Djava.io.tmpdir=") for a in _cmd())
