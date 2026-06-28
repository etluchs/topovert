"""Auto-download a DEM for an area from the Copernicus GLO-30 open dataset.

This is the *partial* area auto-download (full swissALTI3D STAC selection is the
larger, deferred ``topovert-y8s``). Copernicus DEM GLO-30 is the right source for
country-scale relief/contours: it is already ~30 m (the target HGT grid), global,
free, ships as one GeoTIFF per 1x1 degree cell in **EPSG:4326**, and mirrors on
AWS Open Data under a fixed URL pattern — so all of Switzerland is just 18 tiles
(~730 MB) rather than the terabytes a full-resolution swissALTI3D pull would be.

Given a named area or a WGS84 bbox we enumerate the 1 degree tiles it covers
(reusing :mod:`topovert.hgt`'s tiling geometry), download the missing ones into a
per-user cache, and hand the directory back to the pipeline as if it were a local
``--dem-dir``. Tiles are cached across runs; ocean cells that have no Copernicus
tile (HTTP 404) are skipped with a warning. The tiles' EPSG:4326 CRS is read
straight from the files, so no ``--source-epsg`` is needed.
"""

from __future__ import annotations

import logging
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import TopovertError
from .hgt import Tile, tiles_for_bounds
from .jars import cache_dir

log = logging.getLogger(__name__)

# AWS Open Data mirror of Copernicus DEM GLO-30. Override for a mirror/air-gapped
# setup. Each tile lives at ``<base>/<name>/<name>.tif`` (see :func:`tile_url`).
COPERNICUS_GLO30_BASE = os.environ.get(
    "TOPOVERT_COPERNICUS_URL", "https://copernicus-dem-30m.s3.amazonaws.com"
)

# Named areas -> WGS84 bbox (min_lon, min_lat, max_lon, max_lat). The Switzerland
# box is padded a touch past the political border so edge tiles are included; the
# 1 degree tiling rounds out to the 18 cells covering the country (lon 5..10, lat
# 45..47). Add more areas here as needed.
NAMED_AREAS: dict[str, tuple[float, float, float, float]] = {
    "switzerland": (5.9, 45.8, 10.5, 47.9),
    "ch": (5.9, 45.8, 10.5, 47.9),
}


def _tile_basename(tile: Tile) -> str:
    """Copernicus product name for a 1 degree cell, e.g.
    ``Copernicus_DSM_COG_10_N47_00_E007_00_DEM`` for ``Tile(lat=47, lon=7)``."""
    ns = "N" if tile.lat >= 0 else "S"
    ew = "E" if tile.lon >= 0 else "W"
    return (
        f"Copernicus_DSM_COG_10_{ns}{abs(tile.lat):02d}_00_"
        f"{ew}{abs(tile.lon):03d}_00_DEM"
    )


def tile_url(tile: Tile) -> str:
    """Full download URL of the Copernicus GLO-30 GeoTIFF for ``tile``."""
    name = _tile_basename(tile)
    return f"{COPERNICUS_GLO30_BASE}/{name}/{name}.tif"


def parse_area(area: str) -> tuple[float, float, float, float]:
    """Resolve an ``--dem-area`` value to a WGS84 ``(min_lon, min_lat, max_lon,
    max_lat)`` bbox.

    Accepts a named area (see :data:`NAMED_AREAS`, case-insensitive) or a
    ``min_lon,min_lat,max_lon,max_lat`` bbox string. Raises
    :class:`TopovertError` on anything else so a typo fails fast (before the slow
    download).
    """
    key = area.strip().lower()
    if key in NAMED_AREAS:
        return NAMED_AREAS[key]

    parts = [p.strip() for p in area.split(",")]
    if len(parts) == 4:
        try:
            min_lon, min_lat, max_lon, max_lat = (float(p) for p in parts)
        except ValueError:
            pass
        else:
            if max_lon <= min_lon or max_lat <= min_lat:
                raise TopovertError(
                    f"degenerate --dem-area bbox {area!r}: need "
                    "min_lon,min_lat,max_lon,max_lat with max > min"
                )
            return (min_lon, min_lat, max_lon, max_lat)

    raise TopovertError(
        f"unknown --dem-area {area!r}; use a named area "
        f"({', '.join(sorted(NAMED_AREAS))}) or a "
        "'min_lon,min_lat,max_lon,max_lat' WGS84 bbox"
    )


