"""Render the bundled Swiss style + TYP as a browser-viewable legend.

Fine-tuning the map look (``styles/topovert/{lines,points,polygons}`` +
``styles/topovert_typ.txt``) is otherwise a slow loop: build a whole ``.IMG``
and eyeball it in QMapShack for every colour tweak. This module reads those same
files and renders a **legend** — one swatch per rendered element, drawn with the
TYP's own colours/widths and annotated with the Garmin type code, the OSM tags
that route to it (from the style rules), and the zoom resolution.

Two entry points:

* :func:`render_legend` writes a standalone ``legend.html`` (like
  ``render-contours`` writes an SVG) — no server, open the file.
* :func:`serve` runs a tiny stdlib HTTP server that re-reads the style files on
  every request and hot-reloads the page when any of them changes on disk, so
  editing a colour in ``topovert_typ.txt`` updates the browser within a second.

The swatches are drawn from *our* TYP data, so — like ``render-contours`` — this
shows what we asked mkgmap to draw, not what a device's renderer finally paints;
it can't reveal device-side quirks (e.g. the feet/metres contour bug). It is a
fast check of colours, widths, draw order and tag coverage, not a device proof.

Parsing (:func:`parse_typ`, :func:`parse_style_rules`) and HTML generation
(:func:`build_rows`, :func:`render_html`) are pure stdlib and unit-tested; only
:func:`serve` touches the network.
"""

from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from .mkgmap import STYLE_DIR, TYP_FILE

log = logging.getLogger(__name__)

# The three rule files whose tag->type assignments the legend joins against the
# TYP, plus the TYP itself. Editing any of them re-renders the served page.
_RULE_FILES = ("polygons", "lines", "points")
WATCHED = ("polygons", "lines", "points", TYP_FILE.name)

# mkgmap "resolution" is a zoom bits value (16..24): a smaller number means the
# feature survives to a further-out zoom. Shown as-is; documented in the page.


# --------------------------------------------------------------------------- #
# TYP parsing
# --------------------------------------------------------------------------- #
@dataclass
class Xpm:
    """A parsed TYP ``Xpm`` block: colours + an optional bitmap of colour indices.

    ``colours`` is the ordered day colour list (``#rrggbb`` or ``None`` for a
    transparent/``none`` entry). A solid ``"0 0 1 0"`` fill has one colour and no
    ``pixels``; a bitmap (dash pattern / area hatch) has ``width*height`` pixels
    as row-major indices into ``colours``.
    """

    width: int
    height: int
    colours: list[str | None]
    pixels: list[list[int]] = field(default_factory=list)

    @property
    def primary(self) -> str | None:
        """The first opaque colour — what a solid fill/line is drawn with."""
        for c in self.colours:
            if c:
                return c
        return None


@dataclass
class TypElement:
    """One ``[_polygon]`` / ``[_line]`` / ``[_point]`` section of the TYP."""

    kind: str  # 'polygon' | 'line' | 'point'
    type_code: int
    label: str | None
    xpm: Xpm | None
    line_width: int | None = None
    border_width: int | None = None
    draw_order: int | None = None


_COLOUR_RE = re.compile(r'^\s*(?P<key>\S+)\s+[a-zA-Z]\s+(?P<val>#[0-9A-Fa-f]{6}|none)\s*$')
_SECTION_RE = re.compile(r"^\[(_[A-Za-z]+)\]\s*$")


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        return value[1:-1]
    return value


def _parse_xpm(header: str, body: list[str]) -> Xpm:
    """Parse an Xpm from its ``"W H C P"`` header and following quoted lines."""
    parts = header.split()
    w, h, ncol = (int(parts[0]), int(parts[1]), int(parts[2]))
    colours: list[str | None] = []
    char_to_idx: dict[str, int] = {}
    cursor = 0
    for _ in range(ncol):
        if cursor >= len(body):
            break
        m = _COLOUR_RE.match(_unquote(body[cursor]))
        cursor += 1
        if not m:
            colours.append(None)
            continue
        char_to_idx[m.group("key")] = len(colours)
        val = m.group("val")
        colours.append(None if val.lower() == "none" else val.lower())
    pixels: list[list[int]] = []
    cpp = int(parts[3]) if len(parts) > 3 else 0
    if w > 0 and h > 0 and cpp > 0:
        for _ in range(h):
            if cursor >= len(body):
                break
            row_chars = _unquote(body[cursor])
            cursor += 1
            row = [
                char_to_idx.get(row_chars[i : i + cpp], 0)
                for i in range(0, w * cpp, cpp)
            ]
            pixels.append(row)
    return Xpm(width=w, height=h, colours=colours, pixels=pixels)


