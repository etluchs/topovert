"""Synthesize the OSM input mkgmap needs alongside ``--dem``.

mkgmap embeds the DEM as a subfile of a *map*, so it always needs some OSM input
to build that map. Two cases share the same primitives here:

* **hillshade-only** (no ``--tlm``): we emit a tiny valid ``.osm`` — the data
  bounds, a closed way around them (innocuous area type) and one named locality
  POI at the centre — so the produced map is unambiguously non-empty.
* **with vector features** (:mod:`topovert.vector`): the converter reuses the
  ``<bounds>``/node/way serializers below to stream swissTLM3D features into one
  combined ``.osm``.

Node/way ids are negative (the OSM convention for not-yet-uploaded objects);
:func:`escape_attr` keeps tag values XML-safe.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterable
from pathlib import Path
from xml.sax.saxutils import escape

OSM_HEADER = "<?xml version='1.0' encoding='UTF-8'?>\n<osm version='0.6' generator='topovert'>\n"
OSM_FOOTER = "</osm>\n"

# All attributes are single-quoted (matching the original template), so the
# escape map must cover the apostrophe plus newlines/tabs that would break a line.
_ATTR_ESCAPES = {"'": "&apos;", "\n": "&#10;", "\t": "&#9;", "\r": "&#13;"}


def escape_attr(value: str) -> str:
    """Single-quote-safe rendering of ``value`` (handles ``&``, ``<``, ``>``, ``'``)."""
    return escape(value, _ATTR_ESCAPES)


def bounds_element(bounds: tuple[float, float, float, float]) -> str:
    """``<bounds>`` line for ``(min_lon, min_lat, max_lon, max_lat)``."""
    min_lon, min_lat, max_lon, max_lat = bounds
    return (
        f"  <bounds minlat='{min_lat:.7f}' minlon='{min_lon:.7f}' "
        f"maxlat='{max_lat:.7f}' maxlon='{max_lon:.7f}'/>\n"
    )


def node_xml(node_id: int, lat: float, lon: float, tags: dict[str, str] | None = None) -> str:
    """A ``<node>`` element; childless when ``tags`` is empty."""
    head = f"  <node id='{node_id}' lat='{lat:.7f}' lon='{lon:.7f}'"
    if not tags:
        return head + "/>\n"
    body = "".join(_tag_xml(k, v, indent=4) for k, v in tags.items())
    return head + ">\n" + body + "  </node>\n"


def way_xml(way_id: int, node_ids: list[int], tags: dict[str, str] | None = None) -> str:
    """A ``<way>`` referencing ``node_ids`` in order, with ``tags``."""
    refs = "".join(f"    <nd ref='{nid}'/>\n" for nid in node_ids)
    body = "".join(_tag_xml(k, v, indent=4) for k, v in (tags or {}).items())
    return f"  <way id='{way_id}'>\n" + refs + body + "  </way>\n"


def _tag_xml(key: str, value: str, *, indent: int) -> str:
    pad = " " * indent
    return f"{pad}<tag k='{escape_attr(key)}' v='{escape_attr(value)}'/>\n"


def assemble_osm(
    bounds: tuple[float, float, float, float],
    dst: Path,
    fragment_streams: Iterable[Iterable[str]],
    *,
    keep_scratch: bool = False,
) -> int:
    """Assemble OSM XML fragment streams into one osmosis-ordered ``dst`` ``.osm``.

    ``fragment_streams`` is an iterable of iterables, each yielding ``<node>``/
    ``<way>`` XML fragments (as :func:`node_xml`/:func:`way_xml` produce). All
    streams MUST share one id allocator so node/way ids stay unique and ascending
    across them. splitter requires the file osmosis-ordered (all nodes, ascending
    id, before any way), so fragments are routed to separate node/way temp files
    and concatenated — nodes block, then ways. Returns the ``<node>`` count (the
    metric deciding whether the pipeline runs splitter).
    """
    nodes_path = dst.parent / (dst.name + ".nodes")
    ways_path = dst.parent / (dst.name + ".ways")
    nodes = 0
    with nodes_path.open("w", encoding="utf-8") as nf, \
            ways_path.open("w", encoding="utf-8") as wf:
        for stream in fragment_streams:
            for fragment in stream:
                if fragment.startswith("  <node"):
                    nf.write(fragment)
                    nodes += 1
                else:
                    wf.write(fragment)

    with dst.open("w", encoding="utf-8") as fh:
        fh.write(OSM_HEADER)
        fh.write(bounds_element(bounds))
        for part in (nodes_path, ways_path):
            with part.open(encoding="utf-8") as pf:
                shutil.copyfileobj(pf, fh)
        fh.write(OSM_FOOTER)
    if not keep_scratch:
        nodes_path.unlink(missing_ok=True)
        ways_path.unlink(missing_ok=True)
    return nodes


def write_bounds_osm(
    bounds: tuple[float, float, float, float], dst: Path, name: str = "topovert"
) -> Path:
    """Write the minimal bounds ``.osm`` for ``bounds`` (min_lon,min_lat,max_lon,max_lat).

    Used for the hillshade-only build (no vector features). Emits the bounds, a
    closed ring tagged ``natural=heath`` and a single named locality POI so the
    map is non-empty.
    """
    min_lon, min_lat, max_lon, max_lat = bounds
    ctr_lon, ctr_lat = (min_lon + max_lon) / 2, (min_lat + max_lat) / 2

    parts = [OSM_HEADER, bounds_element(bounds)]
    parts.append(node_xml(-1, min_lat, min_lon))
    parts.append(node_xml(-2, min_lat, max_lon))
    parts.append(node_xml(-3, max_lat, max_lon))
    parts.append(node_xml(-4, max_lat, min_lon))
    parts.append(node_xml(-5, ctr_lat, ctr_lon, {"place": "locality", "name": name}))
    parts.append(way_xml(-1, [-1, -2, -3, -4, -1], {"natural": "heath"}))
    parts.append(OSM_FOOTER)

    dst.write_text("".join(parts), encoding="utf-8")
    return dst
