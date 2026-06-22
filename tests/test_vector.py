"""Tests for the swissTLM3D -> OSM conversion (pure functions, no subprocess)."""

from pathlib import Path

from topovert import vector
from topovert.vector import _IdAllocator


def test_ogr_geojson_cmd_reprojects_to_wgs84_geojsonseq():
    cmd = vector.ogr_geojson_cmd(
        Path("tlm.gdb"), "TLM_STRASSE", Path("out.geojsonl"), source_epsg=2056
    )
    assert cmd[0] == "ogr2ogr"
    assert cmd[cmd.index("-f") + 1] == "GeoJSONSeq"
    assert cmd[cmd.index("-s_srs") + 1] == "EPSG:2056"
    assert cmd[cmd.index("-t_srs") + 1] == "EPSG:4326"
    # dst, src, layer come last in that order
    assert cmd[-3:] == ["out.geojsonl", "tlm.gdb", "TLM_STRASSE"]


def test_ogr_geojson_cmd_autodetects_source_crs_when_epsg_none():
    """source_epsg=None drops -s_srs so ogr2ogr reads the layer's own CRS."""
    cmd = vector.ogr_geojson_cmd(
        Path("tlm.gdb"), "TLM_STRASSE", Path("out.geojsonl"), source_epsg=None
    )
    assert "-s_srs" not in cmd
    assert cmd[cmd.index("-t_srs") + 1] == "EPSG:4326"
    assert cmd[-3:] == ["out.geojsonl", "tlm.gdb", "TLM_STRASSE"]


def test_tags_for_road_classes_use_integer_objektart():
    assert vector.tags_for("TLM_STRASSE", {"OBJEKTART": 2}) == {"highway": "motorway"}
    assert vector.tags_for("TLM_STRASSE", {"OBJEKTART": 21}) == {"highway": "trunk"}
    assert vector.tags_for("TLM_STRASSE", {"OBJEKTART": 16}) == {"highway": "path"}
    # ferry is special-cased off OBJEKTART 14
    assert vector.tags_for("TLM_STRASSE", {"OBJEKTART": 14}) == {"route": "ferry"}
    # virtual connector (4) and car-train (13) are dropped
    assert vector.tags_for("TLM_STRASSE", {"OBJEKTART": 4}) is None
    assert vector.tags_for("TLM_STRASSE", {"OBJEKTART": 13}) is None
    # unknown code -> fallback, not dropped
    assert vector.tags_for("TLM_STRASSE", {"OBJEKTART": 999}) == {
        "highway": vector.ROAD_HIGHWAY_DEFAULT
    }


def test_tags_for_road_name_prefers_strassenname():
    tags = vector.tags_for("TLM_STRASSE", {"OBJEKTART": 9, "STRASSENNAME": "Dorfstrasse"})
    assert tags == {"highway": "tertiary", "name": "Dorfstrasse"}


def test_tags_for_waterway_lines():
    # stream, intermittent dry channel, dropped virtual lake axis
    assert vector.tags_for("TLM_FLIESSGEWAESSER", {"OBJEKTART": 4}) == {"waterway": "stream"}
    assert vector.tags_for("TLM_FLIESSGEWAESSER", {"OBJEKTART": 7}) == {
        "waterway": "stream", "intermittent": "yes",
    }
    assert vector.tags_for("TLM_FLIESSGEWAESSER", {"OBJEKTART": 6}) is None


def test_tags_for_landcover_from_bodenbedeckung():
    # lake (10) / river surface (5) -> water; forest (12) -> wood; glacier (9)
    assert vector.tags_for("TLM_BODENBEDECKUNG", {"OBJEKTART": 10, "NAME": "Lac"}) == {
        "natural": "water", "name": "Lac",
    }
    assert vector.tags_for("TLM_BODENBEDECKUNG", {"OBJEKTART": 5}) == {"natural": "water"}
    assert vector.tags_for("TLM_BODENBEDECKUNG", {"OBJEKTART": 12}) == {"natural": "wood"}
    assert vector.tags_for("TLM_BODENBEDECKUNG", {"OBJEKTART": 9}) == {"natural": "glacier"}
    assert vector.tags_for("TLM_BODENBEDECKUNG", {"OBJEKTART": 99}) is None  # unmapped land cover


def test_tags_for_railway_and_aerialway():
    assert vector.tags_for("TLM_EISENBAHN", {"OBJEKTART": 0}) == {"railway": "rail"}
    assert vector.tags_for("TLM_EISENBAHN", {"OBJEKTART": 2}) == {"railway": "narrow_gauge"}
    assert vector.tags_for("TLM_EISENBAHN", {"OBJEKTART": 999}) == {"railway": "rail"}  # default
    assert vector.tags_for("TLM_UEBRIGE_BAHN", {"OBJEKTART": 0}) == {"aerialway": "cable_car"}
    assert vector.tags_for("TLM_UEBRIGE_BAHN", {"OBJEKTART": 5}) == {"aerialway": "drag_lift"}
    assert vector.tags_for("TLM_UEBRIGE_BAHN", {"OBJEKTART": 7}) is None  # Lift dropped


def test_tags_for_poi_and_barrier():
    assert vector.tags_for("TLM_EINZELOBJEKT", {"OBJEKTART": 7}) == {"natural": "spring"}
    assert vector.tags_for("TLM_EINZELOBJEKT", {"OBJEKTART": 9, "NAME": "Fall"}) == {
        "waterway": "waterfall", "name": "Fall",
    }
    assert vector.tags_for("TLM_EINZELOBJEKT", {"OBJEKTART": 99}) is None  # unmapped POI
    assert vector.tags_for("TLM_MAUER", {"OBJEKTART": 0}) == {"barrier": "wall"}


def test_ogr_geojson_cmd_applies_where_filter():
    cmd = vector.ogr_geojson_cmd(
        Path("t.gdb"), "TLM_BODENBEDECKUNG", Path("o.geojsonl"),
        source_epsg=2056, where="OBJEKTART IN (5, 10)",
    )
    assert cmd[cmd.index("-where") + 1] == "OBJEKTART IN (5, 10)"
    assert "TLM_BODENBEDECKUNG" in vector.LAYER_WHERE  # default water filter wired


def test_tags_for_building_is_constant():
    assert vector.tags_for("TLM_GEBAEUDE_FOOTPRINT", {"OBJEKTART": 0}) == {"building": "yes"}


def test_tags_for_unmapped_layer_is_dropped():
    assert vector.tags_for("TLM_NUTZUNGSAREAL", {"OBJEKTART": 0}) is None
    assert vector.tags_for("TLM_STROMTRASSE", {"OBJEKTART": 1}) is None


def test_feature_to_osm_linestring_emits_nodes_and_way():
    feature = {
        "properties": {"OBJEKTART": 15, "STRASSENNAME": "Weg"},
        "geometry": {"type": "LineString", "coordinates": [[8.0, 47.0], [8.1, 47.1]]},
    }
    xml = "".join(vector.feature_to_osm("TLM_STRASSE", feature, _IdAllocator()))
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
    out = vector.feature_to_osm("TLM_GEBAEUDE_FOOTPRINT", feature, _IdAllocator())
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
    out = "".join(vector.geojson_to_osm(["", line, "\x1e"], "TLM_GEBAEUDE_FOOTPRINT", ids))
    assert out.count("<node ") == 1
    assert "k='building' v='yes'" in out
