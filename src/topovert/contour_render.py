"""Render contour lines for a small patch to a standalone SVG.

This is a *check-it-with-your-eyes* utility: building a Garmin ``.IMG`` and
loading it on a device (or in QMapShack) is the only true test of how contours
look, but it's slow and awkward to iterate on. An SVG of the exact line
geometry + elevation labels that the pipeline feeds mkgmap lets you (or a tool)
spot two common problems quickly:

* **bad geometry** — long straight lines shooting across the patch (usually DEM
  voids being contoured; :func:`topovert.gdal_tools.band_nodata` + ``-snodata``
  is the fix), or otherwise implausible spaghetti.
* **wrong elevations** — the labels here are the ground-truth *metres* read from
  the DEM, so they're what a correctly-configured device should show. (The
  on-device feet/metres bug lives in the .IMG encoding, not here — see the
  ``conv:m=>ft`` note in ``styles/topovert/lines``.)

:func:`features_to_svg` is pure (stdlib only) and unit-tested; :func:`render_patch`
shells out to the same ``gdal_contour`` -> ``ogr2ogr`` steps as the build.
"""

from __future__ import annotations

import json
import logging
import math
import shutil
import tempfile
from collections.abc import Iterable, Iterator
from pathlib import Path
from xml.sax.saxutils import escape

from . import TopovertError, contour, gdal_tools, vector

log = logging.getLogger(__name__)

# Brown palette mirroring topovert_typ.txt (index lines darker + bolder).
_MINOR_COLOR = "#b08050"
_MAJOR_COLOR = "#905a20"
_MINOR_WIDTH = 0.6
_MAJOR_WIDTH = 1.5
_LABEL_COLOR = "#4a2f10"
_LABEL_SIZE = 9
# Don't stamp two labels closer than this (px); keeps dense patches readable.
_LABEL_MIN_GAP = 60.0


def _iter_lines(geom: dict) -> Iterator[list]:
    """Yield each coordinate list of a (Multi)LineString geometry."""
    gtype = geom.get("type")
    coords = geom.get("coordinates")
    if not coords:
        return
    if gtype == "LineString":
        yield coords
    elif gtype == "MultiLineString":
        yield from coords


def _is_major(ele: float, *, interval: int, major_every: int) -> bool:
    return (
        contour.contour_tags(ele, interval=interval, major_every=major_every)[
            "contour_ext"
        ]
        == "elevation_major"
    )


