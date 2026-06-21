"""Tests for the swissTLM3D -> OSM conversion (pure functions, no subprocess)."""

from pathlib import Path

from topovert import vector
from topovert.vector import _IdAllocator


def test_ogr_geojson_cmd_reprojects_to_wgs84_geojsonseq():
    cmd = vector.ogr_geojson_cmd(
        Path("tlm.gpkg"), "tlm_strasse", Path("out.geojsonl"), source_epsg=2056
    )
    assert cmd[0] == "ogr2ogr"
    assert cmd[cmd.index("-f") + 1] == "GeoJSONSeq"
    assert cmd[cmd.index("-s_srs") + 1] == "EPSG:2056"
    assert cmd[cmd.index("-t_srs") + 1] == "EPSG:4326"
    # dst, src, layer come last in that order
    assert cmd[-3:] == ["out.geojsonl", "tlm.gpkg", "tlm_strasse"]


def test_tags_for_road_classes():
    assert vector.tags_for("tlm_strasse", {"objektart": "Autobahn"}) == {"highway": "motorway"}
    assert vector.tags_for("tlm_strasse", {"OBJEKTART": "Wanderweg"}) == {"highway": "path"}
    # unknown road class still maps (fallback), not dropped
    assert vector.tags_for("tlm_strasse", {"objektart": "Mystery"}) == {
        "highway": vector.ROAD_HIGHWAY_DEFAULT
    }


def test_tags_for_water_building_and_name_passthrough():
    assert vector.tags_for("tlm_fliessgewaesser", {}) == {"waterway": "stream"}
    assert vector.tags_for("tlm_stehende_gewaesser", {"name": "Lac"}) == {
        "natural": "water", "name": "Lac",
    }
    assert vector.tags_for("tlm_gebaeude_footprint", {}) == {"building": "yes"}


def test_tags_for_unmapped_layer_is_dropped():
    assert vector.tags_for("tlm_bodenbedeckung", {"objektart": "Wald"}) is None


def test_feature_to_osm_linestring_emits_nodes_and_way():
    feature = {
        "properties": {"objektart": "Fahrweg", "name": "Weg"},
        "geometry": {"type": "LineString", "coordinates": [[8.0, 47.0], [8.1, 47.1]]},
    }
    xml = "".join(vector.feature_to_osm("tlm_strasse", feature, _IdAllocator()))
    assert xml.count("<node ") == 2
    assert xml.count("<way ") == 1
    assert "<nd ref=" in xml
    assert "k='highway' v='track'" in xml
    assert "k='name' v='Weg'" in xml


def test_feature_to_osm_polygon_is_closed_way_outer_ring_only():
    # GeoJSON ring repeats the first point; the way should close without a
    # duplicate node (4 distinct corners -> 4 nodes, refs first id again at end).
    ring = [[8.0, 47.0], [8.1, 47.0], [8.1, 47.1], [8.0, 47.1], [8.0, 47.0]]
    feature = {"properties": {}, "geometry": {"type": "Polygon", "coordinates": [ring]}}
    out = vector.feature_to_osm("tlm_gebaeude_footprint", feature, _IdAllocator())
    xml = "".join(out)
    assert xml.count("<node ") == 4
    way = [frag for frag in out if frag.startswith("  <way")][0]
    assert way.count("<nd ref=") == 5          # closed: 4 corners + repeat of first
    first_ref = way.split("ref='")[1].split("'")[0]
    last_ref = way.rsplit("ref='", 1)[1].split("'")[0]
    assert first_ref == last_ref
    assert "k='building' v='yes'" in way


def test_geojson_to_osm_skips_blank_and_rs_prefixed_lines():
    line = (
        '\x1e{"properties": {}, "geometry": {"type": "Point", "coordinates": [8.0, 47.0]}}'
    )
    ids = _IdAllocator()
    out = "".join(vector.geojson_to_osm(["", line, "\x1e"], "tlm_gebaeude_footprint", ids))
    assert out.count("<node ") == 1
    assert "k='building' v='yes'" in out
