# Topographical Map converter

## Purpose

Topovert supports easily configurable conversion of topographical maps into target formats.

The initial use case is creating maps for Garmin navigation devices (see [Openstreetmap Wiki](https://wiki.openstreetmap.org/wiki/OSM_Map_On_Garmin/IMG_File_Format)) based on the freely available [data from Swisstopo](https://www.swisstopo.admin.ch/de/digitale-karten).

## Features

Key features are:

 - Easy selection of base maps (area, geodata, scale, ...) based on what Swisstopo offers (use their selection tooling if possible)
 - Should run on any OS (Linux, OSX, Windows). One idea would be Browser with, if necessary, WASM. But research is necessary here.
 - producds .IMG files ready to install on Garmin navigation gear.

## Status

v1 converts a local directory of **swissALTI3D** GeoTIFF elevation tiles (EPSG:2056) into a
**hill-shaded Garmin `.IMG`**. It is a thin Python orchestrator around the **GDAL** command-line
tools and **mkgmap**. Vector map features (swissTLM3D), contour lines, and automatic area download
are planned next.

## Requirements

- Python ≥ 3.11
- GDAL command-line tools (with the SRTMHGT driver) on `PATH`
- A Java runtime ≥ 1.8 (mkgmap is auto-downloaded into `~/.cache/topovert/` on first run)

## Usage

```bash
uv sync

# Point at a folder of swissALTI3D *.tif tiles; get a hill-shaded .IMG
uv run topovert build --dem-dir ./swissalti3d_tiles --out ./out/swiss.img

# Optionally overlay swissTLM3D vector features (roads, watercourses, water, buildings)
uv run topovert build --dem-dir ./swissalti3d_tiles \
    --tlm ./SWISSTLM3D_CHLV95LN02.gdb --out ./out/swiss.img
```

Then copy the `.IMG` to your Garmin device (or load it in BaseCamp) to see the shaded relief.
Run `topovert build --help` for options (resolution, resampling, source EPSG, `--tlm`/`--tlm-layer`).