def features_to_svg(
    features: Iterable[dict],
    *,
    interval: int = contour.DEFAULT_INTERVAL,
    major_every: int = contour.MAJOR_EVERY,
    width: int = 1000,
    padding: int = 16,
    bbox: tuple[float, float, float, float] | None = None,
) -> str:
    """Render contour ``features`` (GeoJSON dicts, WGS84) to an SVG string.

    Minor lines are drawn thin/light and index (major) lines bold/dark, matching
    the on-device palette; every index line gets its metre elevation labelled
    (de-cluttered so labels don't pile up). The viewport is the data extent, or
    ``bbox`` (``min_lon, min_lat, max_lon, max_lat``) when given. ``width`` is the
    image width in px; height follows from the patch aspect (latitude-corrected).
    """
    feats = [f for f in features if f]
    if bbox is not None:
        min_lon, min_lat, max_lon, max_lat = bbox
    else:
        lons: list[float] = []
        lats: list[float] = []
        for f in feats:
            for line in _iter_lines(f.get("geometry") or {}):
                for pt in line:
                    lons.append(pt[0])
                    lats.append(pt[1])
        if not lons:
            raise TopovertError("no contour line geometry to render")
        min_lon, max_lon = min(lons), max(lons)
        min_lat, max_lat = min(lats), max(lats)

    lon_span = (max_lon - min_lon) or 1e-9
    lat_span = (max_lat - min_lat) or 1e-9
    # Equirectangular with a cos(lat) correction so the patch isn't stretched.
    cos_lat = max(math.cos(math.radians((min_lat + max_lat) / 2)), 1e-6)
    inner_w = max(width - 2 * padding, 1)
    inner_h = max(inner_w * (lat_span / (lon_span * cos_lat)), 1)
    height = inner_h + 2 * padding

    def px(lon: float, lat: float) -> tuple[float, float]:
        x = padding + (lon - min_lon) / lon_span * inner_w
        y = padding + (max_lat - lat) / lat_span * inner_h  # flip: north is up
        return (round(x, 1), round(y, 1))

    minor_paths: list[str] = []
    major_paths: list[str] = []
    labels: list[str] = []
    placed: list[tuple[float, float]] = []

    # Draw minors first, majors (and labels) on top.
    for f in feats:
        props = f.get("properties") or {}
        ele = props.get(contour._ELEV_ATTR)
        if ele is None:
            continue
        ele = float(ele)
        major = _is_major(ele, interval=interval, major_every=major_every)
        for line in _iter_lines(f.get("geometry") or {}):
            pts = [px(p[0], p[1]) for p in line]
            if len(pts) < 2:
                continue
            d = " ".join(f"{x},{y}" for x, y in pts)
            poly = (
                f'<polyline points="{d}" fill="none" '
                f'stroke="{_MAJOR_COLOR if major else _MINOR_COLOR}" '
                f'stroke-width="{_MAJOR_WIDTH if major else _MINOR_WIDTH}"/>'
            )
            (major_paths if major else minor_paths).append(poly)
            if major:
                lx, ly = pts[len(pts) // 2]
                if all(
                    math.hypot(lx - ox, ly - oy) >= _LABEL_MIN_GAP
                    for ox, oy in placed
                ):
                    placed.append((lx, ly))
                    labels.append(
                        f'<text x="{lx}" y="{ly}" font-size="{_LABEL_SIZE}" '
                        f'fill="{_LABEL_COLOR}" stroke="#ffffff" stroke-width="2.5" '
                        f'paint-order="stroke" text-anchor="middle" '
                        f'font-family="sans-serif">{escape(str(int(round(ele))))}</text>'
                    )

    w = round(inner_w + 2 * padding, 1)
    h = round(height, 1)
    header = (
        f"contours every {interval} m, index every {interval * major_every} m "
        f"(labels = metres)"
    )
    body = "\n".join(
        [
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
            f'viewBox="0 0 {w} {h}">',
            f'<rect width="{w}" height="{h}" fill="#fbf8f2"/>',
            f'<g>{"".join(minor_paths)}</g>',
            f'<g>{"".join(major_paths)}</g>',
            f'<g>{"".join(labels)}</g>',
            f'<text x="{padding}" y="{round(h - 5, 1)}" font-size="10" '
            f'fill="#806040" font-family="sans-serif">{escape(header)}</text>',
            "</svg>",
        ]
    )
    return body


def _clip_cmd(
    src: Path, dst: Path, bbox: tuple[float, float, float, float], *,
    source_epsg: int | None = None,
) -> list[str]:
    """argv for ``gdalwarp`` cropping ``src`` to a WGS84 ``bbox`` (CRS preserved).

    ``-te_srs EPSG:4326`` lets the bbox be WGS84 while the output keeps the
    source CRS, so the existing ``gdal_contour`` -> ``ogr2ogr`` reprojection path
    is unchanged. ``source_epsg`` adds ``-s_srs`` only for sources lacking a CRS.
    """
    min_lon, min_lat, max_lon, max_lat = bbox
    cmd = ["gdalwarp", "-overwrite"]
    if source_epsg is not None:
        cmd += ["-s_srs", f"EPSG:{source_epsg}"]
    cmd += [
        "-te_srs", "EPSG:4326",
        "-te", f"{min_lon:.10f}", f"{min_lat:.10f}",
        f"{max_lon:.10f}", f"{max_lat:.10f}",
        "-of", "GTiff", str(src), str(dst),
    ]
    return cmd


def _load_geojsonl(path: Path) -> list[dict]:
    """Parse a GeoJSONSeq (one feature per line, optional RS prefix) file."""
    feats: list[dict] = []
    with path.open(encoding="utf-8") as fh:
        for raw in fh:
            line = raw.lstrip("\x1e").strip()
            if line:
                feats.append(json.loads(line))
    return feats


def render_patch(
    out: Path,
    *,
    dem_dir: Path | None = None,
    dem_area: str | None = None,
    from_geojson: Path | None = None,
    bbox: tuple[float, float, float, float] | None = None,
    interval: int = contour.DEFAULT_INTERVAL,
    major_every: int = contour.MAJOR_EVERY,
    source_epsg: int | None = None,
    width: int = 1000,
    keep_intermediate: bool = False,
) -> Path:
    """Render an SVG of the contours for a patch and write it to ``out``.

    The contour source is one of: ``from_geojson`` (an existing GeoJSONSeq, e.g.
    a ``--keep-intermediate`` ``contours.geojsonl`` — no GDAL needed), ``dem_dir``
    (a folder of GeoTIFFs), or ``dem_area`` (a named area / WGS84 bbox whose
    Copernicus tiles are auto-downloaded). With a DEM source, ``bbox`` crops it
    first so even a huge DEM renders a small patch fast. Returns ``out``.
    """
    out = out.resolve()
    out.parent.mkdir(parents=True, exist_ok=True)

    if from_geojson is not None:
        feats = _load_geojsonl(from_geojson)
        out.write_text(
            features_to_svg(
                feats, interval=interval, major_every=major_every,
                width=width, bbox=bbox,
            ),
            encoding="utf-8",
        )
        log.info("wrote %s (%d feature(s) from %s)", out, len(feats), from_geojson)
        return out

    if dem_dir is None and dem_area is None:
        raise TopovertError(
            "render-contours needs a source: --dem-dir, --dem-area or --from-geojson"
        )
    if dem_dir is not None and dem_area is not None:
        raise TopovertError("pass either --dem-dir or --dem-area, not both")

    gdal_tools.check_available(need_hgt=False, need_contour=True)
    if dem_area is not None:
        from . import dem_download
        dem_download.parse_area(dem_area)  # fail fast on a typo
        log.info("auto-downloading Copernicus GLO-30 DEM for area %r", dem_area)
        tifs = dem_download.download_area(dem_area)
    else:
        from .pipeline import _find_geotiffs
        tifs = _find_geotiffs(dem_dir)

    workdir = Path(tempfile.mkdtemp(prefix="topovert-render-", dir=str(out.parent)))
    try:
        src = gdal_tools.build_vrt(tifs, workdir / "mosaic.vrt")
        if bbox is not None:
            clipped = workdir / "patch.tif"
            gdal_tools._run(_clip_cmd(src, clipped, bbox, source_epsg=source_epsg))
            src = clipped
        gpkg = workdir / "contours.gpkg"
        snodata = gdal_tools.band_nodata(src)
        gdal_tools._run(
            contour.gdal_contour_cmd(src, gpkg, interval=interval, snodata=snodata)
        )
        geojson = workdir / "contours.geojsonl"
        gdal_tools._run(
            vector.ogr_geojson_cmd(
                gpkg, contour._CONTOUR_LAYER, geojson, source_epsg=source_epsg
            )
        )
        feats = _load_geojsonl(geojson)
        out.write_text(
            features_to_svg(
                feats, interval=interval, major_every=major_every,
                width=width, bbox=bbox,
            ),
            encoding="utf-8",
        )
        log.info("wrote %s (%d contour line feature(s))", out, len(feats))
    finally:
        if keep_intermediate:
            log.info("kept render intermediates in %s", workdir)
        else:
            shutil.rmtree(workdir, ignore_errors=True)
    return out
