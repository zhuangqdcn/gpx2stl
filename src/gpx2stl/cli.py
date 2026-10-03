from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
from typing import Any

from dotenv import find_dotenv, load_dotenv

from gpx2stl.dem import iter_local_geotiffs
from gpx2stl.errors import Gpx2StlError
from gpx2stl.models import Config
from gpx2stl.pipeline import convert

SETTING_KEYS = {
    "gpx_file",
    "output",
    "route_width",
    "route_height",
    "topo",
    "boundary_percent",
    "shape",
    "text",
    "text_height",
    "inner_size_percent",
    "font_file",
    "base_stl",
    "use_3mf",
    "max_size",
    "terrain_height",
    "base_height",
    "topo_source",
    "topo_file",
    "topo_dir",
    "dem_type",
    "api_key",
    "force",
}


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


def _percentage_below_100(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0 or parsed >= 100:
        raise argparse.ArgumentTypeError("must be a finite number greater than zero and below 100")
    return parsed


def find_settings(start: Path | None = None) -> Path | None:
    directory = (start or Path.cwd()).resolve()
    for candidate_dir in (directory, *directory.parents):
        candidate = candidate_dir / "settings.json"
        if candidate.is_file():
            return candidate
    return None


def load_settings(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise Gpx2StlError(f"Unable to read settings file '{path}': {exc}") from exc
    except json.JSONDecodeError as exc:
        raise Gpx2StlError(
            f"Invalid JSON in settings file '{path}' at line {exc.lineno}, "
            f"column {exc.colno}: {exc.msg}"
        ) from exc
    if not isinstance(value, dict):
        raise Gpx2StlError(f"Settings file '{path}' must contain a JSON object.")
    unknown = sorted(set(value) - SETTING_KEYS)
    if unknown:
        raise Gpx2StlError(
            f"Unknown setting{'s' if len(unknown) != 1 else ''} in '{path}': "
            f"{', '.join(unknown)}"
        )
    for name in (
        "gpx_file",
        "output",
        "topo_file",
        "topo_dir",
        "font_file",
        "base_stl",
    ):
        setting = value.get(name)
        if isinstance(setting, str):
            setting_path = Path(setting)
            if not setting_path.is_absolute():
                value[name] = str((path.parent / setting_path).resolve())
    return value


def create_parser(settings: dict[str, Any] | None = None) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gpx2stl",
        description="Convert GPX tracks and routes into printable terrain models.",
    )
    parser.add_argument("gpx_file", nargs="?", type=Path, help="input GPX file")
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
        help="include terrain (default: enabled)",
    )
    parser.add_argument(
        "--boundary-percent",
        type=_nonnegative,
        default=10.0,
        help="footprint padding percentage (default: 10)",
    )
    parser.add_argument(
        "--shape",
        choices=("square", "circle", "hex"),
        default="square",
        help="base/terrain footprint (default: square)",
    )
    parser.add_argument(
        "--text",
        help="raised text following a top arc around a centered terrain circle",
    )
    parser.add_argument(
        "--text-height",
        type=_positive,
        default=1.0,
        help="raised text thickness in mm (default: 1)",
    )
    parser.add_argument(
        "--inner-size-percent",
        type=_percentage_below_100,
        default=70.0,
        help="terrain circle diameter as a percentage of model width (default: 70)",
    )
    parser.add_argument(
        "--font-file",
        type=Path,
        help="custom .ttf, .otf, or .ttc font for text glyphs",
    )
    parser.add_argument(
        "--base-stl",
        type=Path,
        help="preserve an STL base and place terrain on its highest flat top",
    )
    parser.add_argument(
        "--3mf",
        dest="use_3mf",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="write multi-material 3MF instead of STL (default: enabled)",
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
        default=None,
        help="terrain relief range in mm (default: same scale as --max-size)",
    )
    parser.add_argument(
        "--base-height",
        type=_positive,
        default=2.0,
        help="base thickness in mm (default: 2)",
    )
    parser.add_argument(
        "--topo-source",
        choices=("auto", "online", "local"),
        default="auto",
        help="terrain source: local first, OpenTopography, or local only (default: auto)",
    )
    parser.add_argument(
        "--topo-file",
        type=Path,
        help="one local GeoTIFF; takes precedence over --topo-dir",
    )
    parser.add_argument(
        "--topo-dir",
        type=Path,
        help="directory recursively containing local GeoTIFF tiles (default: ./asset)",
    )
    parser.add_argument("--dem-type", help="OpenTopography DEM identifier")
    parser.add_argument("--api-key", help="OpenTopography API key")
    parser.add_argument(
        "--force",
        action="store_true",
        help="overwrite an existing output file",
    )
    parser.set_defaults(**(settings or {}))
    return parser


