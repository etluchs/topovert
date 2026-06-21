# Topographical Map converter

## Purpose

Topovert supports easily configurable conversion of topographical maps into target formats.

The initial use case is creating maps for Garmin navigation devices (see [Openstreetmap Wiki](https://wiki.openstreetmap.org/wiki/OSM_Map_On_Garmin/IMG_File_Format)) based on the freely available [data from Swisstopo](https://www.swisstopo.admin.ch/de/digitale-karten).

## Features

Key features are:

 - Easy selection of base maps (area, geodata, scale, ...) based on what Swisstopo offers (use their selection tooling if possible)
 - Runs on any OS (Linux, OSX, Windows) as a local command-line tool. (A browser/WASM front-end was
   explored but is incompatible with the GDAL + JVM toolchain v1 relies on.)
 - Produces .IMG files ready to install on Garmin navigation gear.

## Status

v1 turns freely available Swisstopo data into a Garmin `.IMG`. It is a thin Python orchestrator
(stdlib only) around the **GDAL** command-line tools, **mkgmap**, and **splitter**:

- A local directory of **swissALTI3D** GeoTIFF elevation tiles (EPSG:2056 / LV95) becomes a
  **hill-shaded** map (`--dem-dir`).
- **swissTLM3D** vector features overlay the map (`--tlm`): roads/paths, railways, aerialways,
  watercourses, land cover (water/forest/rock/glacier/wetland), buildings, POIs, and walls.
- Either input is optional: `--tlm` alone makes a **vector-only** map, `--dem-dir` alone a
  hillshade-only map. Large extents (up to whole-country) are tiled with **splitter** automatically.
- A bundled **Swiss topographic style + TYP** colours the output (forest, rock, glacier and water
  fills; red paths; Swiss-tuned road/rail rendering) so the map is legible on-device out of the box.

Contour lines and automatic area download are planned next.

## Requirements

- Python ≥ 3.11
- GDAL command-line tools (with the SRTMHGT driver) on `PATH`
- A Java runtime ≥ 1.8 (mkgmap is auto-downloaded into `~/.cache/topovert/` on first run)

## Usage

```bash
uv sync

# Point at a folder of swissALTI3D *.tif tiles; get a hill-shaded .IMG
uv run topovert build --dem-dir ./swissalti3d_tiles --out ./out/swiss.img

# Overlay swissTLM3D vector features (roads, rail, water, land cover, buildings, POIs)
uv run topovert build --dem-dir ./swissalti3d_tiles \
    --tlm ./SWISSTLM3D_CHLV95LN02.gdb --out ./out/swiss.img

# Vector-only (no DEM); large extents are tiled with splitter automatically
uv run topovert build --tlm ./SWISSTLM3D_CHLV95LN02.gdb --out ./out/swiss.img
```

Then copy the `.IMG` to your Garmin device (or load it in BaseCamp) to see the shaded relief.
Run `topovert build --help` for options (resolution, resampling, source EPSG, `--tlm`/`--tlm-layer`).