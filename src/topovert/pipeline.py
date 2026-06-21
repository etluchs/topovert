"""End-to-end orchestration: swissALTI3D GeoTIFFs -> hill-shaded Garmin .IMG."""

from __future__ import annotations

import logging
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import TopovertError
from . import gdal_tools, jars, mkgmap, osm, splitter, vector
from .hgt import DEFAULT_RESOLUTION, DEM_RESOLUTIONS, tiles_for_bounds

log = logging.getLogger(__name__)

_TIF_GLOBS = ("*.tif", "*.tiff", "*.TIF", "*.TIFF")
DEFAULT_SOURCE_EPSG = 2056  # swissALTI3D / LV95

# mkgmap reads DEM samples in a border *beyond* the map bounds, so data sitting
# near a 1-degree tile edge makes it touch the neighbouring tile. We expand the
# bounds by this margin before choosing tiles so those neighbours get generated
# (as voids where no source data exists), avoiding mkgmap's "file not found,
# height 0" edge artifact. ~0.05 deg (~5.5 km) comfortably exceeds mkgmap's border.
DEM_TILE_MARGIN_DEG = 0.05

# Above this many OSM nodes a single mkgmap tile is impractical, so the OSM is
# first tiled with splitter. Whole-canton/country swissTLM3D extents far exceed it.
SPLIT_NODE_THRESHOLD = 2_000_000


@dataclass
class BuildResult:
    out_path: Path
    tiles: list[str]
    bounds: tuple[float, float, float, float]


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
    resolution: str = DEFAULT_RESOLUTION,
    resampling: str = "bilinear",
    source_epsg: int = DEFAULT_SOURCE_EPSG,
    map_name: str = "topovert",
    tlm_path: Path | None = None,
    tlm_layers: list[str] | None = None,
    max_nodes: int = splitter.DEFAULT_MAX_NODES,
    work_dir: Path | None = None,
    keep_intermediate: bool = False,
) -> BuildResult:
    """Build a Garmin ``.IMG`` from a DEM and/or swissTLM3D vectors.

    At least one of ``dem_dir`` / ``tlm_path`` is required. Steps: preflight ->
    bounds (from the DEM mosaic, else from the TLM extent) -> optional per-tile
    HGT -> OSM (bounds-only, or swissTLM3D vector features) -> splitter when the
    OSM is large -> mkgmap (``--dem`` when a DEM is present) -> copy result.
    """
    if resolution not in DEM_RESOLUTIONS:
        raise TopovertError(
            f"unknown resolution {resolution!r}; choose from {sorted(DEM_RESOLUTIONS)}"
        )
    samples = DEM_RESOLUTIONS[resolution]

    if dem_dir is None and tlm_path is None:
        raise TopovertError("nothing to build: pass --dem-dir and/or --tlm")

    # Preflight: fail fast before touching the (slow) toolchain.
    gdal_tools.check_available(need_hgt=dem_dir is not None)
    java = jars.find_java()
    tifs = _find_geotiffs(dem_dir) if dem_dir is not None else []
    layers = tlm_layers or list(vector.DEFAULT_TLM_LAYERS)
    if tlm_path is not None:
        tlm_path = tlm_path.resolve()
        if not tlm_path.exists():
            raise TopovertError(f"--tlm source not found: {tlm_path}")

    out_path = out_path.resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Whole-country intermediates reach tens of GB, so default the workdir to the
    # output filesystem (next to --out) rather than the system temp, which is
    # often a small RAM-backed tmpfs. Override with --work-dir.
    if work_dir is not None:
        work_dir.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix="topovert-", dir=str(work_dir or out_path.parent)))
    log.debug("workdir: %s", workdir)
    try:
        # --- bounds, and the optional DEM (HGT tiles) -----------------------
        tiles = []
        hgt_dir: Path | None = None
        if dem_dir is not None:
            vrt = gdal_tools.build_vrt(tifs, workdir / "mosaic.vrt")
            bounds = gdal_tools.wgs84_bounds(vrt)
            log.info("DEM covers WGS84 bounds %s", tuple(round(b, 5) for b in bounds))
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
            bounds = vector.tlm_bounds(tlm_path, layers, source_epsg=source_epsg)
            log.info("swissTLM3D covers WGS84 bounds %s", tuple(round(b, 5) for b in bounds))

        # --- OSM (vector features, else minimal bounds) ---------------------
        if tlm_path is not None:
            log.info("converting swissTLM3D features from %s: %s", tlm_path, layers)
            map_osm = workdir / "features.osm"
            n_nodes = vector.build_features_osm(
                tlm_path, layers, bounds, map_osm,
                source_epsg=source_epsg, keep_geojson=keep_intermediate,
            )
        else:
            map_osm = osm.write_bounds_osm(bounds, workdir / "bounds.osm", name=map_name)
            n_nodes = 0

        # --- compile: splitter (large) then mkgmap, or mkgmap directly ------
        jar = jars.ensure_mkgmap()
        out_dir = workdir / "out"
        if n_nodes > SPLIT_NODE_THRESHOLD:
            log.info("%d node(s) exceeds split threshold; tiling with splitter", n_nodes)
            inputs = splitter.split(
                java, jars.ensure_splitter(), map_osm, workdir / "split",
                max_nodes=max_nodes,
            )
            img = mkgmap.build_img(
                java, jar, inputs, out_dir,
                map_name=map_name, hgt_dir=hgt_dir, mapname=None,
            )
        else:
            img = mkgmap.build_img(
                java, jar, [map_osm], out_dir, map_name=map_name, hgt_dir=hgt_dir,
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
