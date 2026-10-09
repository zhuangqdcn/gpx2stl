from __future__ import annotations

import argparse
import json
import math
import os
import sys
from pathlib import Path
from typing import Any, NoReturn

from dotenv import find_dotenv, load_dotenv

from gpx2stl.activity import ACTIVITY_EXTENSIONS
from gpx2stl.dem import iter_local_geotiffs
from gpx2stl.errors import Gpx2StlError
from gpx2stl.models import Config, DirectionalRouteBoundary, RouteBoundaryPercent
from gpx2stl.pipeline import convert

SETTING_KEYS = {
    "gpx_file",
    "output",
    "mode",
    "route_width",
    "route_height",
    "route_depth",
    "road_snap_distance",
    "topo",
    "route_boundary_percent",
    "auto_boundary_max_distance_km",
    "auto_valley_max_relief_m",
    "auto_valley_max_slope_percent",
    "auto_valley_max_height_m",
    "auto_valley_max_height_percent",
    "text_boundary_percent",
    "shape",
    "text",
    "text_height",
    "text_margin",
    "text_end_gap",
    "text_align",
    "text_mode",
    "text_depth",
    "font_family",
    "font_file",
    "font_size",
    "font_weight",
    "font_style",
    "base_stl",
    "use_3mf",
    "max_size",
    "terrain_height",
    "base_height",
    "topo_source",
    "topo_file",
    "topo_file_windows",
    "topo_file_linux",
    "topo_dir",
    "topo_dir_windows",
    "topo_dir_linux",
    "city_dir",
    "building_default_height",
    "building_height_scale",
    "water_depth",
    "dem_type",
    "api_key",
    "force",
}
SETTINGS_FILENAME = ".gpx2stl.settings.json"
PLATFORM_TOPO_KEYS = (
    "topo_file_windows",
    "topo_file_linux",
    "topo_dir_windows",
    "topo_dir_linux",
)
RETIRED_SETTINGS = {
    "boundary_percent": "use route_boundary_percent for route padding and "
    "text_boundary_percent for the text band",
    "inner_size_percent": "use text_boundary_percent = (100 - inner_size_percent) / 2",
}


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        for name, guidance in RETIRED_SETTINGS.items():
            option = "--" + name.replace("_", "-")
            if option in message:
                message += f"; {option} is retired: {guidance}"
        super().error(message)


class _ValleyThresholdAction(argparse.Action):
    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: Any,
        option_string: str | None = None,
    ) -> None:
        setattr(namespace, self.dest, values)
        namespace._valley_threshold_overrides = (
            namespace._valley_threshold_overrides | {self.dest}
        )


class _RouteBoundaryAction(argparse.Action):
    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: Any,
        option_string: str | None = None,
    ) -> None:
        setattr(namespace, self.dest, values)
        namespace._route_boundary_cli_explicit = True


def _reject_retired_settings(settings: dict[str, Any]) -> None:
    for name, guidance in RETIRED_SETTINGS.items():
        if name in settings:
            raise Gpx2StlError(f"Setting '{name}' is retired: {guidance}.")


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


def _route_boundary_percentage(value: str) -> RouteBoundaryPercent:
    if value == "auto":
        return "auto"
    if "," in value:
        parts = value.split(",")
        if len(parts) != 4 or any(not part.strip() for part in parts):
            raise argparse.ArgumentTypeError(
                "must be 'auto', one nonnegative number, or four comma-separated "
                "N,E,S,W percentages"
            )
        parsed = [_nonnegative(part.strip()) for part in parts]
        return parsed[0], parsed[1], parsed[2], parsed[3]
    return _nonnegative(value)


def _directional_route_boundary(
    value: Any,
) -> DirectionalRouteBoundary | None:
    if not isinstance(value, (list, tuple)) or isinstance(value, (str, bytes)):
        return None
    if len(value) != 4:
        return None
    normalized: list[float] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            return None
        number = float(item)
        if not math.isfinite(number) or number < 0:
            return None
        normalized.append(number)
    return normalized[0], normalized[1], normalized[2], normalized[3]


def _valley_height_percentage(value: str) -> float:
    parsed = _nonnegative(value)
    if parsed > 100:
        raise argparse.ArgumentTypeError("must be less than or equal to 100")
    return parsed