def parse_typ(text: str) -> list[TypElement]:
    """Parse the elements + draw order from a ``topovert_typ.txt`` source."""
    lines = text.splitlines()
    draw_order: dict[int, int] = {}
    elements: list[TypElement] = []

    i = 0
    n = len(lines)
    while i < n:
        m = _SECTION_RE.match(lines[i].strip())
        if not m:
            i += 1
            continue
        section = m.group(1)
        i += 1
        # Collect the section body up to [end].
        body: list[str] = []
        while i < n and lines[i].strip().lower() != "[end]":
            body.append(lines[i])
            i += 1
        i += 1  # skip [end]

        if section == "_drawOrder":
            for raw in body:
                dm = re.match(r"\s*Type=(0x[0-9A-Fa-f]+)\s*,\s*(\d+)", raw)
                if dm:
                    draw_order[int(dm.group(1), 16)] = int(dm.group(2))
            continue

        kind = {"_polygon": "polygon", "_line": "line", "_point": "point"}.get(section)
        if kind is None:
            continue
        el = _parse_section(kind, body)
        if el is not None:
            elements.append(el)

    for el in elements:
        el.draw_order = draw_order.get(el.type_code)
    return elements


def _parse_section(kind: str, body: list[str]) -> TypElement | None:
    type_code: int | None = None
    label: str | None = None
    line_width: int | None = None
    border_width: int | None = None
    xpm: Xpm | None = None

    j = 0
    while j < len(body):
        raw = body[j].strip()
        j += 1
        if not raw or raw.startswith(";"):
            continue
        if "=" not in raw:
            continue
        key, val = (s.strip() for s in raw.split("=", 1))
        low = key.lower()
        if low == "type":
            try:
                type_code = int(val, 16)
            except ValueError:
                type_code = None
        elif low.startswith("string") and "," in val:
            label = val.split(",", 1)[1].strip()
        elif low == "linewidth":
            line_width = _safe_int(val)
        elif low == "borderwidth":
            border_width = _safe_int(val)
        elif low == "xpm":
            header = _unquote(val)
            parts = header.split()
            if len(parts) >= 3:
                ncol, h = int(parts[2]), int(parts[1])
                cpp = int(parts[3]) if len(parts) > 3 else 0
                take = ncol + (h if (int(parts[0]) > 0 and h > 0 and cpp > 0) else 0)
                xpm = _parse_xpm(header, body[j : j + take])
                j += take
    if type_code is None:
        return None
    return TypElement(
        kind=kind,
        type_code=type_code,
        label=label,
        xpm=xpm,
        line_width=line_width,
        border_width=border_width,
    )


def _safe_int(val: str) -> int | None:
    try:
        return int(val)
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# Style-rule parsing (OSM tag -> Garmin type + resolution)
# --------------------------------------------------------------------------- #
@dataclass
class StyleRule:
    group: str  # 'polygon' | 'line' | 'point'
    tag: str  # e.g. 'highway=path', 'railway=*'
    type_code: int
    resolution: int | None


_RULE_RE = re.compile(
    r"^(?P<lhs>[^\[#]+?)\s*\[\s*(?P<type>0x[0-9A-Fa-f]+)(?P<rest>[^\]]*)\]"
)


def parse_style_rules(texts: dict[str, str]) -> list[StyleRule]:
    """Parse rule files ``{'polygons': ..., 'lines': ..., 'points': ...}``.

    Returns one :class:`StyleRule` per ``key=value [0xNN ... resolution NN]``
    line. Name-only actions (``contour=elevation { name ... }``) assign no type
    and are skipped — their concrete types (0x21/0x22) come from their own rules.
    """
    group_of = {"polygons": "polygon", "lines": "line", "points": "point"}
    rules: list[StyleRule] = []
    for filename, text in texts.items():
        group = group_of.get(filename, filename)
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            m = _RULE_RE.match(stripped)
            if not m:
                continue
            lhs = m.group("lhs").strip()
            if "=" not in lhs:
                continue
            res_m = re.search(r"resolution\s+(\d+)", m.group("rest"))
            rules.append(
                StyleRule(
                    group=group,
                    tag=lhs,
                    type_code=int(m.group("type"), 16),
                    resolution=int(res_m.group(1)) if res_m else None,
                )
            )
    return rules


