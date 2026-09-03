"""End-to-end orchestration: Swisstopo data -> a Garmin .IMG or Wahoo map tiles.

The pipeline is format-neutral until the assembled ``map.osm``: DEM handling,
swissTLM3D tagging and contour extraction produce the same OSM either way, and
only the last step differs — splitter+mkgmap for Garmin (:mod:`topovert.mkgmap`),
osmosis+mapsforge-map-writer for Wahoo (:mod:`topovert.wahoo`).
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import TopovertError
from . import (
    contour, dem_download, gdal_tools, jars, mkgmap, osm, splitter, tlm_download,
    vector, wahoo,
)
from .hgt import DEFAULT_RESOLUTION, DEM_RESOLUTIONS, tiles_for_bounds

log = logging.getLogger(__name__)

_TIF_GLOBS = ("*.tif", "*.tiff", "*.TIF", "*.TIFF")

# mkgmap reads DEM samples in a border *beyond* the map bounds, so data sitting
# near a 1-degree tile edge makes it touch the neighbouring tile. We expand the
# bounds by this margin before choosing tiles so those neighbours get generated
# (as voids where no source data exists), avoiding mkgmap's "file not found,
# height 0" edge artifact. ~0.05 deg (~5.5 km) comfortably exceeds mkgmap's border.
DEM_TILE_MARGIN_DEG = 0.05

# Above this many OSM nodes a single mkgmap tile is impractical, so the OSM is
# first tiled with splitter. Whole-canton/country swissTLM3D extents far exceed it.
SPLIT_NODE_THRESHOLD = 2_000_000

# Output formats: a single Garmin ``.IMG`` file, or a directory of Wahoo
# (mapsforge) zoom-8 map tiles. See :mod:`topovert.wahoo` for what the second
# one cannot do (no hillshade, no transparent overlay, device-side styling).
OUTPUT_FORMATS = ("garmin", "wahoo")


@dataclass
class BuildResult:
    out_path: Path
    tiles: list[str]
    bounds: tuple[float, float, float, float]
    # Wahoo builds only: the "<x>/<y>" zoom-8 tiles actually written.
    map_tiles: list[str] = field(default_factory=list)


def _find_geotiffs(dem_dir: Path) -> list[Path]:
    if not dem_dir.is_dir():
        raise TopovertError(f"--dem-dir is not a directory: {dem_dir}")
    tifs = sorted({p for g in _TIF_GLOBS for p in dem_dir.glob(g)})
    if not tifs:
        raise TopovertError(f"no GeoTIFF (*.tif) files found in {dem_dir}")
    return tifs


def build(
    dem_dir: Path | None,
    out_path: Path,
    *,
    dem_area: str | None = None,
    resolution: str = DEFAULT_RESOLUTION,
    resampling: str = "bilinear",
    source_epsg: int | None = None,
    map_name: str = "topovert",
    output_format: str = "garmin",
    tlm_path: Path | None = None,
    tlm_release: str | None = None,
    tlm_layers: list[str] | None = None,
    contours: bool = False,
    contour_interval: int = contour.DEFAULT_INTERVAL,
    hillshade: bool = True,
    max_nodes: int = splitter.DEFAULT_MAX_NODES,
    max_heap: str | None = None,
    transparent: bool = True,
    work_dir: Path | None = None,
    keep_intermediate: bool = False,
) -> BuildResult:
    """Build a Garmin ``.IMG`` or Wahoo map tiles from a DEM and/or swissTLM3D vectors.

    The DEM source is either a local ``dem_dir`` of GeoTIFFs or ``dem_area`` (a
    named area or WGS84 bbox) whose Copernicus GLO-30 tiles are auto-downloaded
    into a cache and used in its place — pass at most one. The vector source is
    likewise either a local ``tlm_path`` (a swissTLM3D ``.gdb``) or
    ``tlm_release`` (``"latest"`` or a release id), which downloads the national
    swissTLM3D GeoDatabase from swisstopo's STAC API into the cache — again at
    most one. At least one of a DEM source / a vector source is required.
    Steps: preflight ->
    bounds (from the DEM mosaic, else from the TLM extent) -> optional per-tile
    HGT -> OSM (bounds-only, swissTLM3D vector features, and/or DEM contours) ->
    splitter when the OSM is large -> mkgmap (``--dem`` when a DEM is present) ->
    copy result. ``contours`` adds elevation contour lines derived from the DEM
    (so it requires ``dem_dir``), spaced every ``contour_interval`` metres.
    ``hillshade`` (default) embeds the DEM as a height grid for shaded relief;
    set it ``False`` to skip the heavy DEM embed while still using the DEM for
    bounds and ``--contours`` — a much smaller contour-only map.

    ``output_format`` picks the last step: ``"garmin"`` writes the single
    ``.IMG`` at ``out_path``; ``"wahoo"`` treats ``out_path`` as a *directory*
    and fills it with zoom-8 ``<x>/<y>.map.lzma`` tiles for an ELEMNT/BOLT/ROAM.
    Wahoo maps cannot carry a hillshade (mapsforge holds no elevation grid), so
    that format needs ``contours`` and/or a vector source.
    """
    if resolution not in DEM_RESOLUTIONS:
        raise TopovertError(
            f"unknown resolution {resolution!r}; choose from {sorted(DEM_RESOLUTIONS)}"
        )
    samples = DEM_RESOLUTIONS[resolution]

    if max_heap is not None and not re.fullmatch(r"\d+[kKmMgG]?", max_heap):
        raise TopovertError(
            f"invalid --max-heap {max_heap!r}; use a JVM -Xmx size like "
            "'8g', '512m' or a plain byte count"
        )

    if output_format not in OUTPUT_FORMATS:
        raise TopovertError(
            f"unknown output format {output_format!r}; choose from "
            f"{sorted(OUTPUT_FORMATS)}"
        )
    wahoo_out = output_format == "wahoo"

    if dem_dir is not None and dem_area is not None:
        raise TopovertError(
            "pass either --dem-dir (local tiles) or --dem-area (auto-download), "
            "not both"
        )
    if tlm_path is not None and tlm_release is not None:
        raise TopovertError(
            "pass either --tlm (local .gdb) or --tlm-release (auto-download), "
            "not both"
        )
    have_dem = dem_dir is not None or dem_area is not None
    have_tlm = tlm_path is not None or tlm_release is not None
    if not have_dem and not have_tlm:
        raise TopovertError(
            "nothing to build: pass --dem-dir, --dem-area, --tlm and/or --tlm-release"
        )
    if wahoo_out and not contours and not have_tlm:
        raise TopovertError(
            "--format wahoo cannot embed a hillshade DEM (a mapsforge map holds "
            "no elevation grid), so a DEM on its own builds an empty map: add "
            "--contours and/or a swissTLM3D source"
        )
    if contours and not have_dem:
        raise TopovertError(
            "--contours needs a DEM (--dem-dir or --dem-area): contours are "
            "derived from the elevation data"
        )
    if have_dem and not hillshade and not contours and not have_tlm:
        raise TopovertError(
            "--no-hillshade with only a DEM leaves an empty map: add --contours "
            "and/or --tlm, or drop --no-hillshade"
        )
    if wahoo_out and hillshade:
        if have_dem:
            log.info(
                "Wahoo devices do not render an embedded DEM; skipping the "
                "hillshade (the contour lines carry the terrain)"
            )
        hillshade = False

    # Validate the area now (cheap) so a typo fails before the slow download.
    if dem_area is not None:
        dem_download.parse_area(dem_area)

    # Preflight: fail fast before touching the (slow) toolchain or the DEM
    # download. The SRTMHGT driver is only needed to write HGT tiles, i.e. when
    # actually hillshading.
    embed_dem = have_dem and hillshade
    gdal_tools.check_available(need_hgt=embed_dem, need_contour=contours)
    java = jars.find_java()
    # Resolve the DEM tiles: either the explicit tiles covering --dem-area
    # (auto-downloaded into a shared cache) or every GeoTIFF in --dem-dir. Using
    # the area's exact tile list — not a glob of the cache dir — keeps a small
    # bbox build from sweeping in tiles a previous larger download cached.
    if dem_area is not None:
        log.info("auto-downloading Copernicus GLO-30 DEM for area %r", dem_area)
        tifs = dem_download.download_area(dem_area)
    elif dem_dir is not None:
        tifs = _find_geotiffs(dem_dir)
    else:
        tifs = []
    layers = tlm_layers or list(vector.DEFAULT_TLM_LAYERS)
    # swissTLM3D is published as one national FileGDB per release, so the
    # download is whole-country regardless of the DEM area (see tlm_download).
    if tlm_release is not None:
        log.info("auto-downloading swissTLM3D release %r", tlm_release)
        tlm_path = tlm_download.download_release(tlm_release)
    if tlm_path is not None:
        tlm_path = tlm_path.resolve()
        if not tlm_path.exists():
            raise TopovertError(f"--tlm source not found: {tlm_path}")

    out_path = out_path.resolve()
    # Garmin builds write one file; Wahoo builds fill a directory of tiles.
    if wahoo_out:
        out_path.mkdir(parents=True, exist_ok=True)
    else:
        out_path.parent.mkdir(parents=True, exist_ok=True)

    # Whole-country intermediates reach tens of GB, so default the workdir to the
    # output filesystem (next to --out) rather than the system temp, which is
    # often a small RAM-backed tmpfs. Override with --work-dir.
    if work_dir is not None:
        work_dir.mkdir(parents=True, exist_ok=True)
    scratch_base = work_dir or (out_path if wahoo_out else out_path.parent)
    workdir = Path(tempfile.mkdtemp(prefix="topovert-", dir=str(scratch_base)))
    log.debug("workdir: %s", workdir)
    try:
        # --- bounds, and the optional DEM (HGT tiles) -----------------------
        tiles = []
        hgt_dir: Path | None = None
        if have_dem:
            # The mosaic is always built — it gives the bounds and feeds
            # gdal_contour — but the HGT tiles + --dem embed only happen when
            # hillshading (the heavy part: a contour-only map skips them).
            vrt = gdal_tools.build_vrt(tifs, workdir / "mosaic.vrt")
            bounds = gdal_tools.wgs84_bounds(vrt)
            log.info("DEM covers WGS84 bounds %s", tuple(round(b, 5) for b in bounds))
            if embed_dem:
                min_lon, min_lat, max_lon, max_lat = bounds
                m = DEM_TILE_MARGIN_DEG
                tiles = tiles_for_bounds(min_lon - m, min_lat - m, max_lon + m, max_lat + m)
                log.info("generating %d HGT tile(s): %s", len(tiles), [t.name for t in tiles])
                hgt_dir = workdir / "hgt"
                hgt_dir.mkdir()
                for tile in tiles:
                    gdal_tools.make_hgt_tile(
                        vrt, tile, samples, hgt_dir,
                        source_epsg=source_epsg, resampling=resampling,
                    )
            else:
                log.info("skipping hillshade DEM embed (--no-hillshade)")
        else:
            bounds = vector.tlm_bounds(tlm_path, layers, source_epsg=source_epsg)
            log.info("swissTLM3D covers WGS84 bounds %s", tuple(round(b, 5) for b in bounds))

        # --- OSM (vector features and/or DEM contours, else minimal bounds) -
        # Both feeds share one id allocator so ids stay unique/ascending across
        # them; assemble_osm merges the streams into one osmosis-ordered file.
        ids = vector._IdAllocator()
        streams = []
        if tlm_path is not None:
            log.info("converting swissTLM3D features from %s: %s", tlm_path, layers)
            streams.append(vector.iter_features_osm(
                tlm_path, layers, ids, scratch=workdir,
                source_epsg=source_epsg, keep_geojson=keep_intermediate,
            ))
        if contours:
            log.info("generating contour lines from the DEM (every %d m)", contour_interval)
            streams.append(contour.iter_contour_osm(
                vrt, ids, workdir, interval=contour_interval,
                source_epsg=source_epsg, keep_geojson=keep_intermediate,
            ))
        if streams:
            map_osm = workdir / "map.osm"
            n_nodes = osm.assemble_osm(
                bounds, map_osm, streams, keep_scratch=keep_intermediate
            )
        else:
            map_osm = osm.write_bounds_osm(bounds, workdir / "bounds.osm", name=map_name)
            n_nodes = 0

        # --- compile: the one format-specific step --------------------------
        if wahoo_out:
            # osmosis + the mapsforge map-writer plugin, once per zoom-8 tile.
            map_tiles = wahoo.build_tiles(
                java, jars.ensure_osmosis(), jars.ensure_mapwriter(),
                map_osm, bounds, out_path,
                workdir=workdir,
                # map-writer parallelises within a tile; leave a core free.
                threads=max((os.cpu_count() or 1) - 1, 1),
                # Large extents would not fit a tile's data in RAM.
                hd=n_nodes > SPLIT_NODE_THRESHOLD,
                max_heap=max_heap,
                keep_intermediate=keep_intermediate,
            )
            log.info("wrote %d tile(s) under %s", len(map_tiles), out_path)
            return BuildResult(
                out_path=out_path, tiles=[], bounds=bounds,
                map_tiles=[f"{t.x}/{t.y}" for t in map_tiles],
            )

        jar = jars.ensure_mkgmap()
        out_dir = workdir / "out"
        if n_nodes > SPLIT_NODE_THRESHOLD:
            log.info("%d node(s) exceeds split threshold; tiling with splitter", n_nodes)
            inputs = splitter.split(
                java, jars.ensure_splitter(), map_osm, workdir / "split",
                max_nodes=max_nodes, max_heap=max_heap,
            )
            img = mkgmap.build_img(
                java, jar, inputs, out_dir,
                map_name=map_name, hgt_dir=hgt_dir, mapname=None,
                max_heap=max_heap, transparent=transparent,
            )
        else:
            img = mkgmap.build_img(
                java, jar, [map_osm], out_dir,
                map_name=map_name, hgt_dir=hgt_dir,
                max_heap=max_heap, transparent=transparent,
            )

        shutil.copyfile(img, out_path)
        log.info("wrote %s", out_path)
        return BuildResult(
            out_path=out_path, tiles=[t.name for t in tiles], bounds=bounds
        )
    finally:
        if keep_intermediate:
            log.info("kept intermediates in %s", workdir)
        else:
            shutil.rmtree(workdir, ignore_errors=True)
