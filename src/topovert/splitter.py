"""Tile a large OSM file into mkgmap-sized pieces with mkgmap's ``splitter``.

A whole-canton/country swissTLM3D extent is far larger than a single Garmin map
tile can hold, so ``splitter.jar`` divides the OSM into ``--max-nodes`` sized
pieces (``NNNNNNNN.osm.o5m``) that mkgmap then compiles together into one map.

We emit **o5m**, not PBF. PBF packs entities into fixed fileblocks with an
entity/size cap; dense ``--contours`` data (very long ways with huge node
arrays, e.g. 20 m contours over the Alps) overflows a block and splitter aborts
with "too many entities in a block. Parsers will reject it." o5m is a flat
streaming format with no such block limit, and mkgmap reads it natively.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from . import TopovertError

log = logging.getLogger(__name__)

# splitter's own default; a comfortable tile size for mkgmap.
DEFAULT_MAX_NODES = 1_600_000


def split_cmd(
    java: str,
    jar: Path,
    osm_path: Path,
    out_dir: Path,
    *,
    max_nodes: int,
    mapid: str,
) -> list[str]:
    """Argv for the splitter run. ``--mapid`` seeds the per-tile map numbers."""
    return [
        java, "-jar", str(jar),
        # o5m, not pbf: PBF's per-fileblock entity cap is overflowed by dense
        # contour ways (see module docstring); o5m has no block limit.
        "--output=o5m",
        "--output-dir=" + str(out_dir),
        "--max-nodes=" + str(max_nodes),
        "--mapid=" + mapid,
        str(osm_path),
    ]


def split(
    java: str,
    jar: Path,
    osm_path: Path,
    out_dir: Path,
    *,
    max_nodes: int = DEFAULT_MAX_NODES,
    mapid: str = "63240001",
) -> list[Path]:
    """Split ``osm_path`` into ``out_dir``; return the produced ``.osm.o5m`` tiles."""
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = split_cmd(java, jar, osm_path, out_dir, max_nodes=max_nodes, mapid=mapid)
    log.debug("run: %s", " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise TopovertError(
            f"splitter failed (exit {proc.returncode}):\n"
            f"{proc.stdout.strip()}\n{proc.stderr.strip()}"
        )
    tiles = sorted(out_dir.glob("*.o5m"))
    if not tiles:
        raise TopovertError(
            f"splitter produced no tiles in {out_dir}\n{proc.stdout.strip()}"
        )
    log.info("splitter produced %d tile(s)", len(tiles))
    return tiles
