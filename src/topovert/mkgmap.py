"""Drive mkgmap to compile an OSM file + HGT tiles into a Garmin ``.IMG``."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from . import TopovertError

log = logging.getLogger(__name__)

# mkgmap's default tile output name when no mapname is forced.
DEFAULT_IMG_NAME = "63240001.img"


def build_img_cmd(
    java: str,
    jar: Path,
    osm_path: Path,
    hgt_dir: Path,
    out_dir: Path,
    *,
    map_name: str,
) -> list[str]:
    """Argv for the mkgmap run. ``--dem`` points at the directory of .hgt tiles."""
    return [
        java, "-jar", str(jar),
        "--output-dir=" + str(out_dir),
        "--description=" + map_name,
        "--dem=" + str(hgt_dir),
        str(osm_path),
    ]


def build_img(
    java: str,
    jar: Path,
    osm_path: Path,
    hgt_dir: Path,
    out_dir: Path,
    *,
    map_name: str = "topovert",
) -> Path:
    """Run mkgmap and return the path to the produced ``.img``."""
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = build_img_cmd(java, jar, osm_path, hgt_dir, out_dir, map_name=map_name)
    log.debug("run: %s", " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise TopovertError(
            f"mkgmap failed (exit {proc.returncode}):\n{proc.stdout.strip()}\n"
            f"{proc.stderr.strip()}"
        )
    # Surface the line confirming DEM was embedded, useful for verification.
    for line in proc.stdout.splitlines():
        if "DEM" in line or "dem" in line:
            log.info("mkgmap: %s", line.strip())

    img = out_dir / DEFAULT_IMG_NAME
    if not img.exists():
        raise TopovertError(
            f"mkgmap reported success but {img} was not produced.\n{proc.stdout.strip()}"
        )
    return img
