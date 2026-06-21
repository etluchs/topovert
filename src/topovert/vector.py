"""Convert swissTLM3D vector features into the OSM that mkgmap compiles.

Design constraint (see ``CLAUDE.md``): topovert shells out to the GDAL **CLI**, never
the ``osgeo`` Python bindings. ``ogr2ogr`` cannot *write* OSM (its OSM driver is
read-only) but it can write **GeoJSON**, so the pipeline is:

    swissTLM3D .gpkg  --ogr2ogr (reproject 2056->4326, -f GeoJSONSeq)-->  *.geojsonl
                      --geojson_to_osm (this module, stdlib only)------>  features.osm
                      --mkgmap --dem ----------------------------------->  .IMG

The attribute->tag mapping lives in :data:`TAG_MAP` as plain data so it stays
unit-testable and extending coverage is data-only. Slice 1 ("core nav set") wires
roads, watercourses, lakes and building footprints.

``GeoJSONSeq`` emits one feature per line (optionally RS-prefixed), so conversion
streams line-by-line and never holds a whole layer in memory.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Iterator
from pathlib import Path

from . import TopovertError, gdal_tools, osm

log = logging.getLogger(__name__)

# swissTLM3D ships as EPSG:2056 (LV95), same as the DEM.
DEFAULT_SOURCE_EPSG = 2056

# Default core-nav layers (exact swissTLM3D GDB names, uppercase). Water *areas*
# come from TLM_BODENBEDECKUNG polygons (lakes + wide rivers), filtered to water
# OBJEKTART at the source (see LAYER_WHERE) — the TLM_STEHENDES_GEWAESSER layer is
# unclosed shoreline *lines* and can't fill as area without polygonisation
# (follow-up). Classification (:func:`_layer_kind`) is case-insensitive.
DEFAULT_TLM_LAYERS = (
    "TLM_STRASSE",
    "TLM_FLIESSGEWAESSER",
    "TLM_BODENBEDECKUNG",
    "TLM_GEBAEUDE_FOOTPRINT",
)

# Per-layer OGR SQL filter applied at export time so we only stream the rows we
# map (e.g. just the water polygons out of all land-cover).
LAYER_WHERE = {
    "TLM_BODENBEDECKUNG": "OBJEKTART IN (5, 10)",  # 5 Fliessgewaesser, 10 Stehende Gewaesser
}

# OBJEKTART code tables, authoritative per the swisstopo "Objektkatalog swissTLM3D"
# (v2.4). OBJEKTART is an *integer* in the data, not a German label. ``None`` drops
# the feature (virtual/non-map geometry); unknown codes fall back to a sensible
# default. Reconciled against real data via `ogrinfo -sql "SELECT DISTINCT ..."`.

# TLM_STRASSE.OBJEKTART -> OSM highway value.
ROAD_OBJEKTART: dict[int, str | None] = {
    0: "motorway_link",   # Ausfahrt (exit ramp)
    1: "motorway_link",   # Einfahrt (entry ramp)
    2: "motorway",        # Autobahn
    3: "service",         # Raststaette (rest-area service road)
    4: None,              # Verbindung (virtual connector axis)
    5: "unclassified",    # Zufahrt (ramp<->road link)
    6: "service",         # Dienstzufahrt (maintenance/service access)
    8: "secondary",       # 10m Strasse (wide main road)
    9: "tertiary",        # 6m Strasse
    10: "unclassified",   # 4m Strasse
    11: "residential",    # 3m Strasse (narrow side road)
    12: "service",        # Platz (square / parking axis)
    13: None,             # Autozug (car-carrying train route)
    14: None,             # Faehre (ferry) -> special-cased to route=ferry
    15: "track",          # 2m Weg (drivable track)
    16: "path",           # 1m Weg
    17: "path",           # 1m Wegfragment
    18: "track",          # 2m Wegfragment
    19: "path",           # Markierte Spur (marked route)
    20: "primary",        # 8m Strasse
    21: "trunk",          # Autostrasse (expressway)
    22: "path",           # Klettersteig (via ferrata)
    23: "path",           # Provisorium (provisional slow-traffic axis)
}
ROAD_HIGHWAY_DEFAULT = "road"

# TLM_FLIESSGEWAESSER.OBJEKTART -> tags (None drops penstocks / virtual axes).
WATERWAY_OBJEKTART: dict[int, dict[str, str] | None] = {
    0: {"waterway": "canal"},                          # Bisse / Suone (irrigation)
    1: None, 2: None, 3: None,                         # Druckleitung / -stollen (penstocks)
    4: {"waterway": "stream"},                         # Fliessgewaesser (stream / river)
    6: None,                                           # Seeachse (virtual lake axis)
    7: {"waterway": "stream", "intermittent": "yes"},  # Trockenrinne (dry channel)
}
WATERWAY_DEFAULT = {"waterway": "stream"}

# TLM_BODENBEDECKUNG.OBJEKTART -> tags. The water polygons (filtered via
# LAYER_WHERE) become filled water areas; any other land cover is dropped.
WATER_AREA_OBJEKTART: dict[int, dict[str, str] | None] = {
    5: {"natural": "water"},    # Fliessgewaesser (river surface)
    10: {"natural": "water"},   # Stehende Gewaesser (lake)
}

# TLM_STEHENDES_GEWAESSER.OBJEKTART (shoreline *lines*, not in the default set).
# Kept for explicit --tlm-layer use; code 1 = lake outline, 0 = island outline.
LAKE_OBJEKTART: dict[int, dict[str, str] | None] = {
    0: None,                    # Seeinsel (island shoreline)
    1: {"natural": "water"},    # See (Seeuferlinie)
}

# Aggregated "tag map" (the data driving :func:`tags_for`), exposed for tests/tools.
TAG_MAP = {
    "road": ROAD_OBJEKTART,
    "waterway": WATERWAY_OBJEKTART,
    "water_area": WATER_AREA_OBJEKTART,
    "water_line": LAKE_OBJEKTART,
    "building": {"*": {"building": "yes"}},
}


def _prop(props: dict, *names: str) -> str | None:
    """Case-insensitive lookup of the first present property in ``names``."""
    lowered = {k.lower(): v for k, v in props.items()}
    for name in names:
        val = lowered.get(name.lower())
        if val not in (None, ""):
            return str(val)
    return None


def _int_prop(props: dict, *names: str) -> int | None:
    """Case-insensitive lookup coerced to ``int`` (handles "4"/4.0), else None."""
    raw = _prop(props, *names)
    if raw is None:
        return None
    try:
        return int(float(raw))
    except ValueError:
        return None


def _layer_kind(layer: str) -> str | None:
    """Map an arbitrary swissTLM3D layer name to a logical feature kind."""
    name = layer.lower()
    if "strasse" in name:
        return "road"
    if "fliessgewaesser" in name:
        return "waterway"
    if "bodenbedeckung" in name:
        return "water_area"      # filtered to water polygons via LAYER_WHERE
    if "stehende" in name or "see" in name:
        return "water_line"      # shoreline lines (non-default)
    if "gebaeude" in name:
        return "building"
    return None


def tags_for(layer: str, props: dict) -> dict[str, str] | None:
    """OSM tags for a swissTLM3D feature, or ``None`` to drop it.

    Keyed by the logical kind derived from ``layer`` and the integer ``OBJEKTART``
    code; the source name (``STRASSENNAME``/``NAME``) is carried through.
    """
    kind = _layer_kind(layer)
    if kind is None:
        return None
    code = _int_prop(props, "objektart")

    if kind == "road":
        if code == 14:  # Faehre
            tags: dict[str, str] = {"route": "ferry"}
        else:
            highway = ROAD_OBJEKTART.get(code, ROAD_HIGHWAY_DEFAULT)
            if highway is None:
                return None
            tags = {"highway": highway}
        name = _prop(props, "strassenname", "name")
    elif kind == "waterway":
        base = WATERWAY_OBJEKTART.get(code, WATERWAY_DEFAULT)
        if base is None:
            return None
        tags = dict(base)
        name = _prop(props, "name")
    elif kind == "water_area":
        base = WATER_AREA_OBJEKTART.get(code)
        if base is None:
            return None
        tags = dict(base)
        name = _prop(props, "name")
    elif kind == "water_line":
        base = LAKE_OBJEKTART.get(code, {"natural": "water"})
        if base is None:
            return None
        tags = dict(base)
        name = _prop(props, "name")
    else:  # building
        tags = {"building": "yes"}
        name = _prop(props, "name")

    if name:
        tags["name"] = name
    return tags


class _IdAllocator:
    """Hands out the negative ids OSM uses for not-yet-uploaded objects."""

    def __init__(self) -> None:
        self._n = 0

    def next(self) -> int:
        self._n -= 1
        return self._n


def _coords_to_nodes(coords: list, ids: _IdAllocator, out: list[str]) -> list[int]:
    """Emit a ``<node>`` per coordinate; return their ids in order."""
    node_ids: list[int] = []
    for lon, lat, *_ in coords:
        nid = ids.next()
        out.append(osm.node_xml(nid, float(lat), float(lon)))
        node_ids.append(nid)
    return node_ids


def _ring_way(ring: list, ids: _IdAllocator, tags: dict[str, str], out: list[str]) -> None:
    """Emit a closed way for a polygon ring (GeoJSON rings repeat the first point)."""
    pts = ring[:-1] if len(ring) > 1 and ring[0] == ring[-1] else ring
    node_ids = _coords_to_nodes(pts, ids, out)
    if len(node_ids) < 3:
        return
    out.append(osm.way_xml(ids.next(), node_ids + [node_ids[0]], tags))


def feature_to_osm(layer: str, feature: dict, ids: _IdAllocator) -> list[str]:
    """Serialize one GeoJSON feature to OSM XML fragments (``[]`` if unmapped)."""
    tags = tags_for(layer, feature.get("properties") or {})
    if not tags:
        return []
    geom = feature.get("geometry") or {}
    gtype = geom.get("type")
    coords = geom.get("coordinates")
    if not gtype or coords is None:
        return []

    out: list[str] = []
    if gtype == "Point":
        lon, lat, *_ = coords
        out.append(osm.node_xml(ids.next(), float(lat), float(lon), tags))
    elif gtype == "MultiPoint":
        for lon, lat, *_ in coords:
            out.append(osm.node_xml(ids.next(), float(lat), float(lon), tags))
    elif gtype == "LineString":
        node_ids = _coords_to_nodes(coords, ids, out)
        if len(node_ids) >= 2:
            out.append(osm.way_xml(ids.next(), node_ids, tags))
    elif gtype == "MultiLineString":
        for line in coords:
            node_ids = _coords_to_nodes(line, ids, out)
            if len(node_ids) >= 2:
                out.append(osm.way_xml(ids.next(), node_ids, tags))
    elif gtype == "Polygon":
        # Slice 1: outer ring only (holes deferred — see rn1 follow-ups).
        if coords:
            _ring_way(coords[0], ids, tags, out)
    elif gtype == "MultiPolygon":
        for polygon in coords:
            if polygon:
                _ring_way(polygon[0], ids, tags, out)
    return out


def geojson_to_osm(lines: Iterable[str], layer: str, ids: _IdAllocator) -> Iterator[str]:
    """Stream GeoJSONSeq ``lines`` for ``layer`` into OSM XML fragments."""
    for raw in lines:
        line = raw.lstrip("\x1e").strip()  # GeoJSONSeq may RS-prefix each record
        if not line:
            continue
        feature = json.loads(line)
        yield from feature_to_osm(layer, feature, ids)


def ogr_geojson_cmd(
    src: Path, layer: str, dst: Path, *, source_epsg: int, where: str | None = None
) -> list[str]:
    """argv for ``ogr2ogr`` reprojecting one ``layer`` to WGS84 GeoJSONSeq.

    ``where`` is an optional OGR SQL attribute filter applied at export time.
    """
    cmd = [
        "ogr2ogr",
        "-f", "GeoJSONSeq",
        "-s_srs", f"EPSG:{source_epsg}",
        "-t_srs", "EPSG:4326",
    ]
    if where:
        cmd += ["-where", where]
    cmd += [str(dst), str(src), layer]
    return cmd


def build_features_osm(
    src: Path,
    layers: Iterable[str],
    bounds: tuple[float, float, float, float],
    dst: Path,
    *,
    source_epsg: int = DEFAULT_SOURCE_EPSG,
) -> Path:
    """Convert ``layers`` of swissTLM3D ``src`` into one combined ``dst`` ``.osm``.

    Reprojects each layer to GeoJSONSeq with ``ogr2ogr`` then streams it through
    :func:`geojson_to_osm`. A layer missing from the source is warned and skipped
    (deliveries vary) rather than failing the whole build.
    """
    if not src.exists():
        raise TopovertError(f"--tlm source not found: {src}")

    ids = _IdAllocator()
    scratch = dst.parent
    with dst.open("w", encoding="utf-8") as fh:
        fh.write(osm.OSM_HEADER)
        fh.write(osm.bounds_element(bounds))
        for layer in layers:
            geojson = scratch / f"{layer}.geojsonl"
            try:
                gdal_tools._run(ogr_geojson_cmd(
                    src, layer, geojson, source_epsg=source_epsg,
                    where=LAYER_WHERE.get(layer),
                ))
            except TopovertError as exc:
                log.warning("skipping swissTLM3D layer %r: %s", layer, exc)
                continue
            with geojson.open(encoding="utf-8") as gj:
                count = 0
                for fragment in geojson_to_osm(gj, layer, ids):
                    fh.write(fragment)
                    count += 1
                log.info("layer %s -> %d OSM element(s)", layer, count)
        fh.write(osm.OSM_FOOTER)
    return dst
