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

## Choosing a DEM source

The hillshade is downsampled to an SRTM grid before mkgmap embeds it — `--dem-resolution 1as`
(default, ~30 m) or `3as` (~90 m). So feeding full-resolution **swissALTI3D (0.5 m)** for a large
area downloads terabytes only to throw almost all of it away. For anything bigger than a small,
high-detail extent, start from a source that is *already* near the target resolution:

- **Copernicus DEM GLO-30** (~30 m) — free, global, ships as GeoTIFF in **EPSG:4326**. All of
  Switzerland is 18 one-degree tiles (~730 MB) from the [AWS Open Data mirror](https://registry.opendata.aws/copernicus-dem/),
  e.g. `https://copernicus-dem-30m.s3.amazonaws.com/Copernicus_DSM_COG_10_N47_00_E007_00_DEM/Copernicus_DSM_COG_10_N47_00_E007_00_DEM.tif`.
- **swissALTI3D at 2 m** — same EPSG:2056 as the 0.5 m product (no flag change), ~16× smaller.

The tiles' EPSG:4326 CRS is read straight from the files — no `--source-epsg` needed:

```bash
# Whole-Switzerland hillshade from Copernicus GLO-30 tiles (EPSG:4326)
uv run topovert build --dem-dir ./glo30_tiles --out ./out/swiss-glo30.img
```

This builds a ~90 MB country-wide `gmapsupp.img` in under a minute on a laptop. Use
`--dem-resolution 3as` for an even smaller ~90 m map.

`--source-epsg` is optional: by default each input's CRS is auto-detected from the file, so you
normally never set it. Pass it only to *override* a source that lacks CRS metadata (it then applies
to both inputs).

### Combining a non-LV95 DEM with swissTLM3D vectors

The DEM and `--tlm` source may be in **different** projections — each is reprojected from its own
detected CRS — so a Copernicus (EPSG:4326) DEM combines with swissTLM3D (EPSG:2056) directly, with
no reprojection step and no `--source-epsg`:

```bash
# whole-Switzerland hillshade + full swissTLM3D vector overlay
uv run topovert build --dem-dir ./glo30_tiles \
    --tlm ./SWISSTLM3D_2026_LV95_LN02.gdb --out ./out/swiss-glo30-tlm.img
```

The country-wide vector layer is ~167 M nodes, so splitter tiles it automatically (~112 tiles);
the run takes ~30 min and yields a ~250 MB `gmapsupp.img` with roads/routing, land cover,
buildings, watercourses and the hillshade.

> **Datum note:** Copernicus/SRTM heights are geoid-referenced (EGM2008/EGM96) while swissALTI3D is
> ellipsoidal, so *absolute* elevations differ by the geoid separation (~46–50 m in Switzerland).
> This does not affect shaded relief; it only matters if you read off point elevations.