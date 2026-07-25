# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What the project does

Topovert converts freely available [Swisstopo](https://www.swisstopo.admin.ch/de/digitale-karten)
data into `.IMG` files installable on Garmin navigation devices.

**v1 (current) is one vertical slice:** a local directory of swissALTI3D GeoTIFF elevation tiles
(EPSG:2056 / LV95) → a hill-shaded Garmin `.IMG`. `--tlm <swissTLM3D>` overlays vector features —
roads/paths, railways, aerialways, watercourses, land cover (water/forest/rock/glacier/wetland),
buildings, POIs, and walls. `--contours` adds elevation contour lines derived from the DEM.
Either input is optional: `--tlm` alone makes a **vector-only** map (no DEM), `--dem-dir` alone a
hillshade-only map. Large extents (up to whole-country) are tiled with **splitter** automatically.
`--dem-area <name|bbox>` (e.g. `switzerland`) auto-downloads the covering **Copernicus GLO-30**
(~30 m) DEM tiles in place of `--dem-dir`. A separate `render-contours` command previews a patch's
contours as an SVG (for checking geometry + elevation labels without building an `.IMG`). A
`legend` command previews the bundled style + TYP as a browser-viewable legend (one swatch per
rendered element) and hot-reloads as you edit the style files, so colours/widths can be tuned
without a full conversion. Full
swissTLM3D/swissALTI3D STAC area selection is still deferred (`bd show topovert-y8s`).

## Keep the docs in sync

**When you change user-facing behaviour, update the docs in the same commit** — they drift fast
otherwise. The sources of truth are this `CLAUDE.md`, `README.md`, and the `argparse` help strings
in `cli.py`. Concretely:

- New/renamed/removed CLI flags or commands → update `cli.py` help, the `README.md` **Usage** block,
  and the `## Commands` section here.
- Capabilities shipping or moving from "planned" → "done" → update the `README.md` **Status**/**Features**
  sections and the **What the project does** summary above.
- Input/output formats, prerequisites, or version pins → update wherever they're named (e.g. swissTLM3D
  is a `.gdb`, not `.gpkg`; Java ≥ 1.8; Python ≥ 3.11).

Before ending a session that touched code, skim `README.md` and this file for stale claims.
`tests/test_docs.py` enforces the cheap-to-check parts in CI (README flags exist in `cli.py`, the
Python pin matches `pyproject`, swissTLM3D is a `.gdb`); extend it when you add a checkable invariant.

## Stack & architecture

**Decided by a stability-and-simplicity-first rule:** a thin **Python** orchestrator (stdlib only,
zero runtime pip deps) that shells out to two mature external toolchains and writes almost no
geospatial/encoding code itself:

- **GDAL command-line tools** (`gdalbuildvrt`, `gdalwarp`, `gdal_translate`, `gdalinfo`, `ogr2ogr`,
  and `gdal_contour` for `--contours`) — reprojection, raster conversion, vector export, and contour
  extraction. We use the **CLI, not the `osgeo.gdal` bindings** (far easier cross-platform install,
  version-tolerant). This is also why the vector path uses `ogr2ogr → GeoJSON → our own OSM writer`
  rather than `ogr2pbf`, and contours use `gdal_contour` rather than `pyhgtmap` (both alternatives
  need the `osgeo` bindings + pip deps) — see `bd show topovert-rn1` / `topovert-783`.
- **mkgmap** (Java) — does all Garmin `.IMG` encoding, including embedding the DEM for hillshading.
- **splitter** (Java, same source) — tiles a large `.osm` into mkgmap-sized pieces; used automatically
  when the OSM exceeds `pipeline.SPLIT_NODE_THRESHOLD` nodes.

Pipeline (`src/topovert/pipeline.py:build`): bounds come from the DEM mosaic (`gdalbuildvrt` →
`gdalinfo wgs84Extent`) or, with no DEM, from the swissTLM3D extent (`vector.tlm_bounds`). With
`--dem-area`, `dem_download.download_area` fetches the covering Copernicus GLO-30 GeoTIFFs into the
cache first and the build proceeds as if that were `--dem-dir`. DEM path:
per-1°-tile `gdalwarp` (2056→4326, SRTM1/3 grid) + `gdal_translate -of SRTMHGT`. Vector path:
`ogr2ogr` → GeoJSON → `.osm`. Contour path (`--contours`): `gdal_contour` (DEM mosaic VRT → GPKG)
→ `ogr2ogr` reproject → GeoJSON → `.osm`. The vector and contour feeds share one id allocator and
`osm.assemble_osm` merges their fragment streams into one osmosis-ordered `.osm`. The DEM mosaic is
always built (bounds + contours), but the HGT tiles + `--dem` embed only happen when hillshading;
`--no-hillshade` skips them (and the SRTMHGT driver requirement) for a small contour-only map. Then
(splitter if large →) `mkgmap --gmapsupp [--dem]` → single loadable `gmapsupp.img`. Modules: `hgt.py` (SRTM
tiling geometry — unit-tested), `gdal_tools.py` (GDAL argv builders + exec), `osm.py` (OSM XML
writer + node/way serializers + `assemble_osm`), `vector.py` (swissTLM3D → GeoJSON → OSM, data-only
`TAG_MAP`, bounds), `contour.py` (DEM → `gdal_contour` → OSM contour ways), `contour_render.py` (the
`render-contours` dev utility: DEM/GeoJSON → standalone SVG of contour lines + labels), `legend.py`
(the `legend` dev utility: parse the TYP + style rules → HTML legend of swatch-per-element, served
with a stdlib hot-reload HTTP server or written as a static file), `dem_download.py`
(`--dem-area` → Copernicus GLO-30 tile download-cache), `jars.py` (Java +
mkgmap/splitter download-cache), `splitter.py`, `mkgmap.py` (the `.IMG` build), `cli.py`. The bundled mkgmap
**style + TYP** live under `src/topovert/styles/` (`topovert/` rule files + `topovert_typ.txt`);
`mkgmap.py` always applies them via `--style-file` + the TYP input.

The README's "browser + WASM" idea is **incompatible** with this GDAL+JVM pipeline; v1 is a local CLI.

## Commands

```bash
uv sync                                   # one-time setup (creates .venv + installs dev deps)
uv run pytest                             # run all unit tests
uv run pytest tests/test_hgt.py -q        # one test file
uv run topovert build --dem-dir ./tiles --out ./out/swiss.img   # hillshade-only
uv run topovert build --tlm ./SWISSTLM3D.gdb --out ./out/ch.img # vector-only (no DEM; splitter as needed)
uv run topovert build --dem-dir ./tiles --tlm ./tlm.gdb --out ./out/swiss.img   # both
uv run topovert build --dem-dir ./tiles --contours --out ./out/swiss.img        # + contour lines
uv run topovert build --dem-dir ./tiles --contours --no-hillshade --out ./out/c.img  # contour-only (no DEM embed)
uv run topovert build --dem-area switzerland --contours --no-hillshade --out ./out/ch.img  # auto-download DEM, contour-only
uv run topovert build --dem-area switzerland --contours --no-hillshade --max-heap 8g --out ./out/ch.img  # faster: bigger JVM heap
uv run topovert render-contours --dem-area "8.0,46.55,8.1,46.65" --out ./out/patch.svg  # preview contours as SVG
uv run topovert render-contours --from-geojson contours.geojsonl --out patch.svg        # render a kept intermediate
uv run topovert legend                                          # serve the style legend (hot reload) at 127.0.0.1:8000
uv run topovert legend --out ./out/legend.html                  # write a standalone legend, no server
uv run topovert -v build ... --keep-intermediate                # debug: verbose + keep workdir
```

**System prerequisites** (checked at runtime, not pip-installed): GDAL CLI (the **SRTMHGT driver** is
only needed for the DEM/hillshade path) and a Java **≥ 1.8** runtime. mkgmap.jar and splitter.jar are
auto-downloaded + SHA-pinned into the per-user cache (`~/.cache/topovert/`) on first use; override the
sources with `TOPOVERT_MKGMAP_URL` / `TOPOVERT_SPLITTER_URL`.

## Gotchas (hard-won; see `bd memories`)

- **mkgmap needs its `lib/` jars**: `mkgmap.jar`'s manifest `Class-Path` points at sibling `lib/*.jar`
  (osmpbf/protobuf/fastutil). Extract the **whole** distribution, not just the jar, or you get
  `NoClassDefFoundError: crosby/binary/file/BlockReaderAdapter`.
- **mkgmap reads a DEM border beyond the map bounds**, so data near a 1° tile edge makes it touch the
  neighbour tile (warns "file not found … height 0"). `pipeline.DEM_TILE_MARGIN_DEG` pads the bounds so
  neighbour tiles get generated (void where there's no source data).
- **HGT is point-registered**: a valid SRTM1 tile is 3601×3601 with sample centres exactly on the
  integer-degree grid. `Tile.warp_extent` pads the warp target by half a pixel to achieve this — see
  its docstring for the arithmetic. Verify output tiles with `gdalinfo` (expect 3601×3601, Int16, EPSG:4326).
- **mkgmap.org.uk prunes old revisions**, so a pinned URL 404s eventually. Bumping the version means
  updating **both** `jars.MKGMAP_URL` and `jars.MKGMAP_SHA256` (download + `sha256sum`).
- **Ship a `gmapsupp.img`, not a bare tile**: mkgmap emits a detail tile (`63240001.img`) plus a
  separate overview + `.tdb`. The single self-contained product a device loads is `gmapsupp.img`
  (via `--gmapsupp` + `--family-id`/`--product-id`/`--mapname`), which the pipeline copies to `--out`;
  on a Fenix, drop it in the device's `Garmin/` folder.
- **QMapShack's "Mapping a file beyond its size is not portable" warning is benign** — mkgmap declares
  the IMG partition one 512-byte block larger than the actual file. It is NOT a corruption signal and
  does not affect rendering; ignore it. (If a map looks "empty", check you're viewing the map's
  coordinates — a small extent is easy to miss.)
- **swissTLM3D `OBJEKTART` is an integer code, not a German string** (`vector.py` maps the documented
  codes per layer; verify with `ogrinfo -sql "SELECT DISTINCT OBJEKTART FROM <layer>"`). The GDB does
  **not** carry a coded-value domain for `OBJEKTART`, so the integer→label table comes from the
  swisstopo *Objektkatalog swissTLM3D* PDF.
- **splitter requires osmosis-ordered OSM**: node ids ascending, and all nodes before any way, or
  it dies with "Node ids are not sorted". `vector._IdAllocator` counts up (positive ids, separate
  node/way counters) and `build_features_osm` streams nodes and ways to separate temp files then
  concatenates (nodes block, then ways). splitter is auto-downloaded + SHA-pinned like mkgmap.
- **splitter must emit `--output=o5m`, not pbf**: PBF packs entities into fixed fileblocks with a
  per-block entity/size cap. Dense `--contours` data (very long ways with huge node arrays — e.g.
  whole-Switzerland 20 m contours, ~124 M nodes) overflows a block and splitter aborts with
  `java.lang.Error: This file has too many entities in a block. Parsers will reject it.` o5m is a
  flat streaming format with no block limit and mkgmap reads it natively (`splitter.py` globs
  `*.o5m`). A single small tile happens to fit a PBF block, so this only bites at large extents.
- **The map is built as a transparent overlay** (`mkgmap --transparent --draw-priority=30`, default;
  `--opaque` to disable). Without it, the Garmin detail tile's opaque background paints over whatever
  map is underneath, so on-device the base map's roads/buildings flash then vanish under a solid
  layer (very visible on a Fenix with a contour-only map). `--transparent` marks it an overlay so
  lower maps show through where this one has no fill; `--draw-priority` (>mkgmap's default 25) keeps
  the topo layer on top. `mkgmap.build_img(..., transparent=...)` threads it; `pipeline.build` passes
  `transparent=not --opaque`.
- **mkgmap/splitter are heap-bound on country-scale maps**: with the default JVM heap mkgmap warns
  ("consider increasing … -Xmx") and sets `max-jobs` to 1, so the run is single-threaded and slow
  (the map still builds correctly). `--max-heap <size>` (e.g. `8g`) injects `-Xmx` before `-jar` in
  both java invocations (`mkgmap.build_img_cmd` / `splitter.split_cmd`); it's opt-in (default: no
  `-Xmx`, JVM picks ~¼ RAM) so small builds are unaffected. `mkgmap.build_img` surfaces mkgmap's
  WARNING/SEVERE/ERROR lines to our log on success (they'd otherwise be swallowed with its output).
- **Filled water areas come from `TLM_BODENBEDECKUNG` polygons** (`OBJEKTART IN (5,10)` = river surface
  + lake), *not* `TLM_STEHENDES_GEWAESSER`, whose features are unclosed shoreline **lines** that can't
  fill as area. `vector.LAYER_WHERE` filters land cover to water at export time.
- **Garmin stores contour elevations in *feet*, so the style must emit feet, not metres.** The
  `.IMG` format records a contour line's elevation in feet and the device converts that number to
  the user's display unit. Writing the raw metre value makes a metric watch read e.g. a 2000 m line
  as 2000 ft and show ~610 m — the "metres interpreted as feet" bug. The contour name rule in
  `styles/topovert/lines` therefore uses `name '${ele|conv:m=>ft}'`. The OSM `ele` tag and the
  `render-contours` SVG stay in metres (ground truth); only the on-device label is converted.
- **`gdal_contour` traces lines straight through DEM voids/NoData** (tile gaps, area outside an
  irregular DEM footprint), producing long spurious lines across the map. `gdal_tools.band_nodata`
  reads the source's NoData via `gdalinfo -json` and `contour.gdal_contour_cmd` passes it as
  `-snodata` to mask them. Sources that declare no NoData get no `-snodata` (nothing to mask).
- **`render-contours` is the cheap contour check** (`contour_render.py`): it runs the same
  `gdal_contour → ogr2ogr` steps as the build and renders the WGS84 line geometry + index-line
  elevation labels to a standalone SVG. Use it to spot bad geometry or wrong spacing without
  building/loading an `.IMG`; it can also render a kept `contours.geojsonl` (`--from-geojson`, no
  GDAL). It renders *our* data (correct metres), so it can't reveal device-side unit bugs — that's
  what the `conv:m=>ft` rule above is for.
- **The Swiss rendering is a mkgmap style + a text TYP** (`src/topovert/styles/`). The style's
  `lines`/`points`/`polygons` map the OSM tags `vector.py` emits to Garmin type codes + resolutions;
  `topovert_typ.txt` recolours the topo-relevant types (land cover, paths, watercourses). mkgmap
  compiles the **text** TYP only if it's passed as an input file with a `.txt` extension, *and* its
  `FID`/`ProductCode` match `--family-id`/`--product-id` (6324/1) or the device silently ignores it
  (`test_style.py` guards this). Type codes left out of the TYP fall back to Garmin's default look.
  Editing rules is data-only; `test_style.py` asserts every emitted tag still has a matching rule.
  `topovert legend` (`legend.py`) renders a swatch-per-element legend from the TYP + style rules and
  hot-reloads as you edit — the fast loop for tuning colours/widths. Like `render-contours` it draws
  *our* styling (what we ask mkgmap to draw), so it can't reveal device-side quirks: there's still
  no automated substitute for the final visual check — load the result in QMapShack/on a device.

<!-- BEGIN BEADS INTEGRATION v:1 profile:minimal hash:7510c1e2 -->
## Beads Issue Tracker

This project uses **bd (beads)** for issue tracking. Run `bd prime` to see full workflow context and commands.

### Quick Reference

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --claim  # Claim work
bd close <id>         # Complete work
```

### Rules

- Use `bd` for ALL task tracking — do NOT use TodoWrite, TaskCreate, or markdown TODO lists
- Run `bd prime` for detailed command reference and session close protocol
- Use `bd remember` for persistent knowledge — do NOT use MEMORY.md files

**Architecture in one line:** issues live in a local Dolt DB; sync uses `refs/dolt/data` on your git remote; `.beads/issues.jsonl` is a passive export. See https://github.com/gastownhall/beads/blob/main/docs/SYNC_CONCEPTS.md for details and anti-patterns.

## Session Completion

**When ending a work session**, you MUST complete ALL steps below. Work is NOT complete until `git push` succeeds.

**MANDATORY WORKFLOW:**

1. **File issues for remaining work** - Create issues for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **PUSH TO REMOTE** - This is MANDATORY:
   ```bash
   git pull --rebase
   git push
   git status  # MUST show "up to date with origin"
   ```
5. **Clean up** - Clear stashes, prune remote branches
6. **Verify** - All changes committed AND pushed
7. **Hand off** - Provide context for next session

**CRITICAL RULES:**
- Work is NOT complete until `git push` succeeds
- NEVER stop before pushing - that leaves work stranded locally
- NEVER say "ready to push when you are" - YOU must push
- If push fails, resolve and retry until it succeeds
<!-- END BEADS INTEGRATION -->
