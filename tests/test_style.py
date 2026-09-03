"""The bundled rendering data must stay consistent with what we emit.

These guard the rendering contract without needing mkgmap/osmosis/Java: the style
files ship, the TYP identity matches the Garmin family/product id, and every
concrete OSM tag the swissTLM3D converter (``vector.py``) produces is matched by
a rule — on *both* sides, the mkgmap style/TYP (Garmin) and the map-writer
tag-mapping plus device render theme (Wahoo). A tag with no rule is data that
silently never renders.
"""

import re
import xml.etree.ElementTree as ET

from topovert import contour, mkgmap, vector, wahoo


def _style_rule_keys(filename: str) -> set[tuple[str, str]]:
    """(key, value) pairs a style rule file matches; value '*' is a wildcard."""
    pairs: set[tuple[str, str]] = set()
    text = (mkgmap.STYLE_DIR / filename).read_text(encoding="utf-8")
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        lhs = line.split("[", 1)[0].strip()  # drop the [type ...] definition
        if "=" not in lhs:
            continue
        key, value = (s.strip() for s in lhs.split("=", 1))
        pairs.add((key, value))
    return pairs


def _emitted_tag_dicts() -> list[dict]:
    """Every tag dict the converters emit (vector.tags_for + contour, ferry too)."""
    dicts: list[dict] = [{"route": "ferry"}]
    for table, default, _names in vector._KIND_TABLES.values():
        for tags in list(table.values()) + [default]:
            if tags:
                dicts.append(tags)
    # Contour ways (contour.contour_tags) — a major (index) and a minor line.
    dicts.append(contour.contour_tags(100, interval=20, major_every=5))
    dicts.append(contour.contour_tags(120, interval=20, major_every=5))
    return dicts


def test_style_and_typ_ship():
    for name in ("version", "options", "lines", "points", "polygons"):
        assert (mkgmap.STYLE_DIR / name).is_file(), name
    assert mkgmap.TYP_FILE.is_file()


def test_typ_identity_matches_map():
    text = mkgmap.TYP_FILE.read_text(encoding="utf-8")
    fid = int(re.search(r"(?m)^FID=(\d+)", text).group(1))
    pid = int(re.search(r"(?m)^ProductCode=(\d+)", text).group(1))
    # A TYP whose FID/ProductCode don't match the map is silently ignored.
    assert fid == mkgmap.FAMILY_ID
    assert pid == mkgmap.PRODUCT_ID


def test_every_emitted_tag_is_covered_by_a_style_rule():
    rules = (
        _style_rule_keys("lines")
        | _style_rule_keys("points")
        | _style_rule_keys("polygons")
    )
    for tags in _emitted_tag_dicts():
        # A feature is rendered if any of its tags matches a rule (exact or
        # key=* wildcard). 'name'/attribute tags never type a feature alone.
        covered = any(
            (key, value) in rules or (key, "*") in rules
            for key, value in tags.items()
        )
        assert covered, f"no style rule for {tags}"


def test_typ_colours_only_types_the_style_assigns():
    """Every Type customised in the TYP must be produced by a style rule."""
    style_types = set()
    for name in ("lines", "polygons"):
        for line in (mkgmap.STYLE_DIR / name).read_text().splitlines():
            for m in re.finditer(r"\[(0x[0-9a-fA-F]+)", line):
                style_types.add(int(m.group(1), 16))
    typ_text = mkgmap.TYP_FILE.read_text(encoding="utf-8")
    for m in re.finditer(r"(?m)^Type=(0x[0-9a-fA-F]+)", typ_text):
        assert int(m.group(1), 16) in style_types, m.group(1)


# --- Wahoo: what map-writer stores, and what the device theme draws ---------

_TAG_MAPPING_NS = "{http://mapsforge.org/tag-mapping}"
_THEME_NS = "{http://mapsforge.org/renderTheme}"


def _tag_mapping_entries() -> list[dict]:
    """Every ``osm-tag`` in the map-writer tag-mapping, as plain dicts."""
    root = ET.parse(wahoo.TAG_CONF_FILE).getroot()
    return [dict(el.attrib) for el in root.iter(f"{_TAG_MAPPING_NS}osm-tag")]


def _theme_rule_keys() -> set[tuple[str, str]]:
    """(key, value) pairs the render theme matches; ``v`` may list alternatives."""
    root = ET.parse(wahoo.THEME_FILE).getroot()
    pairs = set()
    for rule in root.iter(f"{_THEME_NS}rule"):
        for value in rule.get("v", "").split("|"):
            pairs.add((rule.get("k"), value))
    return pairs


def test_every_emitted_tag_is_stored_by_the_wahoo_tag_mapping():
    """A tag missing from the tag-mapping never reaches the device at all."""
    stored = {(e["key"], e["value"]) for e in _tag_mapping_entries()}
    for tags in _emitted_tag_dicts():
        covered = any((key, value) in stored for key, value in tags.items())
        assert covered, f"no Wahoo tag-mapping entry for {tags}"


def test_every_stored_wahoo_tag_is_drawn_by_the_theme():
    """And one the theme ignores is stored but invisible."""
    rules = _theme_rule_keys()
    for entry in _tag_mapping_entries():
        if entry.get("renderable") == "false":
            continue  # attribute-only tags (e.g. intermittent) type nothing
        key, value = entry["key"], entry["value"]
        assert (key, value) in rules or (key, "*") in rules or ("*", "*") in rules, (
            f"tag-mapping stores {key}={value} but the theme never draws it"
        )


def test_contour_labels_travel_in_ref_on_both_sides():
    """The Wahoo label slot for a contour is `ref`, and it must stay metres.

    mapsforge stores an elevation on POIs only, so a way's `ele` cannot label it;
    `name` is unusable because mkgmap's `name` action only fires when no label is
    set, which would leave the Garmin map showing metres as feet.
    """
    tags = contour.contour_tags(2000, interval=20, major_every=5)
    assert tags["ref"] == "2000" and "name" not in tags
    theme = ET.parse(wahoo.THEME_FILE).getroot()
    assert any(
        el.get("k") == "ref" for el in theme.iter(f"{_THEME_NS}pathText")
    ), "the theme never labels a line from ref"
    # the metres->feet conversion stays a Garmin-style concern: the device draws
    # the stored string as-is, so no rule may convert (comments aside).
    assert "conv:m=>ft" in (mkgmap.STYLE_DIR / "lines").read_text(encoding="utf-8")
    assert not any(
        "conv:" in value for el in theme.iter() for value in el.attrib.values()
    )
