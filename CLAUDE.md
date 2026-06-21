# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What the project does

Topovert converts freely available [Swisstopo](https://www.swisstopo.admin.ch/de/digitale-karten)
data into `.IMG` files installable on Garmin navigation devices.

**v1 (current) is one vertical slice:** a local directory of swissALTI3D GeoTIFF elevation tiles
(EPSG:2056 / LV95) → a hill-shaded Garmin `.IMG`. `--tlm <swissTLM3D>` overlays vector features —
roads/paths, railways, aerialways, watercourses, land cover (water/forest/rock/glacier/wetland),
buildings, POIs, and walls. Either input is optional: `--tlm` alone makes a **vector-only** map
(no DEM), `--dem-dir` alone a hillshade-only map. Large extents (up to whole-country) are tiled
with **splitter** automatically. Contour lines and area auto-download are deferred (`bd list`).

## Stack & architecture

**Decided by a stability-and-simplicity-first rule:** a thin **Python** orchestrator (stdlib only,
zero runtime pip deps) that shells out to two mature external toolchains and writes almost no
geospatial/encoding code itself:

- **GDAL command-line tools** (`gdalbuildvrt`, `gdalwarp`, `gdal_translate`, `gdalinfo`, `ogr2ogr`) —
  reprojection, raster conversion, and vector export. We use the **CLI, not the `osgeo.gdal`
  bindings** (far easier cross-platform install, version-tolerant). This is also why the vector path
  uses `ogr2ogr → GeoJSON → our own OSM writer` rather than `ogr2pbf` (which needs the `osgeo`
  bindings) — see `bd show topovert-rn1`.
- **mkgmap** (Java) — does all Garmin `.IMG` encoding, including embedding the DEM for hillshading.
- **splitter** (Java, same source) — tiles a large `.osm` into mkgmap-sized pieces; used automatically
  when the OSM exceeds `pipeline.SPLIT_NODE_THRESHOLD` nodes.

Pipeline (`src/topovert/pipeline.py:build`): bounds come from the DEM mosaic (`gdalbuildvrt` →
`gdalinfo wgs84Extent`) or, with no DEM, from the swissTLM3D extent (`vector.tlm_bounds`). DEM path:
per-1°-tile `gdalwarp` (2056→4326, SRTM1/3 grid) + `gdal_translate -of SRTMHGT`. Vector path:
`ogr2ogr` → GeoJSON → `.osm`. Then (splitter if large →) `mkgmap --gmapsupp [--dem]` → single
loadable `gmapsupp.img`. Modules: `hgt.py` (SRTM tiling geometry — unit-tested), `gdal_tools.py`
(GDAL argv builders + exec), `osm.py` (OSM XML writer + node/way serializers), `vector.py`
(swissTLM3D → GeoJSON → OSM, data-only `TAG_MAP`, bounds), `jars.py` (Java + mkgmap/splitter
download-cache), `splitter.py`, `mkgmap.py` (the `.IMG` build), `cli.py`. The bundled mkgmap
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
- **Filled water areas come from `TLM_BODENBEDECKUNG` polygons** (`OBJEKTART IN (5,10)` = river surface
  + lake), *not* `TLM_STEHENDES_GEWAESSER`, whose features are unclosed shoreline **lines** that can't
  fill as area. `vector.LAYER_WHERE` filters land cover to water at export time.
- **The Swiss rendering is a mkgmap style + a text TYP** (`src/topovert/styles/`). The style's
  `lines`/`points`/`polygons` map the OSM tags `vector.py` emits to Garmin type codes + resolutions;
  `topovert_typ.txt` recolours the topo-relevant types (land cover, paths, watercourses). mkgmap
  compiles the **text** TYP only if it's passed as an input file with a `.txt` extension, *and* its
  `FID`/`ProductCode` match `--family-id`/`--product-id` (6324/1) or the device silently ignores it
  (`test_style.py` guards this). Type codes left out of the TYP fall back to Garmin's default look.
  Editing rules is data-only; `test_style.py` asserts every emitted tag still has a matching rule.
  No automated substitute for the visual check — load the result in QMapShack/on a device.

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
