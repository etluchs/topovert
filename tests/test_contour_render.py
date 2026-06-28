"""Tests for the contour SVG render utility (pure functions, no subprocess)."""

from pathlib import Path

from topovert import contour, contour_render


def _line_feature(ele, coords):
    return {
        "properties": {"ele": ele},
        "geometry": {"type": "LineString", "coordinates": coords},
    }


def test_features_to_svg_is_well_formed_and_sizes_to_data():
    feats = [
        _line_feature(100, [[8.0, 47.0], [8.1, 47.0], [8.1, 47.1]]),  # major
        _line_feature(120, [[8.0, 47.05], [8.1, 47.05]]),             # minor
    ]
    svg = contour_render.features_to_svg(feats, interval=20, major_every=5, width=400)
    assert svg.startswith("<svg") and svg.rstrip().endswith("</svg>")
    assert 'width="400"' in svg
    # both lines render as polylines
    assert svg.count("<polyline") == 2


def test_features_to_svg_labels_majors_in_metres_only():
    feats = [
        _line_feature(100, [[8.0, 47.0], [8.2, 47.2]]),   # 100 m -> major (index)
        _line_feature(120, [[8.0, 47.1], [8.2, 47.1]]),   # 120 m -> minor
    ]
    svg = contour_render.features_to_svg(feats, interval=20, major_every=5, width=400)
    # The index line is labelled with its metre value; the minor one is not.
    assert ">100<" in svg
    assert ">120<" not in svg


def test_features_to_svg_handles_multilinestring_and_rounding():
    feats = [
        {
            "properties": {"ele": 99.6},  # rounds to 100 -> major
            "geometry": {
                "type": "MultiLineString",
                "coordinates": [
                    [[8.0, 47.0], [8.1, 47.0]],
                    [[8.2, 47.2], [8.3, 47.2]],
                ],
            },
        }
    ]
    svg = contour_render.features_to_svg(feats, interval=20, major_every=5, width=400)
    assert svg.count("<polyline") == 2
    assert ">100<" in svg


def test_features_to_svg_respects_explicit_bbox_viewport():
    feats = [_line_feature(100, [[8.05, 47.05], [8.06, 47.06]])]
    bbox = (8.0, 47.0, 8.1, 47.1)
    svg = contour_render.features_to_svg(feats, interval=20, width=500, bbox=bbox)
    # square-ish patch at this latitude -> height within the data extent, viewport
    # driven by bbox not the (tiny) line, so the line sits mid-image.
    assert 'width="500"' in svg
    assert "<polyline" in svg


def test_features_to_svg_empty_raises():
    import pytest

    with pytest.raises(Exception):
        contour_render.features_to_svg([], interval=20)


def test_clip_cmd_uses_wgs84_te_srs_and_preserves_crs():
    cmd = contour_render._clip_cmd(
        Path("mosaic.vrt"), Path("patch.tif"), (8.0, 47.0, 8.1, 47.1)
    )
    assert cmd[0] == "gdalwarp"
    assert cmd[cmd.index("-te_srs") + 1] == "EPSG:4326"
    te = cmd.index("-te")
    assert cmd[te + 1 : te + 5] == ["8.0000000000", "47.0000000000",
                                    "8.1000000000", "47.1000000000"]
    # no -t_srs: output keeps the source CRS for the existing contour path
    assert "-t_srs" not in cmd
    assert cmd[-2:] == ["mosaic.vrt", "patch.tif"]


def test_clip_cmd_adds_source_epsg_when_given():
    cmd = contour_render._clip_cmd(
        Path("m.vrt"), Path("p.tif"), (8.0, 47.0, 8.1, 47.1), source_epsg=2056
    )
    assert cmd[cmd.index("-s_srs") + 1] == "EPSG:2056"


def test_load_geojsonl_strips_record_separator(tmp_path):
    f = tmp_path / "c.geojsonl"
    f.write_text(
        '\x1e{"properties":{"ele":100},"geometry":{"type":"LineString",'
        '"coordinates":[[8.0,47.0],[8.1,47.1]]}}\n\n',
        encoding="utf-8",
    )
    feats = contour_render._load_geojsonl(f)
    assert len(feats) == 1
    assert feats[0]["properties"]["ele"] == 100
