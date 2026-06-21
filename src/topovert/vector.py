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

# Default layers (exact swissTLM3D GDB names, uppercase). Adding a feature class
# is data-only: classify it in :func:`_layer_kind` and give its OBJEKTART table
# below. Classification is case-insensitive so other deliveries/casing resolve.
# Water *areas* come from TLM_BODENBEDECKUNG polygons (lakes + wide rivers); the
# TLM_STEHENDES_GEWAESSER layer is unclosed shoreline *lines* that can't fill as
# area without polygonisation (follow-up topovert-ktq).
DEFAULT_TLM_LAYERS = (
    "TLM_STRASSE",            # roads / paths
    "TLM_EISENBAHN",          # railways
    "TLM_UEBRIGE_BAHN",       # aerialways (cable car / gondola / chair / drag lift)
    "TLM_FLIESSGEWAESSER",    # watercourse lines
    "TLM_BODENBEDECKUNG",     # land cover (water, forest, rock, glacier, ...)
    "TLM_GEBAEUDE_FOOTPRINT", # buildings
    "TLM_EINZELOBJEKT",       # POIs (spring, cave entrance, monument, ...)
    "TLM_MAUER",              # walls
)

# OBJEKTART code tables, authoritative per the swisstopo "Objektkatalog swissTLM3D"
# (v2.4). OBJEKTART is an *integer* in the data, not a German label. A value of
# ``None`` (explicit, or via the per-kind default) drops the feature. Verify codes
# with `ogrinfo -sql "SELECT DISTINCT OBJEKTART FROM <layer>" <src>`.

# TLM_STRASSE.OBJEKTART -> highway. (Code 14 Faehre is special-cased to route=ferry.)
ROAD_OBJEKTART = {
    0: {"highway": "motorway_link"},   # Ausfahrt (exit ramp)
    1: {"highway": "motorway_link"},   # Einfahrt (entry ramp)
    2: {"highway": "motorway"},        # Autobahn
    3: {"highway": "service"},         # Raststaette (rest-area service road)
    4: None,                           # Verbindung (virtual connector axis)
    5: {"highway": "unclassified"},    # Zufahrt (ramp<->road link)
    6: {"highway": "service"},         # Dienstzufahrt (service access)
    8: {"highway": "secondary"},       # 10m Strasse (wide main road)
    9: {"highway": "tertiary"},        # 6m Strasse
    10: {"highway": "unclassified"},   # 4m Strasse
    11: {"highway": "residential"},    # 3m Strasse (narrow side road)
    12: {"highway": "service"},        # Platz (square / parking axis)
    13: None,                          # Autozug (car-carrying train route)
    14: None,                          # Faehre (ferry) -> route=ferry (special-cased)
    15: {"highway": "track"},          # 2m Weg (drivable track)
    16: {"highway": "path"},           # 1m Weg
    17: {"highway": "path"},           # 1m Wegfragment
    18: {"highway": "track"},          # 2m Wegfragment
    19: {"highway": "path"},           # Markierte Spur (marked route)
    20: {"highway": "primary"},        # 8m Strasse
    21: {"highway": "trunk"},          # Autostrasse (expressway)
    22: {"highway": "path"},           # Klettersteig (via ferrata)
    23: {"highway": "path"},           # Provisorium (provisional slow-traffic axis)
}
ROAD_DEFAULT = {"highway": "road"}
ROAD_HIGHWAY_DEFAULT = "road"  # back-compat alias for the default highway value

# TLM_EISENBAHN.OBJEKTART -> railway.
RAILWAY_OBJEKTART = {
    0: {"railway": "rail"},            # Normalspur (standard gauge)
    2: {"railway": "narrow_gauge"},    # Schmalspur
    4: {"railway": "rail"},            # Schmalspur mit Normalspur (three-rail)
    5: {"railway": "narrow_gauge"},    # Kleinbahn
}
RAILWAY_DEFAULT = {"railway": "rail"}

# TLM_UEBRIGE_BAHN.OBJEKTART -> aerialway (None drops conveyors / vertical lifts).
AERIALWAY_OBJEKTART = {
    0: {"aerialway": "cable_car"},     # Luftseilbahn
    1: {"aerialway": "gondola"},       # Gondelbahn
    2: {"aerialway": "chair_lift"},    # Sesselbahn
    3: {"aerialway": "goods"},         # Transportseil (material ropeway)
    4: None,                           # Foerderband (conveyor belt)
    5: {"aerialway": "drag_lift"},     # Skilift
    7: None,                           # Lift (public vertical lift)
}

# TLM_BODENBEDECKUNG.OBJEKTART -> land-cover area tags.
LANDCOVER_OBJEKTART = {
    1: {"natural": "bare_rock"},       # Fels
    2: {"natural": "bare_rock"},       # Fels locker
    3: {"natural": "scree"},           # Felsbloecke
    4: {"natural": "scree"},           # Felsbloecke locker
    5: {"natural": "water"},           # Fliessgewaesser (river surface)
    6: {"natural": "scrub"},           # Gebueschwald (brush forest)
    7: {"natural": "scree"},           # Lockergestein
    8: {"natural": "scree"},           # Lockergestein locker
    9: {"natural": "glacier"},         # Gletscher
    10: {"natural": "water"},          # Stehende Gewaesser (lake)
    11: {"natural": "wetland"},        # Feuchtgebiet
    12: {"natural": "wood"},           # Wald
    13: {"natural": "wood"},           # Wald offen
    14: {"natural": "scrub"},          # Gehoelzflaeche
    15: {"natural": "glacier"},        # Schneefeld Toteis
}