def _text_boundary_percentage(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0 or parsed >= 50:
        raise argparse.ArgumentTypeError(
            "must be a finite number greater than or equal to zero and below 50"
        )
    return parsed


def find_settings(start: Path | None = None) -> Path | None:
    directory = (start or Path.cwd()).resolve()
    for candidate_dir in (directory, *directory.parents):
        candidate = candidate_dir / SETTINGS_FILENAME
        if candidate.is_file():
            return candidate
    return None


def _select_platform_topo_settings(
    settings: dict[str, Any],
    platform: str,
) -> dict[str, Any]:
    selected = settings.copy()
    for name in PLATFORM_TOPO_KEYS:
        value = selected.get(name)
        if value is not None and not isinstance(value, str):
            raise Gpx2StlError(
                f"Settings file value '{name}' must be a path string or null."
            )
    platform_suffix = (
        "windows"
        if platform.startswith("win")
        else "linux"
        if platform.startswith("linux")
        else None
    )
    if platform_suffix is not None:
        for generic_name in ("topo_file", "topo_dir"):
            platform_value = selected.get(f"{generic_name}_{platform_suffix}")
            if platform_value is not None:
                selected[generic_name] = platform_value
    for name in PLATFORM_TOPO_KEYS:
        selected.pop(name, None)
    return selected


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
    _reject_retired_settings(value)
    unknown = sorted(set(value) - SETTING_KEYS)
    if unknown:
        raise Gpx2StlError(
            f"Unknown setting{'s' if len(unknown) != 1 else ''} in '{path}': "
            f"{', '.join(unknown)}"
        )
    value = _select_platform_topo_settings(value, sys.platform)
    for name in (
        "gpx_file",
        "output",
        "topo_file",
        "topo_dir",
        "city_dir",
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
    _reject_retired_settings(settings or {})
    parser = _ArgumentParser(
        prog="gpx2stl",
        description="Convert GPX or Garmin FIT activities into printable terrain models.",
    )
    parser.add_argument(
        "gpx_file",
        metavar="input_path",
        nargs="?",
        type=Path,
        help="input .gpx/.fit file or directory containing activity files",
    )
    parser.add_argument(
        "--settings",
        type=Path,
        help=f"settings file path (default: discover {SETTINGS_FILENAME})",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="output file, or output directory for an activity directory input",
    )
    parser.add_argument(
        "--mode",
        choices=("topo", "city"),
        default="topo",
        help="model mode: terrain route or terrain with buildings (default: topo)",
    )
    parser.add_argument(
        "--route-width",
        type=_positive,
        default=None,
        help="route width in mm (default: 0.5 in city mode; 1 otherwise)",
    )
    parser.add_argument(
        "--route-height",
        type=_positive,
        default=None,
        help="route height above terrain in mm (default: 1.5 in city mode; 2 otherwise)",
    )
    parser.add_argument(
        "--route-depth",
        type=_positive,
        default=None,
        help="flush route inlay depth in mm (default: 1.5 in city mode; 0.6 otherwise)",
    )
    parser.add_argument(
        "--road-snap-distance",
        type=_nonnegative,
        default=5.0,
        help="maximum OSM road matching distance in source meters; 0 disables matching "
        "(city mode; default: 5)",
    )
    parser.add_argument(
        "--topo",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="include terrain (default: enabled)",
    )
    parser.add_argument(
        "--route-boundary-percent",
        action=_RouteBoundaryAction,
        type=_route_boundary_percentage,
        default=None,
        help="one route padding percent, N,E,S,W comma-separated percentages, "
        "or auto mountain extent (default: 10,10,10,10 in city mode; "
        "auto with topo; 10 without topo)",
    )
    parser.add_argument(
        "--auto-boundary-max-distance-km",
        type=_positive,
        default=10.0,
        help="maximum auto discovery expansion beyond each route side in km (default: 10)",
    )
    parser.add_argument(
        "--auto-valley-max-relief-m",
        action=_ValleyThresholdAction,
        type=_nonnegative,
        default=1000.0,
        help="maximum smoothed local variation over about 900 m for auto valleys in m "
        "(default: 1000; increasing loosens detection)",
    )
    parser.add_argument(
        "--auto-valley-max-slope-percent",
        action=_ValleyThresholdAction,
        type=_nonnegative,
        default=100.0,
        help="maximum auto valley slope in percent (default: 100; increasing loosens detection)",
    )
    parser.add_argument(
        "--auto-valley-max-height-m",
        action=_ValleyThresholdAction,
        type=_nonnegative,
        default=1000.0,
        help="maximum auto valley height above the window floor in m, capped by "
        "--auto-valley-max-height-percent of window relief "
        "(default: 1000; increasing loosens detection)",
    )
    parser.add_argument(
        "--auto-valley-max-height-percent",
        action=_ValleyThresholdAction,
        type=_valley_height_percentage,
        default=100.0,
        help="maximum auto valley height above the window floor as percent of window relief, "
        "capped by --auto-valley-max-height-m (0 to 100; default: 100; increasing loosens detection)",
    )
    parser.add_argument(
        "--shape",
        choices=("square", "circle", "hex"),
        default="square",
        help="base/terrain footprint (default: square)",
    )
    parser.add_argument(
        "--text",
        help="raised text on the generated frame or custom-base perimeter",
    )
    parser.add_argument(
        "--text-height",
        type=_positive,
        default=1.0,
        help="raised text thickness in mm (default: 1)",
    )
    parser.add_argument(
        "--text-margin",
        type=_nonnegative,
        default=None,
        help="minimum text clearance from frame boundaries in mm (default: automatic)",
    )
    parser.add_argument(
        "--text-end-gap",
        type=_nonnegative,
        default=0.0,
        help="extra bottom seam gap between text ends in mm (default: 0)",
    )
    parser.add_argument(
        "--text-align",
        choices=("left", "center", "right"),
        default="center",
        help="compact text-run alignment relative to the bottom seam (default: center)",
    )
    parser.add_argument(
        "--text-mode",
        choices=("raised", "embedded"),
        default="raised",
        help="raised text or a flush 3MF inlay (default: raised)",
    )
    parser.add_argument(
        "--text-depth",
        type=_positive,
        default=0.6,
        help="embedded text depth in mm (default: 0.6)",
    )
    parser.add_argument(
        "--text-boundary-percent",
        type=_text_boundary_percentage,
        default=7.0,
        help="per-side text band inset in percent; ignored without text (default: 7)",
    )
    parser.add_argument(
        "--font-family",
        default="DejaVu Sans",
        help="installed font family for text (default: DejaVu Sans)",
    )
    parser.add_argument(
        "--font-file",
        type=Path,
        help="custom .ttf, .otf, or .ttc font for text glyphs",
    )
    parser.add_argument(
        "--font-size",
        type=_positive,
        default=None,
        help="glyph height in mm (default: automatically fit)",
    )
    parser.add_argument(
        "--font-weight",
        choices=("normal", "bold"),
        default="normal",
        help="font weight (default: normal)",
    )
    parser.add_argument(
        "--font-style",
        choices=("normal", "italic"),
        default="normal",
        help="font style (default: normal)",
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
    parser.add_argument(
        "--city-dir",
        type=Path,
        help="directory containing cached OSM city tiles (default: ./asset/city)",
    )
    parser.add_argument(
        "--building-default-height",
        type=_positive,
        default=10.0,
        help="fallback building height in source meters (city mode; default: 10)",
    )
    parser.add_argument(
        "--building-height-scale",
        type=_positive,
        default=5.0,
        help="building height multiplier after map scaling (city mode; default: 5)",
    )
    parser.add_argument(
        "--water-depth",
        type=_positive,
        default=0.4,
        help="flush water inlay depth in mm (city mode; default: 0.4)",
    )
    parser.add_argument("--dem-type", help="OpenTopography DEM identifier")
    parser.add_argument("--api-key", help="OpenTopography API key")
    parser.add_argument(
        "--force",
        action="store_true",
        help="overwrite an existing output file",
    )
    parser.set_defaults(**(settings or {}))
    parser.set_defaults(_route_boundary_explicit="route_boundary_percent" in (settings or {}))
    parser.set_defaults(
        _route_boundary_cli_explicit=False,
        _route_boundary_setting_value=(settings or {}).get("route_boundary_percent"),
        _valley_threshold_overrides=frozenset(),
        _valley_threshold_settings={
            name: value
            for name, value in (settings or {}).items()
            if name.startswith("auto_valley_")
        },
    )
    return parser


def _resolve_route_dimensions(args: argparse.Namespace) -> None:
    if args.route_width is None:
        args.route_width = 0.5 if args.mode == "city" else 1.0
    if args.route_height is None:
        args.route_height = 1.5 if args.mode == "city" else 2.0
    if args.route_depth is None:
        args.route_depth = 1.5 if args.mode == "city" else 0.6


def config_from_args(args: argparse.Namespace, parser: argparse.ArgumentParser) -> Config:
    if args.gpx_file is None:
        parser.error(
            f"input_path is required as an argument or {SETTINGS_FILENAME} value"
        )
    _resolve_route_dimensions(args)
    _validate_setting_types(args, parser)
    gpx_file = Path(args.gpx_file).resolve()
    if not gpx_file.is_file():
        parser.error(f"activity file does not exist or is not a file: {gpx_file}")
    if gpx_file.suffix.lower() not in ACTIVITY_EXTENSIONS:
        parser.error("activity file must use the .gpx or .fit extension")
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
    city_dir = Path(args.city_dir or Path("asset") / "city").resolve()
    font_file = Path(args.font_file).resolve() if args.font_file else None
    base_stl = Path(args.base_stl).resolve() if args.base_stl else None
    text = args.text
    if not args.font_family.strip():
        parser.error("--font-family must contain a non-whitespace name")
    if args.text is not None:
        if not text.strip():
            parser.error("--text must contain at least one non-whitespace character")
        if any(not character.isprintable() for character in text):
            parser.error("--text must contain only printable characters")
    if font_file is not None:
        if not font_file.is_file():
            parser.error(f"font file does not exist or is not a file: {font_file}")
        if font_file.suffix.lower() not in {".ttf", ".otf", ".ttc"}:
            parser.error("--font-file must use the .ttf, .otf, or .ttc extension")
        if (
            args.font_family != "DejaVu Sans"
            or args.font_weight != "normal"
            or args.font_style != "normal"
        ):
            parser.error(
                "--font-file cannot be combined with --font-family, "
                "--font-weight, or --font-style"
            )
    if args.text_mode == "embedded":
        if text is None:
            parser.error("--text-mode embedded requires --text")
        if not args.use_3mf:
            parser.error("--text-mode embedded requires 3MF output")
        if base_stl is None and args.text_depth >= args.base_height:
            parser.error("--text-depth must be smaller than --base-height")
    if base_stl is not None:
        if not base_stl.is_file():
            parser.error(f"base STL does not exist or is not a file: {base_stl}")
        if base_stl.suffix.lower() != ".stl":
            parser.error("--base-stl must use the .stl extension")
        if _directional_route_boundary(args.route_boundary_percent) is not None:
            parser.error(
                "directional --route-boundary-percent values are not supported "
                "with --base-stl; use one symmetric percentage"
            )
    if args.mode == "city":
        if not args.topo:
            parser.error("--mode city requires topography; enable --topo")
        if not args.use_3mf:
            parser.error("--mode city requires 3MF output")
        if base_stl is None and args.route_depth >= args.base_height:
            parser.error("--route-depth must be smaller than --base-height in city mode")
        if base_stl is None and args.water_depth >= args.base_height:
            parser.error("--water-depth must be smaller than --base-height in city mode")
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
            args.max_size * (1.0 - 2.0 * args.text_boundary_percent / 100.0)
            if text is not None
            else args.max_size
        )
        if text is None and args.shape == "hex":
            available_route_width *= math.sqrt(3.0) / 2.0
        if (
            text is not None
            and args.shape == "hex"
            and available_route_width >= args.max_size * math.sqrt(3.0) / 2.0
        ):
            parser.error(
                "The terrain circle leaves no text band inside the hex frame; "
                "increase --text-boundary-percent."
            )
        if args.route_width >= available_route_width:
            parser.error(
                "--route-width must be smaller than the available terrain width "
                f"({available_route_width:g} mm)"
            )
    directional_boundary = _directional_route_boundary(args.route_boundary_percent)
    route_boundary = (
        directional_boundary or args.route_boundary_percent
        if args.route_boundary_percent is not None
        else (
            (10.0, 10.0, 10.0, 10.0)
            if args.mode == "city" and base_stl is None
            else 10.0 if args.mode == "city"
            else "auto" if args.topo else 10.0
        )
    )
    return Config(
        gpx_file=gpx_file,
        output=output,
        mode=args.mode,
        route_width=args.route_width,
        route_height=args.route_height,
        route_depth=args.route_depth,
        road_snap_distance=args.road_snap_distance,
        topo=args.topo,
        route_boundary_percent=route_boundary,
        auto_boundary_max_distance_km=args.auto_boundary_max_distance_km,
        auto_valley_max_relief_m=args.auto_valley_max_relief_m,
        auto_valley_max_slope_percent=args.auto_valley_max_slope_percent,
        auto_valley_max_height_m=args.auto_valley_max_height_m,
        auto_valley_max_height_percent=args.auto_valley_max_height_percent,
        text_boundary_percent=args.text_boundary_percent,
        shape=args.shape,
        text=text,
        text_height=args.text_height,
        text_margin=args.text_margin,
        text_end_gap=args.text_end_gap,
        text_align=args.text_align,
        text_mode=args.text_mode,
        text_depth=args.text_depth,
        font_family=args.font_family,
        font_file=font_file,
        font_size=args.font_size,
        font_weight=args.font_weight,
        font_style=args.font_style,
        base_stl=base_stl,
        use_3mf=args.use_3mf,
        max_size=args.max_size,
        terrain_height=args.terrain_height,
        base_height=args.base_height,
        topo_source=args.topo_source,
        topo_file=topo_file,
        topo_dir=topo_dir,
        city_dir=city_dir,
        building_default_height=args.building_default_height,
        building_height_scale=args.building_height_scale,
        water_depth=args.water_depth,
        dem_type=args.dem_type,
        api_key=api_key,
        force=args.force,
    )


def configs_from_args(
    args: argparse.Namespace, parser: argparse.ArgumentParser
) -> tuple[Config, ...]:
    if args.gpx_file is None:
        parser.error(
            f"input_path is required as an argument or {SETTINGS_FILENAME} value"
        )
    _resolve_route_dimensions(args)
    _validate_setting_types(args, parser)
    activity_input = Path(args.gpx_file).resolve()
    if not activity_input.exists():
        parser.error(f"activity input does not exist: {activity_input}")
    if activity_input.is_file():
        return (config_from_args(args, parser),)
    if not activity_input.is_dir():
        parser.error(f"activity input is not a file or directory: {activity_input}")

    try:
        activity_files = tuple(
            sorted(
                (
                    path
                    for path in activity_input.iterdir()
                    if path.is_file()
                    and path.suffix.lower() in ACTIVITY_EXTENSIONS
                ),
                key=lambda path: (path.name.lower(), path.name),
            )
        )
    except OSError as exc:
        parser.error(f"unable to read activity directory '{activity_input}': {exc}")
    if not activity_files:
        parser.error(
            f"activity directory contains no .gpx or .fit files: {activity_input}"
        )

    output_dir = Path(args.output).resolve() if args.output else activity_input
    if not output_dir.is_dir():
        parser.error(
            "output must be an existing directory when the activity input is a directory: "
            f"{output_dir}"
        )
    suffix = ".3mf" if args.use_3mf else ".stl"
    output_names = [f"{activity_file.stem}{suffix}" for activity_file in activity_files]
    output_name_keys = [name.casefold() for name in output_names]
    duplicate_names = sorted(
        {
            name
            for name, key in zip(output_names, output_name_keys, strict=True)
            if output_name_keys.count(key) > 1
        }
    )
    if duplicate_names:
        parser.error(
            "activity directory contains files that map to the same output name: "
            + ", ".join(duplicate_names)
        )
    configs = []
    for activity_file, output_name in zip(
        activity_files,
        output_names,
        strict=True,
    ):
        file_args = argparse.Namespace(
            **{
                **vars(args),
                "gpx_file": activity_file,
                "output": output_dir / output_name,
            }
        )
        configs.append(config_from_args(file_args, parser))
    return tuple(configs)


def _validate_setting_types(
    args: argparse.Namespace, parser: argparse.ArgumentParser
) -> None:
    if args._route_boundary_explicit and not args._route_boundary_cli_explicit:
        configured_boundary = args._route_boundary_setting_value
        if isinstance(configured_boundary, str) and configured_boundary != "auto":
            parser.error(
                "settings file value 'route_boundary_percent' must use a JSON "
                "number, a four-number N,E,S,W array, or 'auto'"
            )
    for name, value in args._valley_threshold_settings.items():
        if name not in args._valley_threshold_overrides and (
            isinstance(value, bool) or not isinstance(value, (int, float))
        ):
            parser.error(f"settings file value '{name}' must be a number")
    for name in (
        "gpx_file",
        "output",
        "topo_file",
        "topo_dir",
        "city_dir",
        "font_file",
        "base_stl",
    ):
        value = getattr(args, name)
        if value is not None and not isinstance(value, (str, Path)):
            parser.error(f"settings file value '{name}' must be a path string or null")
    numeric = (
        "route_width",
        "route_height",
        "route_depth",
        "road_snap_distance",
        "auto_boundary_max_distance_km",
        "auto_valley_max_relief_m",
        "auto_valley_max_slope_percent",
        "auto_valley_max_height_m",
        "auto_valley_max_height_percent",
        "text_boundary_percent",
        "text_height",
        "text_depth",
        "max_size",
        "base_height",
        "building_default_height",
        "building_height_scale",
        "water_depth",
    )
    for name in numeric:
        value = getattr(args, name)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            parser.error(f"settings file value '{name}' must be a number")
        if not math.isfinite(value):
            parser.error(f"settings file value '{name}' must be finite")
    for name in ("topo", "use_3mf", "force"):
        if not isinstance(getattr(args, name), bool):
            parser.error(f"settings file value '{name}' must be true or false")
    boundary = args.route_boundary_percent
    if boundary is None:
        if args._route_boundary_explicit:
            parser.error(
                "settings file value 'route_boundary_percent' must be a number, "
                "a four-number N,E,S,W array, or 'auto'"
            )
    elif boundary == "auto":
        if not args.topo:
            parser.error("--route-boundary-percent auto requires topography; enable --topo or use a numeric percentage")
    elif isinstance(boundary, (list, tuple)):
        if _directional_route_boundary(boundary) is None:
            parser.error(
                "settings file value 'route_boundary_percent' must contain exactly "
                "four finite nonnegative numbers in N,E,S,W order"
            )
    elif isinstance(boundary, bool) or not isinstance(boundary, (int, float)):
        parser.error(
            "settings file value 'route_boundary_percent' must be a number, "
            "a four-number N,E,S,W array, or 'auto'"
        )
    elif not math.isfinite(boundary) or boundary < 0:
        parser.error("--route-boundary-percent must be a finite nonnegative number or auto")
    if args.auto_boundary_max_distance_km <= 0:
        parser.error("--auto-boundary-max-distance-km must be greater than zero")
    for name in (
        "auto_valley_max_relief_m",
        "auto_valley_max_slope_percent",
        "auto_valley_max_height_m",
        "auto_valley_max_height_percent",
    ):
        if getattr(args, name) < 0:
            parser.error(f"--{name.replace('_', '-')} must be greater than or equal to zero")
    if args.auto_valley_max_height_percent > 100:
        parser.error("--auto-valley-max-height-percent must be less than or equal to 100")
    if args.route_width <= 0 or args.route_height <= 0 or args.route_depth <= 0:
        parser.error("route dimensions must be greater than zero")
    if args.road_snap_distance < 0:
        parser.error("--road-snap-distance must be greater than or equal to zero")
    if args.building_default_height <= 0 or args.building_height_scale <= 0:
        parser.error("building dimensions must be greater than zero")
    if args.water_depth <= 0:
        parser.error("--water-depth must be greater than zero")
    if args.text_height <= 0:
        parser.error("--text-height must be greater than zero")
    if args.text_depth <= 0:
        parser.error("--text-depth must be greater than zero")
    if args.font_size is not None:
        if isinstance(args.font_size, bool) or not isinstance(
            args.font_size, (int, float)
        ):
            parser.error("settings file value 'font_size' must be a number or null")
        if not math.isfinite(args.font_size) or args.font_size <= 0:
            parser.error("--font-size must be a finite number greater than zero")
    if args.text_margin is not None:
        if isinstance(args.text_margin, bool) or not isinstance(
            args.text_margin, (int, float)
        ):
            parser.error("settings file value 'text_margin' must be a number or null")
        if not math.isfinite(args.text_margin) or args.text_margin < 0:
            parser.error("--text-margin must be a finite number greater than or equal to zero")
    if isinstance(args.text_end_gap, bool) or not isinstance(
        args.text_end_gap, (int, float)
    ):
        parser.error("settings file value 'text_end_gap' must be a number")
    if not math.isfinite(args.text_end_gap) or args.text_end_gap < 0:
        parser.error(
            "--text-end-gap must be a finite number greater than or equal to zero"
        )
    if args.text_boundary_percent < 0 or args.text_boundary_percent >= 50:
        parser.error(
            "--text-boundary-percent must be greater than or equal to zero and below 50"
        )
    if args.terrain_height is not None:
        if isinstance(args.terrain_height, bool) or not isinstance(
            args.terrain_height, (int, float)
        ):
            parser.error("settings file value 'terrain_height' must be a number or null")
        if not math.isfinite(args.terrain_height) or args.terrain_height <= 0:
            parser.error("--terrain-height must be a finite number greater than zero")
    if args.max_size <= 0 or args.base_height <= 0:
        parser.error("model dimensions must be greater than zero")
    for name in (
        "font_family",
        "text_align",
        "text_mode",
        "font_weight",
        "font_style",
        "mode",
    ):
        if not isinstance(getattr(args, name), str):
            parser.error(f"settings file value '{name}' must be a string")
    if args.shape not in {"square", "circle", "hex"}:
        parser.error(
            "settings file value 'shape' must be 'square', 'circle', or 'hex'"
        )
    if args.mode not in {"topo", "city"}:
        parser.error("settings file value 'mode' must be 'topo' or 'city'")
    if args.text_align not in {"left", "center", "right"}:
        parser.error(
            "settings file value 'text_align' must be 'left', 'center', or 'right'"
        )
    if args.text_mode not in {"raised", "embedded"}:
        parser.error(
            "settings file value 'text_mode' must be 'raised' or 'embedded'"
        )
    if args.font_weight not in {"normal", "bold"}:
        parser.error(
            "settings file value 'font_weight' must be 'normal' or 'bold'"
        )
    if args.font_style not in {"normal", "italic"}:
        parser.error(
            "settings file value 'font_style' must be 'normal' or 'italic'"
        )
    if args.topo_source not in {"auto", "online", "local"}:
        parser.error(
            "settings file value 'topo_source' must be 'auto', 'online', or 'local'"
        )
    for name in ("text", "dem_type", "api_key"):
        value = getattr(args, name)
        if value is not None and not isinstance(value, str):
            parser.error(f"settings file value '{name}' must be a string or null")


def _settings_search_start(input_path: Path | None) -> Path:
    if input_path is None:
        return Path.cwd()
    resolved = Path(input_path).resolve()
    return resolved if resolved.is_dir() else resolved.parent


def main(argv: list[str] | None = None) -> None:
    dotenv_path = find_dotenv(usecwd=True)
    if dotenv_path:
        load_dotenv(dotenv_path, override=False)
    parser = create_parser()
    try:
        initial_args = parser.parse_args(argv)
        settings_path = (
            Path(initial_args.settings).resolve()
            if initial_args.settings is not None
            else find_settings(_settings_search_start(initial_args.gpx_file))
        )
        settings = load_settings(settings_path)
        parser = create_parser(settings)
        configs = configs_from_args(parser.parse_args(argv), parser)
        for config in configs:
            convert(config)
    except Gpx2StlError as exc:
        parser.exit(1, f"gpx2stl: error: {exc}\n")
    except KeyboardInterrupt:
        parser.exit(130, "gpx2stl: interrupted\n")
