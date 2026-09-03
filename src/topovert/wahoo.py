"""Compile the assembled OSM into Wahoo-loadable map tiles (mapsforge ``.map``).

Wahoo's ELEMNT/BOLT/ROAM read **mapsforge** maps, not Garmin ``.IMG``, so this
module is the Wahoo-side twin of :mod:`topovert.mkgmap`: it starts from the very
same ``map.osm`` that :func:`topovert.osm.assemble_osm` produces and ends at files
a device loads. Everything upstream of that OSM (GDAL, swissTLM3D tagging,
contours) is format-neutral and shared with the Garmin path::

    map.osm  --(this module: add OSM 0.6 version attrs)-->  osmosis.osm
             --osmosis --mapfile-writer, once per z8 tile-->  <x>/<y>.map
             --lzma (legacy "alone" container)------------->  <x>/<y>.map.lzma

The firmware wants one file per **zoom-8 slippy tile** under ``maps/tiles/8/`` on
the device, so the map bounds are cut into that grid. Each tile gets an empty
``.map.lzma.17`` companion — Wahoo's "tile present" marker.

Three things do **not** carry over from the Garmin path:

* **No hillshading.** The ``.map`` format embeds no DEM and the firmware does no
  runtime hill shading, so ``--contours`` is what carries the terrain.
* **No transparent overlay.** A tile we ship *replaces* Wahoo's own tile for that
  cell — there is no draw-priority stacking — so a tile with no data of ours
  blanks that square rather than showing through. Empty tiles are therefore not
  written at all (see :func:`osmosis_xml`).
* **Styling lives on the device.** Colours/widths come from a mapsforge render
  theme installed on the device (``styles/wahoo/topovert-theme.xml``); nothing
  style-like is embedded in the map. The tag-mapping only decides what is *stored*.
"""

from __future__ import annotations

import logging
import lzma
import math
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import TopovertError

log = logging.getLogger(__name__)

# Wahoo indexes its maps by zoom-8 slippy tile (``maps/tiles/8/<x>/<y>.map.lzma``).
TILE_ZOOM = 8

# mapsforge sub-file layout: one zoom interval with base zoom 12 covering z0-17,
# matching what wahooMapsCreator ships (Wahoo's own maps use a single interval).
ZOOM_INTERVAL_CONF = "12,0,17"

# osmosis' entry point; we build the classpath ourselves rather than using its
# bin/osmosis wrapper (no shell, no chmod, and it works the same on Windows).
OSMOSIS_MAIN = "org.openstreetmap.osmosis.core.Osmosis"

# Marker file Wahoo expects beside each tile to consider it present.
TILE_PRESENT_SUFFIX = ".17"

_STYLES_DIR = Path(__file__).resolve().parent / "styles"
WAHOO_STYLE_DIR = _STYLES_DIR / "wahoo"
# What gets *stored* in the .map (map-writer's tag-conf-file) ...
TAG_CONF_FILE = WAHOO_STYLE_DIR / "tag-mapping.xml"
# ... and how the device *draws* it (installed on the device, not embedded).
THEME_FILE = WAHOO_STYLE_DIR / "topovert-theme.xml"

# Our OSM writer omits the ``version`` attribute (mkgmap does not care), but
# osmosis rejects OSM 0.6 entities without one. Matches the opening tag of a
# node/way element as osm.py serializes it (ids may be negative).
_ENTITY_RE = re.compile(r"^(\s*<(?:node|way) id='-?\d+')")

# Node coordinates, as osm.node_xml writes them, for the occupancy pass below.
_NODE_RE = re.compile(r"^\s*<node id='-?\d+' lat='(-?[\d.]+)' lon='(-?[\d.]+)'")


@dataclass(frozen=True)
class MapTile:
    """One zoom-8 slippy tile: the unit Wahoo stores and looks maps up by."""

    x: int
    y: int
    zoom: int = TILE_ZOOM

    @property
    def west(self) -> float:
        return _x_to_lon(self.x, self.zoom)

    @property
    def east(self) -> float:
        return _x_to_lon(self.x + 1, self.zoom)

    @property
    def north(self) -> float:
        return _y_to_lat(self.y, self.zoom)

    @property
    def south(self) -> float:
        return _y_to_lat(self.y + 1, self.zoom)

    def bbox_arg(self) -> str:
        """map-writer's ``bbox=`` value: ``minLat,minLon,maxLat,maxLon``."""
        return (
            f"{self.south:.6f},{self.west:.6f},{self.north:.6f},{self.east:.6f}"
        )

    def rel_path(self, suffix: str = ".map.lzma") -> Path:
        """On-device relative path, e.g. ``134/89.map.lzma`` under ``tiles/8/``."""
        return Path(str(self.x)) / f"{self.y}{suffix}"