# --------------------------------------------------------------------------- #
# Legend model
# --------------------------------------------------------------------------- #
@dataclass
class LegendRow:
    group: str  # 'polygon' | 'line' | 'point'
    type_code: int
    label: str
    element: TypElement | None  # TYP styling, or None (Garmin-default look)
    tags: list[tuple[str, int | None]]  # (osm tag, resolution)
    draw_order: int | None


_GROUP_TITLES = {
    "polygon": "Areas (land cover, buildings)",
    "line": "Lines (roads, water, rail, contours)",
    "point": "Points (POIs)",
}
_GROUP_ORDER = ("polygon", "line", "point")


def build_rows(elements: list[TypElement], rules: list[StyleRule]) -> list[LegendRow]:
    """Join TYP elements with style rules into per-type legend rows.

    A row exists for every Garmin type that either the TYP styles or a style rule
    assigns. TYP-only types (rare) still show their swatch; rule-only types (e.g.
    the road classes and POIs that ride on Garmin's built-in look) show up with no
    custom swatch so their coverage is still visible. Rows are ordered by group
    then TYP draw order (what actually paints on top) then type code.
    """
    els_by_type: dict[tuple[str, int], TypElement] = {
        (e.kind, e.type_code): e for e in elements
    }
    tags_by_type: dict[tuple[str, int], list[tuple[str, int | None]]] = {}
    for r in rules:
        tags_by_type.setdefault((r.group, r.type_code), []).append(
            (r.tag, r.resolution)
        )

    keys = set(els_by_type) | set(tags_by_type)
    rows: list[LegendRow] = []
    for group, type_code in keys:
        el = els_by_type.get((group, type_code))
        tags = sorted(set(tags_by_type.get((group, type_code), [])))
        label = (el.label if el and el.label else None) or _label_from_tags(
            tags, type_code
        )
        rows.append(
            LegendRow(
                group=group,
                type_code=type_code,
                label=label,
                element=el,
                tags=tags,
                draw_order=el.draw_order if el else None,
            )
        )

    def sort_key(row: LegendRow) -> tuple:
        return (
            _GROUP_ORDER.index(row.group) if row.group in _GROUP_ORDER else 9,
            -(row.draw_order if row.draw_order is not None else -1),
            row.type_code,
        )

    return sorted(rows, key=sort_key)


def _label_from_tags(tags: list[tuple[str, int | None]], type_code: int) -> str:
    if tags:
        return ", ".join(t for t, _ in tags)
    return f"type {type_code:#04x}"


# --------------------------------------------------------------------------- #
# Swatch + HTML rendering
# --------------------------------------------------------------------------- #
_SWATCH_W = 160
_SWATCH_H = 34

# Px per bitmap pixel. Garmin line patterns are 32 wide and a few tall, so lines
# are drawn at 2x (a 2 px bitmap -> a 4 px line, matching the `LineWidth * 2`
# used for solid lines); area hatches are typically 32x32 and draw 1:1 so a whole
# tile fits the swatch height. Both *tile* rather than stretch — stretching one
# period across the swatch exaggerates the dash spacing several-fold and makes
# the swatch useless for judging a dash pattern.
_LINE_CELL = 2
_AREA_CELL = 1


def _xpm_tiles_svg(xpm: Xpm, *, cell: float, width: float, height: float) -> str:
    """Tile ``xpm`` at ``cell`` px per bitmap pixel to fill ``width`` x ``height``.

    The pattern repeats at its true aspect ratio (as the device repeats it along a
    line / across an area) and is clipped at the box edges. Horizontal runs of one
    colour merge into a single rect, so a 10-px dash is one rect, not ten.
    """
    if not xpm.pixels or xpm.width <= 0 or xpm.height <= 0:
        return ""
    tile_w = xpm.width * cell
    tile_h = xpm.height * cell
    rects: list[str] = []
    y = 0.0
    while y < height - 1e-9:
        for ry, row in enumerate(xpm.pixels):
            ry_top = y + ry * cell
            if ry_top >= height - 1e-9:
                break
            rh = min(cell, height - ry_top)
            # Run-length merge this row, then repeat the row across the width.
            runs: list[tuple[int, int, str]] = []  # (start_col, span, colour)
            col = 0
            while col < len(row):
                idx = row[col]
                span = 1
                while col + span < len(row) and row[col + span] == idx:
                    span += 1
                colour = xpm.colours[idx] if idx < len(xpm.colours) else None
                if colour:
                    runs.append((col, span, colour))
                col += span
            x0 = 0.0
            while x0 < width - 1e-9:
                for start, span, colour in runs:
                    rx = x0 + start * cell
                    if rx >= width - 1e-9:
                        break
                    rw = min(span * cell, width - rx)
                    rects.append(
                        f'<rect x="{rx:.2f}" y="{ry_top:.2f}" '
                        f'width="{rw:.2f}" height="{rh:.2f}" fill="{colour}"/>'
                    )
                x0 += tile_w
        y += tile_h
    return "".join(rects)


