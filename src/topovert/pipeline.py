"""End-to-end orchestration: swissALTI3D GeoTIFFs -> hill-shaded Garmin .IMG."""

from __future__ import annotations

import logging
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import TopovertError
from . import gdal_tools, jars, mkgmap, osm
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
    dem_dir: Path,
    out_path: Path,
    *,
    resolution: str = DEFAULT_RESOLUTION,
    resampling: str = "bilinear",
    source_epsg: int = DEFAULT_SOURCE_EPSG,
    map_name: str = "topovert",
    keep_intermediate: bool = False,
) -> BuildResult:
    """Convert a directory of DEM GeoTIFFs into a single hill-shaded ``.IMG``.

    Steps: preflight -> mosaic (VRT) -> compute WGS84 bounds -> per-tile warp +
    SRTMHGT conversion -> minimal bounds OSM -> mkgmap ``--dem`` -> copy result.
    """
    if resolution not in DEM_RESOLUTIONS:
        raise TopovertError(
            f"unknown resolution {resolution!r}; choose from {sorted(DEM_RESOLUTIONS)}"
        )
    samples = DEM_RESOLUTIONS[resolution]

    # Preflight: fail fast before touching the (slow) toolchain.
    gdal_tools.check_available()
    tifs = _find_geotiffs(dem_dir)
    java = jars.find_java()

    out_path = out_path.resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    workdir = Path(tempfile.mkdtemp(prefix="topovert-"))
    log.debug("workdir: %s", workdir)
    try:
        vrt = gdal_tools.build_vrt(tifs, workdir / "mosaic.vrt")
        bounds = gdal_tools.wgs84_bounds(vrt)
        log.info("input covers WGS84 bounds %s", tuple(round(b, 5) for b in bounds))

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

        bounds_osm = osm.write_bounds_osm(bounds, workdir / "bounds.osm", name=map_name)

        jar = jars.ensure_mkgmap()
        img = mkgmap.build_img(
            java, jar, bounds_osm, hgt_dir, workdir / "out", map_name=map_name
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
