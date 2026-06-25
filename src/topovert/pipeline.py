"""End-to-end orchestration: swissALTI3D GeoTIFFs -> hill-shaded Garmin .IMG."""

from __future__ import annotations

import logging
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import TopovertError
from . import contour, gdal_tools, jars, mkgmap, osm, splitter, vector
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
    source_epsg: int | None = None,
    map_name: str = "topovert",
    tlm_path: Path | None = None,
    tlm_layers: list[str] | None = None,
    contours: bool = False,
    contour_interval: int = contour.DEFAULT_INTERVAL,
    hillshade: bool = True,
    max_nodes: int = splitter.DEFAULT_MAX_NODES,
    work_dir: Path | None = None,
    keep_intermediate: bool = False,
) -> BuildResult:
    """Build a Garmin ``.IMG`` from a DEM and/or swissTLM3D vectors.

    At least one of ``dem_dir`` / ``tlm_path`` is required. Steps: preflight ->
    bounds (from the DEM mosaic, else from the TLM extent) -> optional per-tile
    HGT -> OSM (bounds-only, swissTLM3D vector features, and/or DEM contours) ->
    splitter when the OSM is large -> mkgmap (``--dem`` when a DEM is present) ->
    copy result. ``contours`` adds elevation contour lines derived from the DEM
    (so it requires ``dem_dir``), spaced every ``contour_interval`` metres.
    ``hillshade`` (default) embeds the DEM as a height grid for shaded relief;
    set it ``False`` to skip the heavy DEM embed while still using the DEM for
    bounds and ``--contours`` — a much smaller contour-only map.
    """
    if resolution not in DEM_RESOLUTIONS:
        raise TopovertError(
            f"unknown resolution {resolution!r}; choose from {sorted(DEM_RESOLUTIONS)}"
        )
    samples = DEM_RESOLUTIONS[resolution]

    if dem_dir is None and tlm_path is None:
        raise TopovertError("nothing to build: pass --dem-dir and/or --tlm")
    if contours and dem_dir is None:
        raise TopovertError(
            "--contours needs a DEM (--dem-dir): contours are derived from the "
            "elevation data"
        )
    if dem_dir is not None and not hillshade and not contours and tlm_path is None:
        raise TopovertError(
            "--no-hillshade with only --dem-dir leaves an empty map: add "
            "--contours and/or --tlm, or drop --no-hillshade"
        )

    # Preflight: fail fast before touching the (slow) toolchain. The SRTMHGT
    # driver is only needed to write HGT tiles, i.e. when actually hillshading.
    embed_dem = dem_dir is not None and hillshade
    gdal_tools.check_available(need_hgt=embed_dem, need_contour=contours)
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