def _swatch_svg(row: LegendRow) -> str:
    """An inline SVG swatch drawn from the row's TYP styling (or a neutral stub)."""
    el = row.element
    w, h = _SWATCH_W, _SWATCH_H
    open_tag = (
        f'<svg class="sw" width="{w}" height="{h}" viewBox="0 0 {w} {h}" '
        f'xmlns="http://www.w3.org/2000/svg">'
    )
    if el is None or el.xpm is None:
        # Rule-only type: Garmin's built-in look, we don't know the colour.
        return (
            open_tag
            + '<rect width="100%" height="100%" fill="#f4f1ea" '
            'stroke="#ddd7c8" stroke-dasharray="3 3"/>'
            + f'<text x="{w/2}" y="{h/2+4}" text-anchor="middle" '
            f'font-size="11" fill="#999" font-family="sans-serif">Garmin default</text>'
            "</svg>"
        )

    if row.group == "polygon":
        if el.xpm.pixels:
            # Hatch/pattern fill: tile it at 1:1 over a light ground.
            body = f'<rect width="{w}" height="{h}" fill="#fbf8f2"/>' + _xpm_tiles_svg(
                el.xpm, cell=_AREA_CELL, width=w, height=h
            )
        else:
            body = f'<rect width="{w}" height="{h}" fill="{el.xpm.primary or "#ccc"}"/>'
        body += f'<rect width="{w}" height="{h}" fill="none" stroke="#999" stroke-width="1"/>'
        return open_tag + body + "</svg>"

    if row.group == "line":
        y = h / 2
        stroke = el.xpm.primary or "#333"
        width = max((el.line_width or 1) * 2, 2)
        border = ""
        if el.border_width and len(el.xpm.colours) > 1 and el.xpm.colours[1]:
            border = (
                f'<line x1="6" y1="{y}" x2="{w-6}" y2="{y}" '
                f'stroke="{el.xpm.colours[1]}" stroke-width="{width + el.border_width*2}"/>'
            )
        if el.xpm.pixels:
            # Dash/pattern line: repeat the bitmap along the strip at true scale,
            # so the dash period on screen matches the pattern (mkgmap ignores
            # LineWidth once a bitmap is present — the Xpm height is the width).
            strip_h = el.xpm.height * _LINE_CELL
            strip = _xpm_tiles_svg(
                el.xpm, cell=_LINE_CELL, width=w - 12, height=strip_h
            )
            body = f'<g transform="translate(6,{y - strip_h / 2:.2f})">{strip}</g>'
        else:
            body = (
                border
                + f'<line x1="6" y1="{y}" x2="{w-6}" y2="{y}" '
                f'stroke="{stroke}" stroke-width="{width}" stroke-linecap="round"/>'
            )
        return open_tag + '<rect width="100%" height="100%" fill="#fbf8f2"/>' + body + "</svg>"

    # point
    colour = el.xpm.primary or "#c0392b"
    if el.xpm.pixels:
        # Icon: draw the bitmap once at 1:1, centred (no tiling).
        icon_w = el.xpm.width * _AREA_CELL
        icon_h = el.xpm.height * _AREA_CELL
        body = _xpm_tiles_svg(el.xpm, cell=_AREA_CELL, width=icon_w, height=icon_h)
        body = f'<g transform="translate({w/2 - icon_w/2:.2f},{h/2 - icon_h/2:.2f})">{body}</g>'
    else:
        body = f'<circle cx="{w/2}" cy="{h/2}" r="6" fill="{colour}" stroke="#333" stroke-width="1"/>'
    return open_tag + '<rect width="100%" height="100%" fill="#fbf8f2"/>' + body + "</svg>"


