"""The bundled mkgmap style + TYP must stay consistent with what we emit.

These guard the rendering contract without needing mkgmap/Java: the style files
ship, the TYP identity matches the Garmin family/product id, and every concrete
OSM tag the swissTLM3D converter (``vector.py``) produces is matched by a style
rule (so no mapped feature renders untyped).
"""

import re

from topovert import mkgmap, vector


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
    """Every tag dict vector.tags_for can return (the ferry special case too)."""
    dicts: list[dict] = [{"route": "ferry"}]
    for table, default, _names in vector._KIND_TABLES.values():
        for tags in list(table.values()) + [default]:
            if tags:
                dicts.append(tags)
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
