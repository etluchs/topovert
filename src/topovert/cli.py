"""Command-line entry point for topovert."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import TopovertError, __version__, contour, contour_render, legend, wahoo
from .hgt import DEFAULT_RESOLUTION, DEM_RESOLUTIONS
from .pipeline import OUTPUT_FORMATS, build


def _parse_bbox(value: str) -> tuple[float, float, float, float]:
    """Parse a ``min_lon,min_lat,max_lon,max_lat`` WGS84 bbox string."""
    try:
        parts = [float(p) for p in value.split(",")]
    except ValueError:
        raise argparse.ArgumentTypeError(f"bbox must be 4 numbers, got {value!r}")
    if len(parts) != 4:
        raise argparse.ArgumentTypeError(
            f"bbox needs 4 comma-separated numbers (min_lon,min_lat,max_lon,max_lat), "
            f"got {len(parts)}"
        )
    return tuple(parts)  # type: ignore[return-value]


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="topovert",
        description="Convert Swisstopo data into Garmin .IMG or Wahoo maps.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="enable debug logging"
    )

    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser(
        "build",
        help="build a Garmin .IMG (or Wahoo map tiles) from DEM and/or "
             "swissTLM3D sources",
    )
    b.add_argument(
        "--dem-dir", type=Path, default=None,
        help="directory of swissALTI3D GeoTIFF tiles (EPSG:2056) for hillshading; "
             "optional if --tlm or --dem-area is given",
    )
    b.add_argument(
        "--dem-area", default=None, metavar="AREA",
        help="auto-download the DEM for an area instead of --dem-dir: a named "
             "area ('switzerland') or a 'min_lon,min_lat,max_lon,max_lat' WGS84 "
             "bbox. Fetches Copernicus GLO-30 (~30 m) tiles into a cache. Mutually "
             "exclusive with --dem-dir",
    )
    b.add_argument(
        "--out", type=Path, required=True,
        help="output path: the .IMG file, or (with --format wahoo) the "
             "directory to fill with map tiles",
    )
    b.add_argument(
        "--format", dest="output_format", choices=list(OUTPUT_FORMATS),
        default="garmin",
        help="output format (default: %(default)s). 'garmin' writes one "
             "gmapsupp .IMG; 'wahoo' writes zoom-8 <x>/<y>.map.lzma tiles for "
             "an ELEMNT/BOLT/ROAM (copy them into the device's "
             "maps/tiles/8/ folder). Wahoo maps carry no hillshade, so pair it "
             "with --contours and/or a swissTLM3D source",
    )
    b.add_argument(
        "--dem-resolution", choices=sorted(DEM_RESOLUTIONS), default=DEFAULT_RESOLUTION,
        help="target HGT resolution (default: %(default)s = SRTM1/~30 m)",
    )
    b.add_argument(
        "--resampling", default="bilinear",
        help="gdalwarp resampling method (default: %(default)s; 'average' is "
             "often better for large downsampling)",
    )
    b.add_argument(
        "--source-epsg", type=int, default=None,
        help="EPSG of the --dem-dir/--tlm sources (default: auto-detect each "
             "from its own CRS). Set only to override sources lacking CRS "
             "metadata; it then applies to both inputs, so they must share a CRS",
    )
    b.add_argument("--name", default="topovert", help="map name/description")
    b.add_argument(
        "--tlm", type=Path, default=None,
        help="optional swissTLM3D GeoDatabase (.gdb) to add vector features "
             "(roads, water, buildings); omit for a hillshade-only map",
    )
    b.add_argument(
        "--tlm-release", default=None, metavar="RELEASE",
        help="auto-download swissTLM3D from swisstopo instead of --tlm: "
             "'latest' or a release id (e.g. swisstlm3d_2025-03). This is a "
             "~2.9 GB national download (expands to ~10 GB) cached under "
             "~/.cache/topovert. Mutually exclusive with --tlm",
    )
    b.add_argument(
        "--tlm-layer", dest="tlm_layers", action="append", default=None,
        metavar="LAYER",
        help="swissTLM3D layer to include (repeatable); overrides the default "
             "core-nav set when given",
    )
    b.add_argument(
        "--contours", action="store_true",
        help="add elevation contour lines derived from the DEM (requires "
             "--dem-dir); every 5th line is a bold index contour",
    )
    b.add_argument(
        "--contour-interval", type=int, default=contour.DEFAULT_INTERVAL,
        metavar="M",
        help="contour spacing in metres (default: %(default)s)",
    )
    b.add_argument(
        "--no-hillshade", dest="no_hillshade", action="store_true",
        help="don't embed the DEM for shaded relief (much smaller .IMG); the DEM "
             "is still used for bounds and --contours. Pair with --contours for a "
             "lightweight contour-only map. Implied by --format wahoo",
    )
    b.add_argument(
        "--max-nodes", type=int, default=None,
        help="splitter tile size in OSM nodes for large extents (default: "
             "splitter's own ~1.6M); only used when the map needs tiling "
             "(Garmin output only — Wahoo tiles on its own zoom-8 grid)",
    )
    b.add_argument(
        "--max-heap", default=None, metavar="SIZE",
        help="JVM -Xmx for mkgmap/splitter (e.g. '8g', '512m'). Big "
             "(country-scale) builds otherwise warn and throttle to a single "
             "job under the default heap; raise it to parallelise and speed "
             "them up",
    )
    b.add_argument(
        "--opaque", action="store_true",
        help="build a standalone (non-transparent) map. By default the map is "
             "transparent so it overlays the device's base map (roads/towns "
             "show through); use this only if topovert is your sole base map. "
             "Garmin output only: a Wahoo map always replaces the device's tile",
    )
    b.add_argument(
        "--work-dir", type=Path, default=None,
        help="directory for (large) intermediates; defaults next to --out. Point "
             "it at a roomy filesystem for whole-country builds (avoid small tmpfs)",
    )
    b.add_argument(
        "--keep-intermediate", action="store_true",
        help="keep the temp workdir (VRT, HGT tiles, OSM, GeoJSON) for inspection",
    )
    b.set_defaults(func=_cmd_build)

    # --- render-contours: eyeball a patch's contours as an SVG ----------------
    r = sub.add_parser(
        "render-contours",
        help="render contour lines for a patch to an SVG (for checking geometry "
             "+ elevation labels without building/loading an .IMG)",
    )
    r.add_argument(
        "--dem-dir", type=Path, default=None,
        help="directory of DEM GeoTIFF tiles to derive contours from",
    )
    r.add_argument(
        "--dem-area", default=None, metavar="AREA",
        help="auto-download the DEM for an area instead of --dem-dir (named area "
             "or WGS84 bbox); pair with a small --bbox to keep it fast",
    )
    r.add_argument(
        "--from-geojson", type=Path, default=None, metavar="FILE",
        help="render an existing GeoJSONSeq of contours (e.g. a kept "
             "contours.geojsonl) instead of running GDAL",
    )
    r.add_argument("--out", type=Path, required=True, help="output .svg path")
    r.add_argument(
        "--bbox", type=_parse_bbox, default=None,
        metavar="min_lon,min_lat,max_lon,max_lat",
        help="WGS84 patch to crop the DEM to (and the SVG viewport); recommended "
             "for large DEM sources",
    )
    r.add_argument(
        "--contour-interval", type=int, default=contour.DEFAULT_INTERVAL,
        metavar="M", help="contour spacing in metres (default: %(default)s)",
    )
    r.add_argument(
        "--source-epsg", type=int, default=None,
        help="EPSG of the DEM source (default: auto-detect from its CRS)",
    )
    r.add_argument(
        "--width", type=int, default=1000, metavar="PX",
        help="SVG image width in pixels (default: %(default)s)",
    )
    r.add_argument(
        "--keep-intermediate", action="store_true",
        help="keep the temp workdir (VRT, clip, GPKG, GeoJSON) for inspection",
    )
    r.set_defaults(func=_cmd_render_contours)

    # --- legend: browse the style + TYP as a hot-reloading web legend ---------
    lg = sub.add_parser(
        "legend",
        help="preview the bundled style + TYP as a browser legend (swatch per "
             "element); serves with hot reload, or writes a static HTML file",
    )
    lg.add_argument(
        "--out", type=Path, default=None,
        help="write a standalone legend.html and exit instead of serving "
             "(no hot reload); open it in a browser",
    )
    lg.add_argument(
        "--port", type=int, default=8000,
        help="port for the hot-reload server (default: %(default)s)",
    )
    lg.add_argument(
        "--host", default="127.0.0.1",
        help="host/interface to bind the server to (default: %(default)s)",
    )
    lg.add_argument(
        "--no-browser", dest="no_browser", action="store_true",
        help="don't try to open a web browser when serving",
    )
    lg.set_defaults(func=_cmd_legend)
    return parser


def _cmd_build(args: argparse.Namespace) -> int:
    result = build(
        args.dem_dir,
        args.out,
        dem_area=args.dem_area,
        resolution=args.dem_resolution,
        resampling=args.resampling,
        source_epsg=args.source_epsg,
        map_name=args.name,
        output_format=args.output_format,
        tlm_path=args.tlm,
        tlm_release=args.tlm_release,
        tlm_layers=args.tlm_layers,
        contours=args.contours,
        contour_interval=args.contour_interval,
        hillshade=not args.no_hillshade,
        **({"max_nodes": args.max_nodes} if args.max_nodes else {}),
        max_heap=args.max_heap,
        transparent=not args.opaque,
        work_dir=args.work_dir,
        keep_intermediate=args.keep_intermediate,
    )
    feats = []
    if result.tiles:
        feats.append(f"hillshade, {len(result.tiles)} DEM tile(s)")
    if args.contours:
        feats.append("contours")
    if args.tlm or args.tlm_release:
        feats.append("vector features")
    summary = "; ".join(feats) if feats else "bounds only"
    if result.map_tiles:
        print(
            f"Wrote {len(result.map_tiles)} Wahoo tile(s) under {result.out_path} "
            f"({summary}): {', '.join(result.map_tiles)}.\n"
            "Copy the <x>/ folders into the device's maps/tiles/8/ folder, and "
            f"install {wahoo.THEME_FILE.name} as the device's render theme "
            "(see the README) or the map will not be drawn."
        )
    else:
        print(f"Wrote {result.out_path} ({summary}).")
    return 0


def _cmd_render_contours(args: argparse.Namespace) -> int:
    out = contour_render.render_patch(
        args.out,
        dem_dir=args.dem_dir,
        dem_area=args.dem_area,
        from_geojson=args.from_geojson,
        bbox=args.bbox,
        interval=args.contour_interval,
        source_epsg=args.source_epsg,
        width=args.width,
        keep_intermediate=args.keep_intermediate,
    )
    print(f"Wrote {out} (open it in a browser to check the contours).")
    return 0


def _cmd_legend(args: argparse.Namespace) -> int:
    if args.out is not None:
        out = legend.render_legend(args.out)
        print(f"Wrote {out} (open it in a browser to view the style legend).")
        return 0
    legend.serve(
        host=args.host,
        port=args.port,
        open_browser=not args.no_browser,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )
    try:
        return args.func(args)
    except TopovertError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