def _esc(s: str) -> str:
    return html.escape(str(s), quote=True)


def _row_html(row: LegendRow) -> str:
    tags_html = (
        "".join(
            f'<span class="tag">{_esc(tag)}'
            + (f'<span class="res">r{res}</span>' if res is not None else "")
            + "</span>"
            for tag, res in row.tags
        )
        or '<span class="tag none">no style rule (TYP only)</span>'
    )
    meta = f'<code>{row.type_code:#04x}</code>'
    if row.draw_order is not None:
        meta += f'<span class="draw">draw {row.draw_order}</span>'
    if row.element is None:
        meta += '<span class="draw untyped">no TYP</span>'
    return (
        '<div class="row">'
        f'<div class="swatch">{_swatch_svg(row)}</div>'
        '<div class="info">'
        f'<div class="label">{_esc(row.label)}</div>'
        f'<div class="tags">{tags_html}</div>'
        "</div>"
        f'<div class="meta">{meta}</div>'
        "</div>"
    )


def render_html(rows: list[LegendRow], *, live: bool = False) -> str:
    """Render the legend ``rows`` to a complete standalone HTML document.

    ``live=True`` embeds the hot-reload poll script (used by :func:`serve`); the
    static file written by :func:`render_legend` omits it.
    """
    sections: list[str] = []
    for group in _GROUP_ORDER:
        grouped = [r for r in rows if r.group == group]
        if not grouped:
            continue
        sections.append(
            f'<h2>{_esc(_GROUP_TITLES.get(group, group))} '
            f'<span class="count">{len(grouped)}</span></h2>'
        )
        sections.append('<div class="grid">')
        sections.extend(_row_html(r) for r in grouped)
        sections.append("</div>")

    live_script = _LIVE_SCRIPT if live else ""
    return _PAGE.format(body="\n".join(sections), live=live_script)


_LIVE_SCRIPT = """
<script>
(function () {
  let last = null;
  async function poll() {
    try {
      const r = await fetch('/api/version', {cache: 'no-store'});
      const v = await r.text();
      if (last === null) last = v;
      else if (v !== last) { location.reload(); return; }
    } catch (e) { /* server restarting; keep polling */ }
    setTimeout(poll, 700);
  }
  poll();
})();
</script>
"""

