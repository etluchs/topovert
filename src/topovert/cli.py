"""Command-line entry point for topovert."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import TopovertError, __version__
from .hgt import DEFAULT_RESOLUTION, DEM_RESOLUTIONS
from .pipeline import DEFAULT_SOURCE_EPSG, build


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="topovert",
        description="Convert Swisstopo data into Garmin .IMG maps.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="enable debug logging"
    )

    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser(
        "build",
        help="build a hill-shaded .IMG from a directory of DEM GeoTIFFs",
    )
    b.add_argument(
        "--dem-dir", type=Path, default=None,
        help="directory of swissALTI3D GeoTIFF tiles (EPSG:2056) for hillshading; "
             "optional if --tlm is given",
    )
    b.add_argument(
        "--out", type=Path, required=True, help="output .IMG path",
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
        "--source-epsg", type=int, default=DEFAULT_SOURCE_EPSG,
        help="EPSG code of the input DEM (default: %(default)s = Swiss LV95)",
    )
    b.add_argument("--name", default="topovert", help="map name/description")
    b.add_argument(
        "--tlm", type=Path, default=None,
        help="optional swissTLM3D GeoPackage (.gpkg) to add vector features "
             "(roads, water, buildings); omit for a hillshade-only map",
    )
    b.add_argument(
        "--tlm-layer", dest="tlm_layers", action="append", default=None,
        metavar="LAYER",
        help="swissTLM3D layer to include (repeatable); overrides the default "
             "core-nav set when given",
    )
    b.add_argument(
        "--max-nodes", type=int, default=None,
        help="splitter tile size in OSM nodes for large extents (default: "
             "splitter's own ~1.6M); only used when the map needs tiling",
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
    return parser


def _cmd_build(args: argparse.Namespace) -> int:
    result = build(
        args.dem_dir,
        args.out,
        resolution=args.dem_resolution,
        resampling=args.resampling,
        source_epsg=args.source_epsg,
        map_name=args.name,
        tlm_path=args.tlm,
        tlm_layers=args.tlm_layers,
        **({"max_nodes": args.max_nodes} if args.max_nodes else {}),
        work_dir=args.work_dir,
        keep_intermediate=args.keep_intermediate,
    )
    dem = f"{len(result.tiles)} DEM tile(s)" if result.tiles else "no DEM"
    print(f"Wrote {result.out_path} ({dem}).")
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
