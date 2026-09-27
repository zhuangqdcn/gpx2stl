from __future__ import annotations

import argparse
import math
import os
import sys
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

from gpx2stl.errors import Gpx2StlError
from gpx2stl.models import Config
from gpx2stl.pipeline import convert


def _positive(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be a finite number greater than zero")
    return parsed


def _nonnegative(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0:
        raise argparse.ArgumentTypeError("must be a finite number greater than or equal to zero")
    return parsed


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gpx2stl",
        description="Convert GPX tracks and routes into printable terrain models.",
    )
    parser.add_argument("gpx_file", type=Path, help="input GPX file")
    parser.add_argument("-o", "--output", type=Path, help="output .3mf or .stl path")
    parser.add_argument("--route-width", type=_positive, default=1.0, help="route width in mm")
    parser.add_argument(
        "--route-height",
        type=_positive,
        default=2.0,
        help="route height above terrain in mm (topo mode)",
    )
    parser.add_argument(
        "--topo",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="include OpenTopography terrain (default: enabled)",
    )
    parser.add_argument(
        "--boundary-percent",
        type=_nonnegative,
        default=10.0,
        help="footprint padding percentage (default: 10)",
    )
    parser.add_argument(
        "--shape",
        choices=("square", "circle"),
        default="square",
        help="base/terrain footprint (default: square)",
    )
    parser.add_argument(
        "--3mf",
        dest="use_3mf",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="write two-material 3MF instead of STL (default: enabled)",
    )
    parser.add_argument(
        "--max-size",
        type=_positive,
        default=200.0,
        help="maximum XY model dimension in mm (default: 200)",
    )
    parser.add_argument(
        "--terrain-height",
        type=_positive,
        default=20.0,
        help="terrain relief range in mm (default: 20)",
    )
    parser.add_argument(
        "--base-height",
        type=_positive,
        default=2.0,
        help="base thickness in mm (default: 2)",
    )
    parser.add_argument("--dem-type", help="OpenTopography DEM identifier")
    parser.add_argument("--api-key", help="OpenTopography API key")
    parser.add_argument(
        "--force",
        action="store_true",
        help="overwrite an existing output file",
    )
    return parser


def config_from_args(args: argparse.Namespace, parser: argparse.ArgumentParser) -> Config:
    gpx_file = args.gpx_file.resolve()
    if not gpx_file.is_file():
        parser.error(f"GPX file does not exist or is not a file: {gpx_file}")
    suffix = ".3mf" if args.use_3mf else ".stl"
    output = args.output.resolve() if args.output else gpx_file.with_suffix(suffix)
    if output.suffix.lower() != suffix:
        parser.error(f"output must use the {suffix} extension")
    if output.exists() and not args.force:
        parser.error(f"output already exists (use --force to replace it): {output}")
    if not output.parent.is_dir():
        parser.error(f"output directory does not exist: {output.parent}")
    api_key = args.api_key or os.environ.get("OPENTOPOGRAPHY_API_KEY")
    if args.topo and not api_key:
        parser.error(
            "topo mode requires --api-key or the OPENTOPOGRAPHY_API_KEY environment variable"
        )
    if args.route_width >= args.max_size:
        parser.error("--route-width must be smaller than --max-size")
    return Config(
        gpx_file=gpx_file,
        output=output,
        route_width=args.route_width,
        route_height=args.route_height,
        topo=args.topo,
        boundary_percent=args.boundary_percent,
        shape=args.shape,
        use_3mf=args.use_3mf,
        max_size=args.max_size,
        terrain_height=args.terrain_height,
        base_height=args.base_height,
        dem_type=args.dem_type,
        api_key=api_key,
        force=args.force,
    )


def main(argv: list[str] | None = None) -> None:
    dotenv_path = find_dotenv(usecwd=True)
    if dotenv_path:
        load_dotenv(dotenv_path, override=False)
    parser = create_parser()
    try:
        config = config_from_args(parser.parse_args(argv), parser)
        convert(config)
    except Gpx2StlError as exc:
        parser.exit(1, f"gpx2stl: error: {exc}\n")
    except KeyboardInterrupt:
        parser.exit(130, "gpx2stl: interrupted\n")
    print(f"Wrote {config.output}", file=sys.stdout)