_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>topovert style legend</title>
<style>
  :root {{ color-scheme: light; }}
  body {{
    margin: 0; padding: 24px 28px 60px;
    font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
    background: #f7f5ef; color: #2a2a28;
  }}
  header {{ margin-bottom: 8px; }}
  h1 {{ font-size: 20px; margin: 0 0 4px; }}
  .sub {{ color: #6a6a64; font-size: 13px; margin: 0 0 20px; max-width: 60ch; }}
  h2 {{
    font-size: 14px; text-transform: uppercase; letter-spacing: .05em;
    color: #7a6a4a; border-bottom: 1px solid #ddd7c8; padding-bottom: 6px;
    margin: 28px 0 12px;
  }}
  .count {{
    font-size: 11px; background: #e7e0cf; color: #7a6a4a; border-radius: 8px;
    padding: 1px 7px; margin-left: 6px; vertical-align: middle;
  }}
  .grid {{
    display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
    gap: 10px;
  }}
  .row {{
    display: flex; align-items: center; gap: 12px;
    background: #fff; border: 1px solid #e6e1d4; border-radius: 8px;
    padding: 8px 12px;
  }}
  .swatch {{ flex: 0 0 auto; line-height: 0; }}
  .sw {{ border-radius: 4px; display: block; }}
  .info {{ flex: 1 1 auto; min-width: 0; }}
  .label {{ font-weight: 600; font-size: 14px; }}
  .tags {{ margin-top: 3px; display: flex; flex-wrap: wrap; gap: 4px; }}
  .tag {{
    font-size: 11px; font-family: ui-monospace, monospace;
    background: #eef2f6; color: #33556e; border-radius: 4px; padding: 1px 5px;
  }}
  .tag.none {{ background: #f3eee2; color: #9a8a68; font-style: italic; }}
  .res {{ margin-left: 4px; color: #8aa; font-size: 10px; }}
  .meta {{ flex: 0 0 auto; text-align: right; font-size: 11px; color: #999; }}
  .meta code {{ font-size: 12px; color: #555; }}
  .draw {{ display: block; margin-top: 2px; }}
  .untyped {{ color: #c08040; }}
  footer {{ margin-top: 40px; color: #9a948a; font-size: 12px; }}
</style>
</head>
<body>
<header>
  <h1>topovert style legend</h1>
  <p class="sub">Every rendered element, drawn from the bundled TYP colours and
  the style rules. Swatches reflect <em>our</em> styling (what we ask mkgmap to
  draw), not a device's final render. <code>r18</code> = mkgmap resolution
  (smaller shows at further-out zoom). Edit the style files and this page
  reloads.</p>
</header>
{body}
<footer>Generated by topovert &middot; styles/topovert_typ.txt + styles/topovert/&#123;polygons,lines,points&#125;</footer>
{live}
</body>
</html>
"""


# --------------------------------------------------------------------------- #
# Model assembly from disk
# --------------------------------------------------------------------------- #
def _read_rules(style_dir: Path) -> dict[str, str]:
    texts: dict[str, str] = {}
    for name in _RULE_FILES:
        p = style_dir / name
        if p.is_file():
            texts[name] = p.read_text(encoding="utf-8")
    return texts


def build_legend(
    *, typ_file: Path = TYP_FILE, style_dir: Path = STYLE_DIR
) -> list[LegendRow]:
    """Read the TYP + style rules from disk and build the joined legend rows."""
    elements = parse_typ(typ_file.read_text(encoding="utf-8"))
    rules = parse_style_rules(_read_rules(style_dir))
    return build_rows(elements, rules)


def render_legend(
    out: Path, *, typ_file: Path = TYP_FILE, style_dir: Path = STYLE_DIR
) -> Path:
    """Write a standalone ``legend.html`` for the current style + TYP. Returns it."""
    out = out.resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    rows = build_legend(typ_file=typ_file, style_dir=style_dir)
    out.write_text(render_html(rows, live=False), encoding="utf-8")
    log.info("wrote %s (%d legend rows)", out, len(rows))
    return out


# --------------------------------------------------------------------------- #
# Hot-reload server
# --------------------------------------------------------------------------- #
def _watched_paths(typ_file: Path, style_dir: Path) -> list[Path]:
    return [typ_file] + [style_dir / n for n in _RULE_FILES]


def _version_token(typ_file: Path, style_dir: Path) -> str:
    """A cheap change token: the max mtime_ns over the watched files."""
    latest = 0
    for p in _watched_paths(typ_file, style_dir):
        try:
            latest = max(latest, p.stat().st_mtime_ns)
        except OSError:
            continue
    return str(latest)


def serve(
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
    typ_file: Path = TYP_FILE,
    style_dir: Path = STYLE_DIR,
    open_browser: bool = True,
) -> None:
    """Serve the legend with hot reload until interrupted (Ctrl-C).

    ``GET /`` re-reads the style files and renders the page fresh, so a browser
    reload always shows the current styling. The page polls ``GET /api/version``
    (the watched files' max mtime); when it changes, the page reloads itself.
    """
    import webbrowser
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # quieter than the default stderr spam
            log.debug("http %s", args[0] % args[1:] if args else "")

        def _send(self, body: bytes, content_type: str) -> None:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802 (stdlib naming)
            if self.path.startswith("/api/version"):
                self._send(
                    _version_token(typ_file, style_dir).encode(), "text/plain"
                )
                return
            if self.path in ("/", "/index.html"):
                try:
                    rows = build_legend(typ_file=typ_file, style_dir=style_dir)
                    body = render_html(rows, live=True).encode("utf-8")
                except Exception as exc:  # a bad edit shouldn't kill the server
                    body = _error_page(exc).encode("utf-8")
                self._send(body, "text/html; charset=utf-8")
                return
            self.send_error(404)

    httpd = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{host}:{port}/"
    log.info("serving style legend at %s (Ctrl-C to stop)", url)
    print(f"topovert style legend: {url}")
    print("Edit styles/topovert_typ.txt or styles/topovert/{polygons,lines,points}; "
          "the page hot-reloads.")
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        httpd.server_close()


def _error_page(exc: Exception) -> str:
    body = (
        f'<h2 style="color:#b00">Style error</h2>'
        f"<pre>{_esc(repr(exc))}</pre>"
        "<p>Fix the style file and save; this page will reload.</p>"
    )
    return _PAGE.format(body=body, live=_LIVE_SCRIPT)
