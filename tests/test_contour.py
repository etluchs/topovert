"""Tests for the DEM -> contour -> OSM conversion (pure functions, no subprocess)."""

from pathlib import Path

from topovert import contour
from topovert.vector import _IdAllocator


def test_gdal_contour_cmd_sets_interval_attr_and_gpkg():
    cmd = contour.gdal_contour_cmd(Path("mosaic.vrt"), Path("c.gpkg"), interval=20)
    assert cmd[0] == "gdal_contour"
    assert cmd[cmd.index("-i") + 1] == "20"
    assert cmd[cmd.index("-a") + 1] == "ele"
    assert cmd[cmd.index("-f") + 1] == "GPKG"
    # src then dst come last
    assert cmd[-2:] == ["mosaic.vrt", "c.gpkg"]


def test_contour_tags_classifies_major_and_minor():
    # every 5th * 20 m == each 100 m is a bold index (major) line
    assert contour.contour_tags(100, interval=20, major_every=5) == {
        "contour": "elevation", "ele": "100", "contour_ext": "elevation_major",
    }
    assert contour.contour_tags(120, interval=20, major_every=5) == {
        "contour": "elevation", "ele": "120", "contour_ext": "elevation_minor",
    }
    # elevation is rounded to an integer ele
    assert contour.contour_tags(119.6, interval=20, major_every=5)["ele"] == "120"


def test_feature_to_osm_linestring_emits_nodes_and_way_with_tags():
    feature = {
        "properties": {"ele": 100.0},
        "geometry": {"type": "LineString", "coordinates": [[8.0, 47.0], [8.1, 47.1]]},
    }
    xml = "".join(
        contour._feature_to_osm(feature, _IdAllocator(), interval=20, major_every=5)
    )
    assert xml.count("<node ") == 2
    assert xml.count("<way ") == 1
    assert "k='contour' v='elevation'" in xml
    assert "k='ele' v='100'" in xml
    assert "k='contour_ext' v='elevation_major'" in xml


def test_feature_to_osm_multilinestring_emits_each_segment():
    feature = {
        "properties": {"ele": 120.0},
        "geometry": {
            "type": "MultiLineString",
            "coordinates": [[[8.0, 47.0], [8.1, 47.0]], [[8.2, 47.2], [8.3, 47.2]]],
        },
    }
    out = contour._feature_to_osm(feature, _IdAllocator(), interval=20, major_every=5)
    xml = "".join(out)
    assert xml.count("<way ") == 2
    assert xml.count("<node ") == 4
    assert "k='contour_ext' v='elevation_minor'" in xml


def test_feature_to_osm_without_elevation_is_dropped():
    feature = {
        "properties": {},
        "geometry": {"type": "LineString", "coordinates": [[8.0, 47.0], [8.1, 47.1]]},
    }
    assert contour._feature_to_osm(feature, _IdAllocator(), interval=20, major_every=5) == []