def _lon_to_x(lon: float, zoom: int) -> int:
    return int(math.floor((lon + 180.0) / 360.0 * (1 << zoom)))


def _lat_to_y(lat: float, zoom: int) -> int:
    # Web Mercator is undefined at the poles; clamp to its usual cutoff.
    lat = max(min(lat, 85.0511287798), -85.0511287798)
    rad = math.radians(lat)
    return int(math.floor((1.0 - math.asinh(math.tan(rad)) / math.pi) / 2.0 * (1 << zoom)))


def _x_to_lon(x: int, zoom: int) -> float:
    return x / (1 << zoom) * 360.0 - 180.0


def _y_to_lat(y: int, zoom: int) -> float:
    n = math.pi - 2.0 * math.pi * y / (1 << zoom)
    return math.degrees(math.atan(math.sinh(n)))


def tiles_for_bounds(
    bounds: tuple[float, float, float, float], zoom: int = TILE_ZOOM
) -> list[MapTile]:
    """Every zoom-8 tile touched by ``(min_lon, min_lat, max_lon, max_lat)``.

    Bounds landing exactly on a tile edge do not pull in the next tile: that one
    would hold no data, and an empty tile blanks the device's own map there.
    """
    min_lon, min_lat, max_lon, max_lat = bounds
    x_lo, x_hi = _lon_to_x(min_lon, zoom), _lon_to_x(max_lon, zoom)
    if x_hi > x_lo and _x_to_lon(x_hi, zoom) == max_lon:
        x_hi -= 1
    # y grows southwards, so the *north* edge gives the low index.
    y_lo, y_hi = _lat_to_y(max_lat, zoom), _lat_to_y(min_lat, zoom)
    if y_hi > y_lo and _y_to_lat(y_hi, zoom) == max_lat:
        y_hi -= 1
    return [
        MapTile(x=x, y=y, zoom=zoom)
        for x in range(x_lo, x_hi + 1)
        for y in range(y_lo, y_hi + 1)
    ]


def osmosis_xml(
    src: Path, dst: Path, *, zoom: int = TILE_ZOOM
) -> tuple[Path, set[tuple[int, int]]]:
    """Rewrite ``src`` into ``dst`` for osmosis, and report which tiles hold data.

    The rewrite adds the ``version`` attribute: mkgmap accepts our OSM as written,
    but osmosis' XML reader aborts with "does not have a version attribute as OSM
    0.6 are required to have". Doing it here rather than in :mod:`topovert.osm`
    keeps the attribute — dead weight for mkgmap, ~20% of a node line — off the
    Garmin path. Timestamps stay absent; ``enableDateParsing=false`` tells the
    reader not to expect them.

    The ``(x, y)`` tiles that actually contain a node come along for free, since
    the whole file streams past anyway. They are what keeps empty tiles from being
    written (an empty tile is a valid ~1 KB file that would blank the device's own
    map for that square) — a size threshold cannot tell the two apart. Ways need
    no separate check: a zoom-8 tile is ~100 km across, so nothing spans one
    without a node inside it.
    """
    occupied: set[tuple[int, int]] = set()
    with src.open("r", encoding="utf-8") as fin, dst.open("w", encoding="utf-8") as fout:
        for line in fin:
            node = _NODE_RE.match(line)
            if node is not None:
                lat, lon = float(node.group(1)), float(node.group(2))
                occupied.add((_lon_to_x(lon, zoom), _lat_to_y(lat, zoom)))
            fout.write(_ENTITY_RE.sub(r"\1 version='1'", line))
    return dst, occupied


def classpath(osmosis_dir: Path, plugin_jar: Path) -> str:
    """osmosis' jars plus the map-writer plugin.

    The plugin registers itself through the ``osmosis-plugins.conf`` resource that
    osmosis scans on the classpath, so simply being *on* it is enough — no need to
    install anything into ``~/.openstreetmap``. ``lib/default/*`` is expanded by
    the JVM launcher itself, not the shell.
    """
    return os.pathsep.join(
        [str(osmosis_dir / "lib" / "default" / "*"), str(plugin_jar)]
    )


