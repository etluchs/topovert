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
- **Elevation contour lines** derived from the DEM (`--contours`), spaced every `--contour-interval`
  metres (default 20 m) with every 5th drawn as a bold index line. Labels show the correct elevation
  in whatever unit the device is set to (the style emits feet, which Garmin's `.IMG` format expects).
  `--no-hillshade` skips embedding the (large) DEM for shaded relief, so `--contours --no-hillshade`
  yields a lightweight contour-only map.
- **Preview contours as an SVG** (`render-contours`): render a patch's contour geometry + elevation
  labels to an SVG you can open in a browser, to check them quickly without building/loading an `.IMG`.
- **Preview the style as a browser legend** (`legend`): a swatch per rendered element — drawn from the
  bundled TYP's own colours/widths, annotated with the OSM tags that route to it and the Garmin type
  code — served with **hot reload** so editing `styles/topovert_typ.txt` or the rule files updates the
  page live. Tune colours without a full conversion + QMapShack round-trip.
- A bundled **Swiss topographic style + TYP** colours the output (forest, rock, glacier and water
  fills; red paths; brown contours; Swiss-tuned road/rail rendering) so the map is legible on-device
  out of the box.
- **Auto-download the DEM for an area** (`--dem-area`): instead of supplying local tiles, name an
  area (`switzerland`) or a WGS84 bbox and topovert fetches the covering **Copernicus GLO-30**
  (~30 m) GeoTIFF tiles into a cache and builds from them. All of Switzerland is 18 tiles (~730 MB).

Full **swissTLM3D / swissALTI3D STAC** area selection is the planned next step.

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

# Add elevation contour lines from the DEM (20 m spacing by default)
uv run topovert build --dem-dir ./swissalti3d_tiles --contours --out ./out/swiss.img

# Lightweight contour-only map: derive contours but skip the heavy hillshade DEM
uv run topovert build --dem-dir ./swissalti3d_tiles --contours --no-hillshade --out ./out/swiss.img

# No local tiles? Auto-download the DEM for an area (Copernicus GLO-30) and build
# a whole-Switzerland contour-only map (no shading) — ideal for a Garmin Fenix:
uv run topovert build --dem-area switzerland --contours --no-hillshade --out ./out/swiss-contours.img

# Sanity-check the contours for a small patch without building an .IMG: render
# the line geometry + elevation labels to an SVG you can open in a browser.
uv run topovert render-contours --dem-area "8.0,46.55,8.1,46.65" --out ./out/patch.svg
uv run topovert render-contours --dem-dir ./swissalti3d_tiles --bbox 8.0,46.55,8.1,46.65 \
    --out ./out/patch.svg

# Fine-tune the style: serve a live legend of every element (swatch + tags + type
# code) and edit styles/topovert_typ.txt — the browser hot-reloads on save.
uv run topovert legend                         # http://127.0.0.1:8000 (Ctrl-C to stop)
uv run topovert legend --out ./out/legend.html # or a standalone HTML file, no server
```

Then copy the `.IMG` to your Garmin device (or load it in BaseCamp) to see the shaded relief.
Run `topovert build --help` for options (resolution, resampling, source EPSG, `--tlm`/`--tlm-layer`,
`--contours`/`--contour-interval`, `--no-hillshade`, `--max-heap`, `--opaque`),
`topovert render-contours --help` to preview contours for a patch as an SVG, or
`topovert legend --help` to preview/tune the style in a browser.

> **The map is a transparent overlay by default**, so it draws *on top of* your device's base map
> (roads, towns and labels show through between the contours/features) instead of hiding it behind an
> opaque tile. Pass `--opaque` only if topovert is meant to be your sole base map.

> **Large builds run faster with more JVM heap.** On a country-scale map mkgmap warns that it is
> throttling to a single job under the default heap; pass e.g. `--max-heap 8g` (sized to your RAM) to
> let mkgmap/splitter parallelise. It is off by default, so small builds are unaffected.

## Choosing a DEM source

The hillshade is downsampled to an SRTM grid before mkgmap embeds it — `--dem-resolution 1as`
(default, ~30 m) or `3as` (~90 m). So feeding full-resolution **swissALTI3D (0.5 m)** for a large
area downloads terabytes only to throw almost all of it away. For anything bigger than a small,
high-detail extent, start from a source that is *already* near the target resolution:

- **Copernicus DEM GLO-30** (~30 m) — free, global, ships as GeoTIFF in **EPSG:4326**. All of
  Switzerland is 18 one-degree tiles (~730 MB) from the [AWS Open Data mirror](https://registry.opendata.aws/copernicus-dem/),
  e.g. `https://copernicus-dem-30m.s3.amazonaws.com/Copernicus_DSM_COG_10_N47_00_E007_00_DEM/Copernicus_DSM_COG_10_N47_00_E007_00_DEM.tif`.
  **`--dem-area` fetches these for you** (cached in `~/.cache/topovert/copernicus-dem-30m/`), so you
  never have to download them by hand — pass `--dem-area switzerland` (or a bbox) instead of `--dem-dir`.
- **swissALTI3D at 2 m** — same EPSG:2056 as the 0.5 m product (no flag change), ~16× smaller.

The tiles' EPSG:4326 CRS is read straight from the files — no `--source-epsg` needed:

```bash
# Whole-Switzerland hillshade from Copernicus GLO-30 tiles (EPSG:4326)
uv run topovert build --dem-dir ./glo30_tiles --out ./out/swiss-glo30.img

# ...or let topovert download those same tiles for you:
uv run topovert build --dem-area switzerland --out ./out/swiss-glo30.img
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