# TLM_EINZELOBJEKT.OBJEKTART -> POI node tags.
POI_OBJEKTART = {
    1: {"historic": "wayside_shrine"}, # Bildstock
    2: {"amenity": "fountain"},        # Brunnen
    3: {"historic": "monument"},       # Denkmal
    4: {"man_made": "cross"},          # Gipfelkreuz (summit cross)
    5: {"natural": "cave_entrance"},   # Grotte, Hoehle
    6: {"historic": "boundary_stone"}, # Landesgrenzstein
    7: {"natural": "spring"},          # Quelle
    8: {"man_made": "survey_point"},   # Triangulationspyramide
    9: {"waterway": "waterfall"},      # Wasserfall
    10: {"man_made": "water_works"},   # Wasserversorgung
}

# TLM_MAUER.OBJEKTART -> barrier.
BARRIER_OBJEKTART = {
    0: {"barrier": "wall"},            # Mauer
}
BARRIER_DEFAULT = {"barrier": "wall"}

# TLM_FLIESSGEWAESSER.OBJEKTART -> waterway lines (None drops penstocks / virtual axes).
WATERWAY_OBJEKTART = {
    0: {"waterway": "canal"},                          # Bisse / Suone (irrigation)
    1: None, 2: None, 3: None,                         # Druckleitung / -stollen (penstocks)
    4: {"waterway": "stream"},                         # Fliessgewaesser (stream / river)
    6: None,                                           # Seeachse (virtual lake axis)
    7: {"waterway": "stream", "intermittent": "yes"},  # Trockenrinne (dry channel)
}
WATERWAY_DEFAULT = {"waterway": "stream"}

# TLM_STEHENDES_GEWAESSER.OBJEKTART (shoreline *lines*, non-default — see
# topovert-ktq). Code 1 = lake outline, 0 = island outline.
LAKE_OBJEKTART = {
    0: None,                    # Seeinsel (island shoreline)
    1: {"natural": "water"},    # See (Seeuferlinie)
}

# kind -> (OBJEKTART table, default tags (None = drop unknown code), NAME fields).
_KIND_TABLES: dict[str, tuple[dict, dict | None, tuple[str, ...]]] = {
    "road":       (ROAD_OBJEKTART,      ROAD_DEFAULT,         ("strassenname", "name")),
    "railway":    (RAILWAY_OBJEKTART,   RAILWAY_DEFAULT,      ("name",)),
    "aerialway":  (AERIALWAY_OBJEKTART, None,                 ("name",)),
    "landcover":  (LANDCOVER_OBJEKTART, None,                 ("name",)),
    "poi":        (POI_OBJEKTART,       None,                 ("name",)),
    "barrier":    (BARRIER_OBJEKTART,   BARRIER_DEFAULT,      ("name",)),
    "waterway":   (WATERWAY_OBJEKTART,  WATERWAY_DEFAULT,     ("name",)),
    "water_line": (LAKE_OBJEKTART,      {"natural": "water"}, ("name",)),
    "building":   ({},                  {"building": "yes"},  ("name",)),
}

# Aggregated "tag map" (the data driving :func:`tags_for`), exposed for tests/tools.
TAG_MAP = {kind: spec[0] for kind, spec in _KIND_TABLES.items()}

# Export only the OBJEKTART rows we actually map (keeps big layers cheap). Built
# from the land-cover table so widening coverage updates the filter automatically.
LAYER_WHERE = {
    "TLM_BODENBEDECKUNG": "OBJEKTART IN ("
    + ", ".join(str(c) for c in sorted(LANDCOVER_OBJEKTART)) + ")",
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
    if "eisenbahn" in name:
        return "railway"
    if "uebrige_bahn" in name:
        return "aerialway"
    if "fliessgewaesser" in name:
        return "waterway"
    if "bodenbedeckung" in name:
        return "landcover"
    if "einzelobjekt" in name:
        return "poi"
    if "mauer" in name:
        return "barrier"
    if "gebaeude" in name:
        return "building"
    if "stehende" in name or "see" in name:
        return "water_line"      # shoreline lines (non-default)
    return None


def tags_for(layer: str, props: dict) -> dict[str, str] | None:
    """OSM tags for a swissTLM3D feature, or ``None`` to drop it.

    Keyed by the logical kind derived from ``layer`` and the integer ``OBJEKTART``
    code (see :data:`_KIND_TABLES`); the source name is carried through.
    """
    kind = _layer_kind(layer)
    spec = _KIND_TABLES.get(kind or "")
    if spec is None:
        return None
    table, default, name_fields = spec
    code = _int_prop(props, "objektart")

    if kind == "road" and code == 14:  # Faehre
        base: dict | None = {"route": "ferry"}
    elif code in table:
        base = table[code]            # explicit mapping (may be None -> drop)
    else:
        base = default                # unknown code -> default (may be None -> drop)
    if base is None:
        return None

    tags = dict(base)
    name = _prop(props, *name_fields)
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