def config_from_args(args: argparse.Namespace, parser: argparse.ArgumentParser) -> Config:
    if args.gpx_file is None:
        parser.error("gpx_file is required as an argument or settings.json value")
    _validate_setting_types(args, parser)
    gpx_file = Path(args.gpx_file).resolve()
    if not gpx_file.is_file():
        parser.error(f"GPX file does not exist or is not a file: {gpx_file}")
    suffix = ".3mf" if args.use_3mf else ".stl"
    output = Path(args.output).resolve() if args.output else gpx_file.with_suffix(suffix)
    if output.suffix.lower() != suffix:
        parser.error(f"output must use the {suffix} extension")
    if output.exists() and not args.force:
        parser.error(f"output already exists (use --force to replace it): {output}")
    if not output.parent.is_dir():
        parser.error(f"output directory does not exist: {output.parent}")
    api_key = args.api_key or os.environ.get("OPENTOPOGRAPHY_API_KEY")
    topo_file = Path(args.topo_file).resolve() if args.topo_file else None
    topo_dir = Path(args.topo_dir or "asset").resolve()
    font_file = Path(args.font_file).resolve() if args.font_file else None
    base_stl = Path(args.base_stl).resolve() if args.base_stl else None
    text = args.text.strip() if args.text is not None else None
    if args.text is not None:
        if not text:
            parser.error("--text must contain at least one non-whitespace character")
        if any(not character.isprintable() for character in text):
            parser.error("--text must contain only printable characters")
    if font_file is not None:
        if not font_file.is_file():
            parser.error(f"font file does not exist or is not a file: {font_file}")
        if font_file.suffix.lower() not in {".ttf", ".otf", ".ttc"}:
            parser.error("--font-file must use the .ttf, .otf, or .ttc extension")
    if base_stl is not None:
        if not base_stl.is_file():
            parser.error(f"base STL does not exist or is not a file: {base_stl}")
        if base_stl.suffix.lower() != ".stl":
            parser.error("--base-stl must use the .stl extension")
    if args.topo:
        if args.topo_source == "online":
            if topo_file is not None or args.topo_dir is not None:
                parser.error("--topo-file and --topo-dir cannot be used with --topo-source online")
            if not api_key:
                parser.error(
                    "online topo requires --api-key or the "
                    "OPENTOPOGRAPHY_API_KEY environment variable"
                )
        if topo_file is not None:
            if not topo_file.is_file():
                parser.error(f"local topo file does not exist or is not a file: {topo_file}")
            if topo_file.suffix.lower() not in {".tif", ".tiff"}:
                parser.error("--topo-file must use the .tif or .tiff extension")
        elif args.topo_dir is not None and not topo_dir.is_dir():
            parser.error(f"local topo directory does not exist: {topo_dir}")
        if (
            args.topo_source == "local"
            and topo_file is None
            and not iter_local_geotiffs(topo_dir)
        ):
            parser.error(
                f"local topo requires at least one .tif or .tiff file in: {topo_dir}"
            )
    if base_stl is None:
        available_route_width = (
            args.max_size * args.inner_size_percent / 100.0
            if text is not None
            else args.max_size
        )
        if text is None and args.shape == "hex":
            available_route_width *= math.sqrt(3.0) / 2.0
        if args.route_width >= available_route_width:
            parser.error(
                "--route-width must be smaller than the available terrain width "
                f"({available_route_width:g} mm)"
            )
    return Config(
        gpx_file=gpx_file,
        output=output,
        route_width=args.route_width,
        route_height=args.route_height,
        topo=args.topo,
        boundary_percent=args.boundary_percent,
        shape=args.shape,
        text=text,
        text_height=args.text_height,
        inner_size_percent=args.inner_size_percent,
        font_file=font_file,
        base_stl=base_stl,
        use_3mf=args.use_3mf,
        max_size=args.max_size,
        terrain_height=args.terrain_height,
        base_height=args.base_height,
        topo_source=args.topo_source,
        topo_file=topo_file,
        topo_dir=topo_dir,
        dem_type=args.dem_type,
        api_key=api_key,
        force=args.force,
    )