def tiles_for_area(area: str) -> list[Tile]:
    """The 1 degree Copernicus tiles covering ``area`` (named or bbox)."""
    return tiles_for_bounds(*parse_area(area))


# Network is the flakiest part of a country-scale pull (~730 MB over many tiles),
# so transient failures — incomplete reads, resets, 5xx — are retried with
# exponential backoff before giving up. A 404 is definitive (no such tile) and is
# not retried. Mirrors the retry convention used elsewhere for git/network ops.
_RETRIES = 4
_BACKOFF_BASE_S = 2


def _fetch(url: str, dst: Path) -> bool:
    """Download ``url`` to ``dst`` atomically, retrying transient failures.

    Returns ``False`` if the tile does not exist (HTTP 404 — an ocean/void cell),
    raises :class:`TopovertError` if it still fails after :data:`_RETRIES`
    attempts. The partial download lands in a ``.part`` sidecar and is renamed
    into place only on a complete transfer, so an interrupted run never leaves a
    truncated tile that a later run would treat as cached.
    """
    tmp = dst.with_suffix(dst.suffix + ".part")
    last = ""
    for attempt in range(1, _RETRIES + 1):
        try:
            urllib.request.urlretrieve(url, tmp)
        except urllib.error.HTTPError as exc:
            tmp.unlink(missing_ok=True)
            if exc.code == 404:
                return False
            last = f"HTTP {exc.code}"
        except OSError as exc:
            # ContentTooShortError (incomplete read), timeouts, connection
            # resets — all OSError subclasses, all worth a retry.
            tmp.unlink(missing_ok=True)
            last = str(exc)
        else:
            tmp.replace(dst)
            return True
        if attempt < _RETRIES:
            delay = _BACKOFF_BASE_S * 2 ** (attempt - 1)
            log.warning(
                "download of %s failed (%s); retrying in %ds [%d/%d]",
                url, last, delay, attempt, _RETRIES,
            )
            time.sleep(delay)
    raise TopovertError(
        f"failed to download {url} after {_RETRIES} attempts: {last}\n"
        "Set TOPOVERT_COPERNICUS_URL to a reachable mirror if needed."
    )


def download_area(area: str, dst_dir: Path | None = None) -> list[Path]:
    """Download the Copernicus GLO-30 tiles covering ``area`` and return their
    file paths (the GeoTIFFs to feed the build).

    ``area`` is a named area or a WGS84 bbox (see :func:`parse_area`). Tiles are
    cached in ``dst_dir`` (default: ``<cache>/copernicus-dem-30m``); already
    present tiles are reused, and ocean cells with no tile are skipped. Returns
    only the tiles covering ``area`` — NOT every file in the (shared) cache — so
    a small bbox build never accidentally pulls in tiles a previous larger
    download left behind. Raises if the area resolves to no available tile.
    """
    tiles = tiles_for_area(area)
    dst = dst_dir or (cache_dir() / "copernicus-dem-30m")
    dst.mkdir(parents=True, exist_ok=True)

    paths: list[Path] = []
    have, fetched, missing = 0, 0, 0
    for tile in tiles:
        local = dst / f"{_tile_basename(tile)}.tif"
        if local.exists() and local.stat().st_size > 0:
            have += 1
            paths.append(local)
            continue
        url = tile_url(tile)
        log.info("downloading DEM tile %s ...", _tile_basename(tile))
        if _fetch(url, local):
            fetched += 1
            paths.append(local)
        else:
            missing += 1
            log.debug("no Copernicus tile for %s (void/ocean cell)", tile.name)

    log.info(
        "DEM for %r: %d tile(s) ready (%d downloaded, %d cached, %d void) in %s",
        area, len(paths), fetched, have, missing, dst,
    )
    if not paths:
        raise TopovertError(
            f"no Copernicus DEM tiles available for --dem-area {area!r} "
            f"(checked {len(tiles)} cell(s)); is the area over land?"
        )
    return paths
