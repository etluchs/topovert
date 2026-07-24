"""Tests for the style-legend renderer (pure parsing + HTML, no network)."""

from topovert import legend, mkgmap


# --------------------------------------------------------------------------- #
# TYP parsing
# --------------------------------------------------------------------------- #
def test_parse_typ_solid_polygon_and_line_and_draworder():
    text = """
[_drawOrder]
Type=0x3c,1
Type=0x50,2
[end]

[_polygon]
Type=0x3c
String1=0x04,Wasser
Xpm="0 0 1 0"
"1 c #5AA0D8"
[end]

[_line]
Type=0x16
String1=0x04,Wanderweg
Xpm="0 0 1 0"
"1 c #E03028"
LineWidth=2
BorderWidth=0
[end]
"""
    els = {(e.kind, e.type_code): e for e in legend.parse_typ(text)}
    water = els[("polygon", 0x3C)]
    assert water.label == "Wasser"
    assert water.xpm.primary == "#5aa0d8"
    assert water.draw_order == 1
    assert water.xpm.pixels == []  # solid: no bitmap
    path = els[("line", 0x16)]
    assert path.label == "Wanderweg"
    assert path.line_width == 2
    assert path.xpm.primary == "#e03028"


def test_parse_typ_bitmap_pattern_reads_pixels():
    """A non-solid Xpm (dash/hatch) parses its colour grid, not just a fill."""
    text = """
[_line]
Type=0x1a
String1=0x04,Dashed
Xpm="4 1 2 1"
"# c #000000"
". c none"
"##.."
LineWidth=1
[end]
"""
    (el,) = legend.parse_typ(text)
    assert el.xpm.width == 4 and el.xpm.height == 1
    assert el.xpm.colours == ["#000000", None]
    assert el.xpm.pixels == [[0, 0, 1, 1]]  # '##..' -> black,black,none,none
    assert el.xpm.primary == "#000000"


def test_parse_real_bundled_typ():
    """The shipped TYP parses into the expected elements + colours."""
    els = {
        (e.kind, e.type_code): e
        for e in legend.parse_typ(mkgmap.TYP_FILE.read_text(encoding="utf-8"))
    }
    assert els[("polygon", 0x50)].label == "Wald"
    assert els[("polygon", 0x3C)].draw_order == 1  # water paints early
    assert els[("polygon", 0x13)].draw_order == 3  # buildings paint last
    assert els[("line", 0x22)].label == "Hoehenlinie Index"


# --------------------------------------------------------------------------- #
# Style-rule parsing + join
# --------------------------------------------------------------------------- #
def test_parse_style_rules_extracts_type_and_resolution():
    rules = legend.parse_style_rules(
        {
            "lines": "highway=path [0x16 road_class=0 resolution 24]\n"
            "# comment\ncontour=elevation { name '${ele}' }\n",
            "polygons": "natural=water [0x3c resolution 18]\n",
            "points": "man_made=cross [0x6616 resolution 20]\n",
        }
    )
    by_tag = {(r.group, r.tag): r for r in rules}
    assert by_tag[("line", "highway=path")].type_code == 0x16
    assert by_tag[("line", "highway=path")].resolution == 24
    assert by_tag[("polygon", "natural=water")].type_code == 0x3C
    # the name-only contour action assigns no type -> not a rule
    assert all(r.tag != "contour=elevation" for r in rules)


def test_build_rows_joins_typ_and_rules():
    els = legend.parse_typ(
        '[_polygon]\nType=0x3c\nString1=0x04,Wasser\nXpm="0 0 1 0"\n'
        '"1 c #5AA0D8"\n[end]\n'
    )
    rules = legend.parse_style_rules(
        {
            "polygons": "natural=water [0x3c resolution 18]\n",
            "lines": "highway=motorway [0x01 resolution 16]\n",  # rule-only, no TYP
        }
    )
    rows = {(r.group, r.type_code): r for r in legend.build_rows(els, rules)}
    water = rows[("polygon", 0x3C)]
    assert water.element is not None and water.label == "Wasser"
    assert water.tags == [("natural=water", 18)]
    motorway = rows[("line", 0x01)]
    assert motorway.element is None  # Garmin default look
    assert motorway.label == "highway=motorway"


def test_build_legend_covers_every_bundled_element():
    """The real style + TYP produce a row for each styled type, both directions."""
    rows = legend.build_legend()
    types = {(r.group, r.type_code) for r in rows}
    # A TYP-styled polygon, a rule-only road class, and both contour line types.
    assert ("polygon", 0x50) in types  # Wald (styled)
    assert ("line", 0x01) in types  # motorway (rule-only)
    assert ("line", 0x21) in types and ("line", 0x22) in types  # contours
    assert ("point", 0x6616) in types  # summit cross POI
    # Every row has a non-empty label.
    assert all(r.label for r in rows)


# --------------------------------------------------------------------------- #
# HTML rendering
# --------------------------------------------------------------------------- #
def test_render_html_is_well_formed_with_one_swatch_per_row():
    rows = legend.build_legend()
    doc = legend.render_html(rows, live=True)
    assert doc.startswith("<!doctype html>")
    assert doc.rstrip().endswith("</html>")
    assert doc.count("<svg") == len(rows)  # a swatch per element
    assert "/api/version" in doc  # live => hot-reload script present
    # A known colour from the TYP makes it into the swatch.
    assert "#e03028" in doc.lower()


def test_render_html_static_omits_live_script():
    rows = legend.build_legend()
    assert "/api/version" not in legend.render_html(rows, live=False)


def test_render_legend_writes_file(tmp_path):
    out = legend.render_legend(tmp_path / "legend.html")
    assert out.is_file()
    text = out.read_text(encoding="utf-8")
    assert "topovert style legend" in text
    assert "/api/version" not in text  # static file, no server


# --------------------------------------------------------------------------- #
# Server helpers (no socket)
# --------------------------------------------------------------------------- #
def test_version_token_changes_when_a_watched_file_changes(tmp_path):
    style_dir = tmp_path / "topovert"
    style_dir.mkdir()
    (style_dir / "lines").write_text("highway=path [0x16 resolution 24]\n")
    (style_dir / "points").write_text("")
    (style_dir / "polygons").write_text("")
    typ = tmp_path / "typ.txt"
    typ.write_text("[_line]\nType=0x16\nString1=0x04,X\n[end]\n")

    v1 = legend._version_token(typ, style_dir)
    (style_dir / "lines").write_text("highway=path [0x16 resolution 22]\n")
    import os

    os.utime(style_dir / "lines", (2_000_000_000, 2_000_000_000))  # future mtime
    v2 = legend._version_token(typ, style_dir)
    assert v1 != v2
