"""Drive mkgmap to compile an OSM file + HGT tiles into a Garmin ``.IMG``."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from . import TopovertError

log = logging.getLogger(__name__)

# The deliverable: a single self-contained map. ``--gmapsupp`` bundles the detail
# tile + overview (TRE/RGN/LBL/DEM) into one gmapsupp.img that a Garmin device
# loads from its Garmin/ folder and QMapShack opens directly. Copying the bare
# detail tile instead renders empty and makes QMapShack mmap past EOF.
GMAPSUPP_NAME = "gmapsupp.img"

# Garmin map identity. The mapname is the 8-digit map number (also the detail
# tile filename); family/product id must be set so the map registers on-device.
FAMILY_ID = 6324
PRODUCT_ID = 1
MAP_NUMBER = "63240001"


def build_img_cmd(
    java: str,
    jar: Path,
    osm_inputs: list[Path],
    out_dir: Path,
    *,
    map_name: str,
    hgt_dir: Path | None = None,
    mapname: str | None = MAP_NUMBER,
) -> list[str]:
    """Argv for the mkgmap run.

    ``--gmapsupp`` makes the loadable single-file product. ``hgt_dir`` adds
    ``--dem`` for hillshading (omit for a vector-only map). ``osm_inputs`` is one
    ``.osm`` (single tile) or many ``.osm.pbf`` (splitter tiles); for the latter
    pass ``mapname=None`` so mkgmap takes each tile's number from its filename.
    """
    cmd = [
        java, "-jar", str(jar),
        "--output-dir=" + str(out_dir),
        "--description=" + map_name,
        "--family-id=" + str(FAMILY_ID),
        "--product-id=" + str(PRODUCT_ID),
        "--country-name=Switzerland",
        "--gmapsupp",
    ]
    if mapname is not None:
        cmd.append("--mapname=" + mapname)
    if hgt_dir is not None:
        cmd.append("--dem=" + str(hgt_dir))
    cmd += [str(p) for p in osm_inputs]
    return cmd


def build_img(
    java: str,
    jar: Path,
    osm_inputs: list[Path],
    out_dir: Path,
    *,
    map_name: str = "topovert",
    hgt_dir: Path | None = None,
    mapname: str | None = MAP_NUMBER,
) -> Path:
    """Run mkgmap and return the path to the produced ``gmapsupp.img``."""
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = build_img_cmd(
        java, jar, osm_inputs, out_dir,
        map_name=map_name, hgt_dir=hgt_dir, mapname=mapname,
    )
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

    img = out_dir / GMAPSUPP_NAME
    if not img.exists():
        raise TopovertError(
            f"mkgmap reported success but {img} was not produced.\n{proc.stdout.strip()}"
        )
    return img
