"""Thin wrappers over the GDAL command-line utilities.

We shell out to the GDAL CLI (``gdalbuildvrt``, ``gdalwarp``, ``gdal_translate``,
``gdalinfo``) rather than importing ``osgeo.gdal`` — the CLI is far less painful
to install across Linux/macOS/Windows and keeps topovert tolerant of GDAL
version drift. Command construction is split into pure ``*_cmd`` builders (unit
tested by asserting on argv) from the side-effecting ``run`` execution.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path

from . import TopovertError
from .hgt import Tile

log = logging.getLogger(__name__)

# GDAL utilities topovert depends on; checked together in :func:`check_available`.
# ogr2ogr/ogrinfo are needed for the swissTLM3D vector path (ship with gdal-bin).
REQUIRED_TOOLS = ("gdalbuildvrt", "gdalwarp", "gdal_translate", "gdalinfo", "ogr2ogr")

# SRTM "no data" sentinel (big-endian int16). mkgmap reads this as a void sample.
HGT_VOID = -32768


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    log.debug("run: %s", " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise TopovertError(
            f"`{cmd[0]}` failed (exit {proc.returncode}):\n{proc.stderr.strip()}"
        )
    return proc


def check_available(*, need_hgt: bool = True) -> None:
    """Raise if any required GDAL CLI tool is missing.

    ``need_hgt`` additionally requires the SRTMHGT driver (only the DEM path needs
    it; a vector-only ``--tlm`` build does not).
    """
    missing = [t for t in REQUIRED_TOOLS if shutil.which(t) is None]
    if missing:
        raise TopovertError(
            "GDAL command-line tools not found on PATH: "
            + ", ".join(missing)
            + ".\nInstall GDAL (e.g. `apt install gdal-bin`, `brew install gdal`)."
        )
    if need_hgt and "SRTMHGT" not in _run(["gdalinfo", "--formats"]).stdout:
        raise TopovertError(
            "this GDAL build lacks the SRTMHGT driver, which topovert needs to "
            "write .hgt tiles. Install a full GDAL build."
        )


# --- command builders (pure) ------------------------------------------------


def build_vrt_cmd(tifs: list[Path], vrt_path: Path) -> list[str]:
    return ["gdalbuildvrt", "-overwrite", str(vrt_path), *(str(t) for t in tifs)]


def warp_tile_cmd(
    src: Path,
    dst: Path,
    extent: tuple[float, float, float, float],
    samples: int,
    *,
    source_epsg: int | None,
    resampling: str,
) -> list[str]:
    """gdalwarp reprojecting ``src`` into one point-registered tile grid.

    ``-te``/``-ts`` pin the exact target extent and size so the output is a valid
    SRTM grid (see :meth:`topovert.hgt.Tile.warp_extent`); ``-t_srs EPSG:4326``
    is the WGS84 that Garmin/mkgmap expect. ``source_epsg=None`` omits ``-s_srs``
    so gdalwarp reads the source's own embedded CRS (the usual case); pass an int
    only to override a source that lacks CRS metadata.
    """
    min_lon, min_lat, max_lon, max_lat = extent
    cmd = ["gdalwarp", "-overwrite"]
    if source_epsg is not None:
        cmd += ["-s_srs", f"EPSG:{source_epsg}"]
    cmd += [
        "-t_srs", "EPSG:4326",
        "-te", f"{min_lon:.10f}", f"{min_lat:.10f}", f"{max_lon:.10f}", f"{max_lat:.10f}",
        "-ts", str(samples), str(samples),
        "-r", resampling,
        # Pixels with no source coverage become the SRTM void value, not a fake
        # sea-level 0 — matters for tile areas outside the supplied DEM footprint.
        "-dstnodata", str(HGT_VOID),
        "-of", "GTiff",
        str(src),
        str(dst),
    ]
    return cmd


def translate_hgt_cmd(src_tif: Path, dst_hgt: Path) -> list[str]:
    return ["gdal_translate", "-q", "-of", "SRTMHGT", str(src_tif), str(dst_hgt)]


# --- operations (side-effecting) --------------------------------------------


def build_vrt(tifs: list[Path], vrt_path: Path) -> Path:
    """Build a virtual mosaic over ``tifs`` (no pixel copy)."""
    if not tifs:
        raise TopovertError("no input GeoTIFFs to mosaic")
    _run(build_vrt_cmd(tifs, vrt_path))
    return vrt_path


def wgs84_bounds(src: Path) -> tuple[float, float, float, float]:
    """WGS84 ``(min_lon, min_lat, max_lon, max_lat)`` extent of ``src``.

    Uses ``gdalinfo -json``'s ``wgs84Extent``, which is computed regardless of
    the source CRS, so no reprojection step is needed just to learn the bounds.
    """
    info = json.loads(_run(["gdalinfo", "-json", str(src)]).stdout)
    extent = info.get("wgs84Extent")
    if not extent:
        raise TopovertError(
            f"could not determine WGS84 extent of {src} — does it have a CRS?"
        )
    coords = [pt for ring in extent["coordinates"] for pt in ring]
    lons = [c[0] for c in coords]
    lats = [c[1] for c in coords]
    return (min(lons), min(lats), max(lons), max(lats))


def make_hgt_tile(
    src: Path,
    tile: Tile,
    samples: int,
    dst_dir: Path,
    *,
    source_epsg: int | None,
    resampling: str,
) -> Path:
    """Warp + convert ``src`` into one ``dst_dir/<tile>.hgt`` file."""
    tmp_tif = dst_dir / f"{tile.name}.tmp.tif"
    hgt_path = dst_dir / tile.name
    try:
        _run(
            warp_tile_cmd(
                src, tmp_tif, tile.warp_extent(samples), samples,
                source_epsg=source_epsg, resampling=resampling,
            )
        )
        _run(translate_hgt_cmd(tmp_tif, hgt_path))
    finally:
        tmp_tif.unlink(missing_ok=True)
    return hgt_path
