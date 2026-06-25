"""Derive elevation contour lines from the DEM and convert them to OSM.

Design constraint (see ``CLAUDE.md``): topovert shells out to the GDAL **CLI**,
never the ``osgeo`` Python bindings, and ships zero runtime pip deps. So contours
come from **``gdal_contour``** (part of the GDAL CLI we already require), *not*
``pyhgtmap`` — pyhgtmap pulls in numpy and the compiled ``osgeo`` bindings, the
same reason the vector path avoids ``ogr2pbf``. The pipeline mirrors the vector
one:

    DEM mosaic .vrt   --gdal_contour (-i interval, -a ele)----->  contours.gpkg
                      --ogr2ogr (reproject ->4326, GeoJSONSeq)-->  *.geojsonl
                      --(this module, stdlib only)-------------->  OSM <way>s
                      --mkgmap --------------------------------->  .IMG

The GPKG intermediate is what preserves the source CRS exactly (GeoJSON's
RFC 7946 WGS84 assumption is lossy), so the reprojection step is unambiguous.

Each contour way is tagged ``contour=elevation`` + ``ele=<m>`` and classified
``contour_ext=elevation_major`` (every ``MAJOR_EVERY``-th line, drawn bold) or
``elevation_minor``; the bundled style maps those to Garmin contour line types.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from pathlib import Path

from . import gdal_tools, osm, vector

log = logging.getLogger(__name__)

# Contour spacing in metres, and how often a bold "index" contour falls. 20 m
# with every 5th bold (i.e. an index line each 100 m) is a common topo cadence.
DEFAULT_INTERVAL = 20
MAJOR_EVERY = 5

# gdal_contour's default output layer name and the elevation attribute we request.
_CONTOUR_LAYER = "contour"
_ELEV_ATTR = "ele"


def gdal_contour_cmd(
    src: Path, dst: Path, *, interval: int, elev_attr: str = _ELEV_ATTR
) -> list[str]:
    """argv for ``gdal_contour`` writing a GPKG of contour lines from ``src``.

    ``-i`` sets the spacing in the DEM's vertical unit (metres for swissALTI3D /
    Copernicus); ``-a`` names the elevation attribute. GPKG output preserves the
    source CRS so the later ``ogr2ogr`` reprojection to WGS84 is unambiguous.
    """
    return [
        "gdal_contour",
        "-i", str(interval),
        "-a", elev_attr,
        "-f", "GPKG",
        str(src),
        str(dst),
    ]


def contour_tags(ele: float, *, interval: int, major_every: int) -> dict[str, str]:
    """OSM tags for a contour at ``ele`` metres.

    ``contour=elevation`` + integer ``ele`` identify it; ``contour_ext`` marks it
    a bold index line (``elevation_major``) when its step is a multiple of
    ``major_every``, else ``elevation_minor`` — the style renders the two
    differently.
    """
    tags = {"contour": "elevation", "ele": str(int(round(ele)))}
    is_major = (
        interval > 0
        and major_every > 0
        and round(ele / interval) % major_every == 0
    )
    tags["contour_ext"] = "elevation_major" if is_major else "elevation_minor"
    return tags


def _emit_line(coords: list, ids: vector._IdAllocator, tags: dict[str, str], out: list[str]) -> None:
    """Emit nodes + an (open) way for one contour line geometry."""
    node_ids = vector._coords_to_nodes(coords, ids, out)
    if len(node_ids) >= 2:
        out.append(osm.way_xml(ids.way(), node_ids, tags))


def _feature_to_osm(
    feature: dict, ids: vector._IdAllocator, *, interval: int, major_every: int
) -> list[str]:
    """Serialize one contour GeoJSON feature (Line/MultiLineString) to OSM XML."""
    props = feature.get("properties") or {}
    ele = props.get(_ELEV_ATTR)
    if ele is None:
        return []
    tags = contour_tags(float(ele), interval=interval, major_every=major_every)
    geom = feature.get("geometry") or {}
    gtype = geom.get("type")
    coords = geom.get("coordinates")
    if not coords:
        return []
    out: list[str] = []
    if gtype == "LineString":
        _emit_line(coords, ids, tags, out)
    elif gtype == "MultiLineString":
        for line in coords:
            _emit_line(line, ids, tags, out)
    return out


def iter_contour_osm(
    dem_src: Path,
    ids: vector._IdAllocator,
    scratch: Path,
    *,
    interval: int = DEFAULT_INTERVAL,
    major_every: int = MAJOR_EVERY,
    source_epsg: int | None = None,
    keep_geojson: bool = False,
) -> Iterator[str]:
    """Stream contour lines for the DEM ``dem_src`` as OSM XML fragments.

    Runs ``gdal_contour`` (into ``scratch``) then reprojects to WGS84 GeoJSONSeq
    with ``ogr2ogr`` (reusing :func:`topovert.vector.ogr_geojson_cmd`), allocating
    ids from the shared ``ids``. Combine with :func:`topovert.osm.assemble_osm` to
    write the ``.osm``. Intermediates are removed unless ``keep_geojson``.
    """
    gpkg = scratch / "contours.gpkg"
    geojson = scratch / "contours.geojsonl"
    gdal_tools._run(gdal_contour_cmd(dem_src, gpkg, interval=interval))
    gdal_tools._run(vector.ogr_geojson_cmd(
        gpkg, _CONTOUR_LAYER, geojson, source_epsg=source_epsg
    ))
    lines = 0
    with geojson.open(encoding="utf-8") as gj:
        for raw in gj:
            line = raw.lstrip("\x1e").strip()  # GeoJSONSeq may RS-prefix records
            if not line:
                continue
            feature = json.loads(line)
            yield from _feature_to_osm(
                feature, ids, interval=interval, major_every=major_every
            )
            lines += 1
    log.info("contours -> %d line feature(s) at %d m spacing", lines, interval)
    if not keep_geojson:
        gpkg.unlink(missing_ok=True)
        geojson.unlink(missing_ok=True)


def build_contour_osm(
    dem_src: Path,
    bounds: tuple[float, float, float, float],
    dst: Path,
    *,
    interval: int = DEFAULT_INTERVAL,
    major_every: int = MAJOR_EVERY,
    source_epsg: int | None = None,
    keep_geojson: bool = False,
) -> int:
    """Build a standalone contour ``.osm`` from ``dem_src``; return the node count.

    Thin wrapper around :func:`iter_contour_osm` + :func:`topovert.osm.assemble_osm`
    (the pipeline instead merges the contour stream with the vector stream under a
    shared id allocator).
    """
    ids = vector._IdAllocator()
    return osm.assemble_osm(
        bounds, dst,
        [iter_contour_osm(
            dem_src, ids, dst.parent, interval=interval, major_every=major_every,
            source_epsg=source_epsg, keep_geojson=keep_geojson,
        )],
        keep_scratch=keep_geojson,
    )