def map_writer_cmd(
    java: str,
    osmosis_dir: Path,
    plugin_jar: Path,
    osm_path: Path,
    tile: MapTile,
    out_map: Path,
    *,
    tag_conf: Path | None = TAG_CONF_FILE,
    zoom_interval_conf: str = ZOOM_INTERVAL_CONF,
    threads: int = 1,
    hd: bool = False,
    max_heap: str | None = None,
) -> list[str]:
    """Argv for one map-writer run: the whole OSM in, one tile's ``.map`` out.

    map-writer clips to its own ``bbox``, so pre-cutting the OSM per tile is not
    needed for correctness (it is only a speed question — see topovert-clv.5).
    ``hd`` picks map-writer's hard-disk mode, which trades speed for not holding
    the tile's data in RAM; ``max_heap`` (e.g. ``8g``) sets the JVM ``-Xmx``.
    """
    cmd = [java]
    if max_heap is not None:
        cmd.append("-Xmx" + max_heap)
    cmd += [
        "-cp", classpath(osmosis_dir, plugin_jar),
        OSMOSIS_MAIN,
        "--read-xml", "file=" + str(osm_path),
        # We emit no timestamps (mkgmap never wanted them); without this the
        # reader fails with "The entity timestamp attribute is missing".
        "enableDateParsing=false",
        "--mapfile-writer", "file=" + str(out_map),
        "bbox=" + tile.bbox_arg(),
        "zoom-interval-conf=" + zoom_interval_conf,
        "threads=" + str(threads),
    ]
    if hd:
        cmd.append("type=hd")
    if tag_conf is not None:
        cmd.append("tag-conf-file=" + str(tag_conf))
    return cmd


def compress_map(src: Path, dst: Path, *, preset: int = 6) -> Path:
    """LZMA-compress ``src`` to ``dst`` in the container Wahoo expects.

    That is the **legacy "alone"** ``.lzma`` container (what the ``lzma`` CLI
    writes), *not* ``.xz`` — hence ``FORMAT_ALONE``. Streamed so a country-scale
    tile never has to be held in memory.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    comp = lzma.LZMACompressor(format=lzma.FORMAT_ALONE, preset=preset)
    with src.open("rb") as fin, dst.open("wb") as fout:
        for chunk in iter(lambda: fin.read(1 << 20), b""):
            fout.write(comp.compress(chunk))
        fout.write(comp.flush())
    return dst


def _run_osmosis(cmd: list[str], tile: MapTile) -> None:
    log.debug("run: %s", " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise TopovertError(
            f"osmosis/mapsforge-map-writer failed for tile {tile.x}/{tile.y} "
            f"(exit {proc.returncode}):\n{proc.stdout.strip()}\n{proc.stderr.strip()}"
        )
    # osmosis logs to stderr and keeps going on plenty of real problems, so
    # surface the loud lines instead of swallowing them with its output.
    for line in (proc.stdout + "\n" + proc.stderr).splitlines():
        if "SEVERE" in line or "WARNING" in line:
            log.warning("osmosis: %s", line.strip())


def build_tiles(
    java: str,
    osmosis_dir: Path,
    plugin_jar: Path,
    map_osm: Path,
    bounds: tuple[float, float, float, float],
    out_dir: Path,
    *,
    workdir: Path,
    tag_conf: Path | None = TAG_CONF_FILE,
    threads: int = 1,
    hd: bool = False,
    max_heap: str | None = None,
    zoom: int = TILE_ZOOM,
    keep_intermediate: bool = False,
) -> list[MapTile]:
    """Write ``<out_dir>/<x>/<y>.map.lzma`` (+ marker) for every tile in ``bounds``.

    Returns the tiles written: those covering ``bounds`` that actually contain
    data (see :func:`osmosis_xml`). Tiles are compiled one at a time, so the run
    is O(tiles x input) — fine for a patch, the thing to optimise for a country
    (topovert-clv.5).
    """
    osm_path, occupied = osmosis_xml(map_osm, workdir / "osmosis.osm", zoom=zoom)
    tiles = [t for t in tiles_for_bounds(bounds, zoom) if (t.x, t.y) in occupied]
    if not tiles:
        raise TopovertError(
            f"no zoom-{zoom} tile holds any data — nothing to write for bounds "
            f"{bounds} (a Wahoo map needs --contours and/or --tlm)"
        )
    log.info("Wahoo output: %d zoom-%d tile(s) with data", len(tiles), zoom)
    raw_dir = workdir / "wahoo"
    raw_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    written: list[MapTile] = []
    for n, tile in enumerate(tiles, start=1):
        log.info("tile %d/%d: %d/%d", n, len(tiles), tile.x, tile.y)
        raw_map = raw_dir / f"{tile.x}-{tile.y}.map"
        _run_osmosis(
            map_writer_cmd(
                java, osmosis_dir, plugin_jar, osm_path, tile, raw_map,
                tag_conf=tag_conf, threads=threads, hd=hd, max_heap=max_heap,
            ),
            tile,
        )
        if not raw_map.exists():
            raise TopovertError(
                f"map-writer reported success but {raw_map} was not produced"
            )
        dst = out_dir / tile.rel_path()
        compress_map(raw_map, dst)
        # Wahoo's "tile present" marker sits beside the tile.
        (dst.parent / (dst.name + TILE_PRESENT_SUFFIX)).write_bytes(b"")
        if not keep_intermediate:
            raw_map.unlink()
        written.append(tile)
    return written
