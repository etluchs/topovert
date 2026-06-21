"""Pure geometry for SRTM ``.hgt`` tiling — no I/O, fully unit-testable.

An ``.hgt`` tile covers exactly a 1 degree x 1 degree cell, is named after its
south-west corner (``N47E008.hgt``), and holds a square grid of point-registered
elevation samples: SRTM1 = 3601x3601 (1 arc-second), SRTM3 = 1201x1201 (3
arc-seconds). "Point registered" means the samples sit *on* the integer-degree
grid lines, so a tile's first/last sample in each axis lie exactly on its
boundaries and overlap the neighbouring tile by one row/column.

The subtle part is producing such a grid from GDAL. GDAL rasters are area/pixel
registered (a value covers a pixel *cell*), so to land sample centres exactly on
lon..lon+1 at 1/(N-1) degree spacing we must warp to an extent padded outward by
half a pixel on every side. :meth:`Tile.warp_extent` encodes that; see its
docstring for the arithmetic.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# Samples per axis for each supported resolution. N samples span N-1 intervals
# across the 1 degree tile, so the sample spacing is 1/(N-1) degrees.
DEM_RESOLUTIONS: dict[str, int] = {
    "1as": 3601,  # SRTM1, 1 arc-second  (~30 m at the equator)
    "3as": 1201,  # SRTM3, 3 arc-seconds (~90 m)
}
DEFAULT_RESOLUTION = "1as"


@dataclass(frozen=True)
class Tile:
    """One 1x1 degree HGT tile, identified by its south-west corner."""

    lat: int  # south edge (integer degrees, floor of any covered latitude)
    lon: int  # west edge

    @property
    def name(self) -> str:
        """SRTM filename, e.g. ``N47E008.hgt`` or ``S01W135.hgt``."""
        lat_hem = "N" if self.lat >= 0 else "S"
        lon_hem = "E" if self.lon >= 0 else "W"
        return f"{lat_hem}{abs(self.lat):02d}{lon_hem}{abs(self.lon):03d}.hgt"

    def warp_extent(self, samples: int) -> tuple[float, float, float, float]:
        """Outer extent ``(min_lon, min_lat, max_lon, max_lat)`` to warp to.

        We want ``samples`` sample centres per axis sitting exactly on
        ``lon .. lon+1`` (spacing ``px = 1/(samples-1)``). With pixel
        registration the warp target extent is padded by half a pixel on each
        side so the centres land on the boundaries:

            width = 1 + 2*(px/2) = 1 + px = samples/(samples-1) degrees
            width / samples       = 1/(samples-1) = px            (exact)

        i.e. asking GDAL for ``samples`` pixels across this padded extent yields
        a pixel size of exactly ``px`` with the first/last centres on
        ``lon``/``lon+1`` — a valid point-registered SRTM grid.
        """
        half_px = 0.5 / (samples - 1)
        return (
            self.lon - half_px,
            self.lat - half_px,
            self.lon + 1 + half_px,
            self.lat + 1 + half_px,
        )


def tiles_for_bounds(
    min_lon: float, min_lat: float, max_lon: float, max_lat: float
) -> list[Tile]:
    """Every 1x1 degree tile intersecting the WGS84 bounding box.

    Iterates integer-degree cells from ``floor(min)`` up to (but not including)
    ``ceil(max)``: a cell with south-west corner ``(lat, lon)`` spans
    ``[lat, lat+1] x [lon, lon+1]`` and is included iff it overlaps the box.
    """
    if max_lon < min_lon or max_lat < min_lat:
        raise ValueError(f"degenerate bounds: {(min_lon, min_lat, max_lon, max_lat)}")

    tiles: list[Tile] = []
    for lat in range(math.floor(min_lat), math.ceil(max_lat)):
        for lon in range(math.floor(min_lon), math.ceil(max_lon)):
            tiles.append(Tile(lat=lat, lon=lon))
    return tiles
