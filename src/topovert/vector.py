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

# Layer names vary slightly between swissTLM3D deliveries (and case), so we
# classify by a substring of the layer name rather than pinning exact names.
# Verify the real names with `ogrinfo -so <gpkg>` (see the rn1 plan).
DEFAULT_TLM_LAYERS = (
    "tlm_strasse",
    "tlm_fliessgewaesser",
    "tlm_stehende_gewaesser",
    "tlm_gebaeude_footprint",
)

# objektart (TLM_STRASSE) -> OSM highway value. Best-effort against the documented
# swissTLM3D road-class domain; reconcile with `ogrinfo -so <gpkg> <layer>` against
# real data. Unknown road classes fall back to ROAD_HIGHWAY_DEFAULT.
ROAD_HIGHWAY = {
    "Autobahn": "motorway",
    "Autostrasse": "trunk",
    "Hauptstrasse": "primary",
    "Verbindungsstrasse": "secondary",
    "Verbindung": "secondary",
    "Sammelstrasse": "tertiary",
    "Nebenstrasse": "unclassified",
    "Quartierstrasse": "residential",
    "Zufahrt": "service",
    "Dienstzufahrt": "service",
    "Fahrweg": "track",
    "Feldweg": "track",
    "Wanderweg": "path",
    "Verbindungsweg": "footway",
    "Klettersteig": "path",
    "Markierte Route": "path",
}
ROAD_HIGHWAY_DEFAULT = "road"


def _prop(props: dict, *names: str) -> str | None:
    """Case-insensitive lookup of the first present property in ``names``."""
    lowered = {k.lower(): v for k, v in props.items()}
    for name in names:
        val = lowered.get(name.lower())
        if val not in (None, ""):
            return str(val)
    return None


def _layer_kind(layer: str) -> str | None:
    """Map an arbitrary swissTLM3D layer name to a logical feature kind."""
    name = layer.lower()
    if "strasse" in name or "strassen" in name:
        return "road"
    if "fliessgewaesser" in name:
        return "waterway"
    if "stehende" in name or "see" in name:
        return "water"
    if "gebaeude" in name:
        return "building"
    return None


def tags_for(layer: str, props: dict) -> dict[str, str] | None:
    """OSM tags for a swissTLM3D feature, or ``None`` to drop it.

    The mapping (:data:`TAG_MAP` semantics) is keyed by the logical kind derived
    from ``layer``; the source ``NAME`` attribute is carried through when present.
    """
    kind = _layer_kind(layer)
    if kind is None:
        return None

    tags: dict[str, str] = {}
    if kind == "road":
        objektart = _prop(props, "objektart")
        tags["highway"] = ROAD_HIGHWAY.get(objektart, ROAD_HIGHWAY_DEFAULT)
    elif kind == "waterway":
        tags["waterway"] = "stream"
    elif kind == "water":
        tags["natural"] = "water"
    elif kind == "building":
        tags["building"] = "yes"

    name = _prop(props, "name", "uuid_name")
    if name:
        tags["name"] = name
    return tags


# Public alias so callers/tests can introspect the supported kinds as data.
TAG_MAP = {
    "road": {"highway": ROAD_HIGHWAY_DEFAULT},
    "waterway": {"waterway": "stream"},
    "water": {"natural": "water"},
    "building": {"building": "yes"},
}


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


def ogr_geojson_cmd(src: Path, layer: str, dst: Path, *, source_epsg: int) -> list[str]:
    """argv for ``ogr2ogr`` reprojecting one ``layer`` to WGS84 GeoJSONSeq."""
    return [
        "ogr2ogr",
        "-f", "GeoJSONSeq",
        "-s_srs", f"EPSG:{source_epsg}",
        "-t_srs", "EPSG:4326",
        str(dst),
        str(src),
        layer,
    ]


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
                gdal_tools._run(ogr_geojson_cmd(src, layer, geojson, source_epsg=source_epsg))
            except TopovertError as exc:
                log.warning("skipping swissTLM3D layer %r: %s", layer, exc)
                continue
            with geojson.open(encoding="utf-8") as gj:
                count = 0
                for fragment in geojson_to_osm(gj, layer, ids):
                    fh.write(fragment)
                    count += 1
                log.info("layer %s -> %d OSM element group(s)", layer, count)
        fh.write(osm.OSM_FOOTER)
    return dst