def _validate_setting_types(
    args: argparse.Namespace, parser: argparse.ArgumentParser
) -> None:
    for name in (
        "gpx_file",
        "output",
        "topo_file",
        "topo_dir",
        "font_file",
        "base_stl",
    ):
        value = getattr(args, name)
        if value is not None and not isinstance(value, (str, Path)):
            parser.error(f"settings.json value '{name}' must be a path string or null")
    numeric = (
        "route_width",
        "route_height",
        "boundary_percent",
        "text_height",
        "inner_size_percent",
        "max_size",
        "base_height",
    )
    for name in numeric:
        value = getattr(args, name)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            parser.error(f"settings.json value '{name}' must be a number")
        if not math.isfinite(value):
            parser.error(f"settings.json value '{name}' must be finite")
    for name in ("topo", "use_3mf", "force"):
        if not isinstance(getattr(args, name), bool):
            parser.error(f"settings.json value '{name}' must be true or false")
    if args.route_width <= 0 or args.route_height <= 0:
        parser.error("route dimensions must be greater than zero")
    if args.boundary_percent < 0:
        parser.error("--boundary-percent must be greater than or equal to zero")
    if args.text_height <= 0:
        parser.error("--text-height must be greater than zero")
    if args.inner_size_percent <= 0 or args.inner_size_percent >= 100:
        parser.error("--inner-size-percent must be greater than zero and below 100")
    if args.terrain_height is not None:
        if isinstance(args.terrain_height, bool) or not isinstance(
            args.terrain_height, (int, float)
        ):
            parser.error("settings.json value 'terrain_height' must be a number or null")
        if not math.isfinite(args.terrain_height) or args.terrain_height <= 0:
            parser.error("--terrain-height must be a finite number greater than zero")
    if args.max_size <= 0 or args.base_height <= 0:
        parser.error("model dimensions must be greater than zero")
    if args.shape not in {"square", "circle", "hex"}:
        parser.error("settings.json value 'shape' must be 'square', 'circle', or 'hex'")
    if args.topo_source not in {"auto", "online", "local"}:
        parser.error(
            "settings.json value 'topo_source' must be 'auto', 'online', or 'local'"
        )
    for name in ("text", "dem_type", "api_key"):
        value = getattr(args, name)
        if value is not None and not isinstance(value, str):
            parser.error(f"settings.json value '{name}' must be a string or null")


def main(argv: list[str] | None = None) -> None:
    dotenv_path = find_dotenv(usecwd=True)
    if dotenv_path:
        load_dotenv(dotenv_path, override=False)
    parser = create_parser()
    try:
        settings = load_settings(find_settings())
        parser = create_parser(settings)
        config = config_from_args(parser.parse_args(argv), parser)
        convert(config)
    except Gpx2StlError as exc:
        parser.exit(1, f"gpx2stl: error: {exc}\n")
    except KeyboardInterrupt:
        parser.exit(130, "gpx2stl: interrupted\n")
