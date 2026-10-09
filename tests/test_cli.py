from __future__ import annotations

import json
from pathlib import Path

import pytest

import gpx2stl.cli
from gpx2stl.cli import (
    SETTINGS_FILENAME,
    _select_platform_topo_settings,
    config_from_args,
    configs_from_args,
    create_parser,
    find_settings,
    load_settings,
)
from gpx2stl.errors import Gpx2StlError


def test_cli_defaults(simple_gpx: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENTOPOGRAPHY_API_KEY", "test-key")
    parser = create_parser()
    args = parser.parse_args([str(simple_gpx)])
    assert args.route_boundary_percent is None
    config = config_from_args(args, parser)

    assert config.output == simple_gpx.with_suffix(".3mf")
    assert config.mode == "topo"
    assert config.topo is True
    assert config.use_3mf is True
    assert config.route_width == 1.0
    assert config.route_height == 2.0
    assert config.route_depth == 0.6
    assert config.road_snap_distance == 5.0
    assert config.route_boundary_percent == "auto"
    assert config.resolved_route_boundary_percent == "auto"
    assert config.auto_boundary_max_distance_km == 10.0
    assert config.shape == "square"
    assert config.text is None
    assert config.text_height == 1.0
    assert config.text_margin is None
    assert config.text_end_gap == 0.0
    assert config.text_align == "center"
    assert config.text_mode == "raised"
    assert config.text_depth == 0.6
    assert config.text_boundary_percent == 7.0
    assert config.font_family == "DejaVu Sans"
    assert config.font_file is None
    assert config.font_size is None
    assert config.font_weight == "normal"
    assert config.font_style == "normal"
    assert config.base_stl is None
    assert config.max_size == 200.0
    assert config.terrain_height is None
    assert config.base_height == 2.0
    assert config.topo_source == "auto"
    assert config.topo_file is None
    assert config.topo_dir == (Path.cwd() / "asset").resolve()
    assert config.city_dir == (Path.cwd() / "asset" / "city").resolve()
    assert config.building_default_height == 10.0
    assert config.building_height_scale == 5.0
    assert config.water_depth == 0.4
    assert config.api_key == "test-key"


def test_no_topo_stl_does_not_require_key(simple_gpx: Path) -> None:
    parser = create_parser()
    config = config_from_args(
        parser.parse_args([str(simple_gpx), "--no-topo", "--no-3mf"]),
        parser,
    )
    assert config.output.suffix == ".stl"
    assert config.api_key is None
    assert config.route_boundary_percent == 10.0
    assert config.resolved_route_boundary_percent == 10.0


def test_city_mode_configuration(simple_gpx: Path) -> None:
    parser = create_parser()
    config = config_from_args(
        parser.parse_args(
            [
                str(simple_gpx),
                "--mode",
                "city",
                "--road-snap-distance",
                "0",
                "--route-depth",
                "0.8",
                "--building-default-height",
                "12",
                "--building-height-scale",
                "1.5",
                "--city-dir",
                "osm-cache",
            ]
        ),
        parser,
    )

    assert config.mode == "city"
    assert config.route_width == 0.5
    assert config.route_height == 1.5
    assert config.route_boundary_percent == (10.0, 10.0, 10.0, 10.0)
    assert config.resolved_route_boundary_percent == (10.0, 10.0, 10.0, 10.0)
    assert config.road_snap_distance == 0.0
    assert config.route_depth == 0.8
    assert config.building_default_height == 12.0
    assert config.building_height_scale == 1.5
    assert config.city_dir == (Path.cwd() / "osm-cache").resolve()


def test_city_mode_route_defaults_are_narrow_and_deep(simple_gpx: Path) -> None:
    parser = create_parser()
    config = config_from_args(
        parser.parse_args([str(simple_gpx), "--mode", "city"]),
        parser,
    )

    assert config.route_width == 0.5
    assert config.route_height == 1.5
    assert config.route_depth == 1.5


def test_city_mode_accepts_directional_route_boundary(simple_gpx: Path) -> None:
    parser = create_parser()
    config = config_from_args(
        parser.parse_args(
            [
                str(simple_gpx),
                "--mode",
                "city",
                "--route-boundary-percent",
                "10,20,30,40",
            ]
        ),
        parser,
    )

    assert config.route_boundary_percent == (10.0, 20.0, 30.0, 40.0)


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (["--mode", "city", "--no-topo"], "requires topography"),
        (["--mode", "city", "--no-3mf"], "requires 3MF"),
    ],
)
def test_city_mode_rejects_incompatible_output_options(
    simple_gpx: Path, arguments: list[str], message: str, capsys
) -> None:
    parser = create_parser()
    with pytest.raises(SystemExit):
        config_from_args(parser.parse_args([str(simple_gpx), *arguments]), parser)
    assert message in capsys.readouterr().err


def test_city_mode_accepts_custom_base_with_symmetric_default(
    simple_gpx: Path, tmp_path: Path
) -> None:
    base = tmp_path / "base.stl"
    base.touch()
    parser = create_parser()
    config = config_from_args(
        parser.parse_args(
            [str(simple_gpx), "--mode", "city", "--base-stl", str(base)]
        ),
        parser,
    )

    assert config.base_stl == base.resolve()
    assert config.route_boundary_percent == 10.0


def test_directory_input_creates_one_config_per_gpx(
    simple_gpx: Path, simple_fit: Path, tmp_path: Path
) -> None:
    routes = tmp_path / "routes"
    routes.mkdir()
    (routes / "zeta.gpx").write_bytes(simple_gpx.read_bytes())
    (routes / "Alpha.GPX").write_bytes(simple_gpx.read_bytes())
    (routes / "middle.FIT").write_bytes(simple_fit.read_bytes())
    (routes / "notes.txt").write_text("not a route", encoding="utf-8")
    nested = routes / "nested"
    nested.mkdir()
    (nested / "ignored.gpx").write_bytes(simple_gpx.read_bytes())
    parser = create_parser()

    configs = configs_from_args(
        parser.parse_args([str(routes), "--no-topo"]),
        parser,
    )

    assert [config.gpx_file.name for config in configs] == [
        "Alpha.GPX",
        "middle.FIT",
        "zeta.gpx",
    ]
    assert [config.output for config in configs] == [
        routes / "Alpha.3mf",
        routes / "middle.3mf",
        routes / "zeta.3mf",
    ]


def test_directory_input_uses_output_directory(
    simple_gpx: Path, tmp_path: Path
) -> None:
    routes = tmp_path / "routes"
    routes.mkdir()
    output = tmp_path / "models"
    output.mkdir()
    (routes / "first.gpx").write_bytes(simple_gpx.read_bytes())
    (routes / "second.gpx").write_bytes(simple_gpx.read_bytes())
    parser = create_parser()

    configs = configs_from_args(
        parser.parse_args(
            [str(routes), "--no-topo", "--no-3mf", "--output", str(output)]
        ),
        parser,
    )

    assert [config.output for config in configs] == [
        output / "first.stl",
        output / "second.stl",
    ]


def test_directory_input_rejects_empty_directory(tmp_path: Path) -> None:
    parser = create_parser()
    with pytest.raises(SystemExit):
        configs_from_args(
            parser.parse_args([str(tmp_path), "--no-topo"]),
            parser,
        )


def test_directory_input_requires_output_directory(
    simple_gpx: Path, tmp_path: Path
) -> None:
    routes = tmp_path / "routes"
    routes.mkdir()
    (routes / "route.gpx").write_bytes(simple_gpx.read_bytes())
    parser = create_parser()
    with pytest.raises(SystemExit):
        configs_from_args(
            parser.parse_args(
                [
                    str(routes),
                    "--no-topo",
                    "--output",
                    str(tmp_path / "models.3mf"),
                ]
            ),
            parser,
        )


def test_directory_input_rejects_duplicate_output_stems(
    simple_gpx: Path, simple_fit: Path, tmp_path: Path
) -> None:
    routes = tmp_path / "routes"
    routes.mkdir()
    (routes / "route.gpx").write_bytes(simple_gpx.read_bytes())
    (routes / "route.fit").write_bytes(simple_fit.read_bytes())
    parser = create_parser()
    with pytest.raises(SystemExit):
        configs_from_args(
            parser.parse_args([str(routes), "--no-topo"]),
            parser,
        )


def test_file_input_rejects_unsupported_activity_extension(tmp_path: Path) -> None:
    route = tmp_path / "route.tcx"
    route.touch()
    parser = create_parser()
    with pytest.raises(SystemExit):
        configs_from_args(
            parser.parse_args([str(route), "--no-topo"]),
            parser,
        )


def test_auto_topo_defers_missing_api_key(simple_gpx: Path) -> None:
    parser = create_parser()
    config = config_from_args(parser.parse_args([str(simple_gpx)]), parser)
    assert config.topo_source == "auto"
    assert config.api_key is None


def test_online_topo_requires_api_key(simple_gpx: Path) -> None:
    parser = create_parser()
    with pytest.raises(SystemExit):
        config_from_args(
            parser.parse_args([str(simple_gpx), "--topo-source", "online"]),
            parser,
        )


def test_local_topo_file_takes_precedence(
    simple_gpx: Path, tmp_path: Path
) -> None:
    topo_file = tmp_path / "terrain.tif"
    topo_file.touch()
    parser = create_parser()
    config = config_from_args(
        parser.parse_args(
            [
                str(simple_gpx),
                "--topo-source",
                "local",
                "--topo-file",
                str(topo_file),
                "--topo-dir",
                str(tmp_path / "missing"),
            ]
        ),
        parser,
    )
    assert config.topo_file == topo_file.resolve()
    assert config.topo_dir == (tmp_path / "missing").resolve()


def test_main_loads_dotenv_before_resolving_config(
    simple_gpx: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".env").write_text(
        "OPENTOPOGRAPHY_API_KEY=dotenv-key\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPENTOPOGRAPHY_API_KEY", raising=False)
    captured = []
    monkeypatch.setattr(gpx2stl.cli, "convert", captured.append)

    gpx2stl.cli.main([str(simple_gpx)])

    assert captured[0].api_key == "dotenv-key"


def test_main_converts_each_directory_gpx_individually(
    simple_gpx: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    routes = tmp_path / "routes"
    routes.mkdir()
    (routes / "one.gpx").write_bytes(simple_gpx.read_bytes())
    (routes / "two.gpx").write_bytes(simple_gpx.read_bytes())
    captured = []
    monkeypatch.setattr(gpx2stl.cli, "convert", captured.append)

    gpx2stl.cli.main([str(routes), "--no-topo"])

    assert [config.gpx_file.name for config in captured] == ["one.gpx", "two.gpx"]
    assert [config.output.name for config in captured] == ["one.3mf", "two.3mf"]


def test_main_discovers_settings_beside_input_file(
    simple_gpx: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (simple_gpx.parent / SETTINGS_FILENAME).write_text(
        '{"route_width": 3.0, "topo": false}',
        encoding="utf-8",
    )
    captured = []
    monkeypatch.setattr(gpx2stl.cli, "convert", captured.append)

    gpx2stl.cli.main([str(simple_gpx)])

    assert captured[0].route_width == 3.0
    assert captured[0].topo is False


def test_explicit_settings_path_overrides_discovery(
    simple_gpx: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (simple_gpx.parent / SETTINGS_FILENAME).write_text(
        '{"route_width": 3.0, "topo": false}',
        encoding="utf-8",
    )
    explicit = tmp_path / "custom-settings.json"
    explicit.write_text(
        '{"route_width": 4.0, "topo": false}',
        encoding="utf-8",
    )
    captured = []
    monkeypatch.setattr(gpx2stl.cli, "convert", captured.append)

    gpx2stl.cli.main([str(simple_gpx), "--settings", str(explicit)])

    assert captured[0].route_width == 4.0


def test_command_line_overrides_explicit_settings(
    simple_gpx: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    explicit = tmp_path / "custom-settings.json"
    explicit.write_text(
        '{"route_width": 3.0, "topo": false}',
        encoding="utf-8",
    )
    captured = []
    monkeypatch.setattr(gpx2stl.cli, "convert", captured.append)

    gpx2stl.cli.main(
        [
            str(simple_gpx),
            "--settings",
            str(explicit),
            "--route-width",
            "5",
        ]
    )

    assert captured[0].route_width == 5.0


def test_settings_supply_defaults_and_cli_wins(
    simple_gpx: Path, tmp_path: Path
) -> None:
    settings_path = tmp_path / SETTINGS_FILENAME
    settings_path.write_text(
        """
{
  "gpx_file": "from-settings.gpx",
  "route_width": 3.0,
  "shape": "circle",
  "topo": false,
  "use_3mf": false
}
""",
        encoding="utf-8",
    )
    parser = create_parser(load_settings(settings_path))
    config = config_from_args(
        parser.parse_args([str(simple_gpx), "--route-width", "4"]),
        parser,
    )
    assert config.gpx_file == simple_gpx.resolve()
    assert config.route_width == 4.0
    assert config.shape == "circle"
    assert config.topo is False
    assert config.use_3mf is False


def test_every_cli_parameter_overrides_settings_defaults() -> None:
    settings = {
        "gpx_file": "settings.gpx",
        "output": "settings.3mf",
        "route_width": 1.0,
        "route_height": 2.0,
        "topo": True,
        "route_boundary_percent": 10.0,
        "auto_boundary_max_distance_km": 20.0,
        "shape": "square",
        "text": "SETTINGS",
        "text_height": 1.0,
        "text_margin": 0.5,
        "text_end_gap": 1.0,
        "text_align": "left",
        "text_mode": "raised",
        "text_depth": 0.5,
        "text_boundary_percent": 15.0,
        "font_family": "Settings Sans",
        "font_file": None,
        "font_size": 4.0,
        "font_weight": "normal",
        "font_style": "normal",
        "base_stl": "settings.stl",
        "use_3mf": True,
        "max_size": 200.0,
        "terrain_height": 20.0,
        "base_height": 2.0,
        "topo_source": "auto",
        "topo_file": "settings.tif",
        "topo_dir": "settings-assets",
        "dem_type": "SRTMGL1",
        "api_key": "settings-key",
        "force": False,
    }
    args = create_parser(settings).parse_args(
        [
            "cli.gpx",
            "--settings",
            "cli-settings.json",
            "--output",
            "cli.stl",
            "--route-width",
            "3",
            "--route-height",
            "4",
            "--no-topo",
            "--route-boundary-percent",
            "25",
            "--auto-boundary-max-distance-km",
            "30",
            "--shape",
            "hex",
            "--text",
            "CLI",
            "--text-height",
            "1.5",
            "--text-margin",
            "2",
            "--text-end-gap",
            "3",
            "--text-align",
            "right",
            "--text-mode",
            "embedded",
            "--text-depth",
            "0.8",
            "--text-boundary-percent",
            "17.5",
            "--font-family",
            "CLI Sans",
            "--font-size",
            "5",
            "--font-weight",
            "bold",
            "--font-style",
            "italic",
            "--base-stl",
            "cli.stl",
            "--3mf",
            "--max-size",
            "180",
            "--terrain-height",
            "30",
            "--base-height",
            "5",
            "--topo-source",
            "local",
            "--topo-file",
            "cli.tif",
            "--topo-dir",
            "cli-assets",
            "--dem-type",
            "COP30",
            "--api-key",
            "cli-key",
            "--force",
        ]
    )

    assert args.gpx_file == Path("cli.gpx")
    assert args.settings == Path("cli-settings.json")
    assert args.output == Path("cli.stl")
    assert args.route_width == 3.0
    assert args.route_height == 4.0
    assert args.topo is False
    assert args.route_boundary_percent == 25.0
    assert args.auto_boundary_max_distance_km == 30.0
    assert args.shape == "hex"
    assert args.text == "CLI"
    assert args.text_height == 1.5
    assert args.text_margin == 2.0
    assert args.text_end_gap == 3.0
    assert args.text_align == "right"
    assert args.text_mode == "embedded"
    assert args.text_depth == 0.8
    assert args.text_boundary_percent == 17.5
    assert args.font_family == "CLI Sans"
    assert args.font_file is None
    assert args.font_size == 5.0
    assert args.font_weight == "bold"
    assert args.font_style == "italic"
    assert args.base_stl == Path("cli.stl")
    assert args.use_3mf is True
    assert args.max_size == 180.0
    assert args.terrain_height == 30.0
    assert args.base_height == 5.0
    assert args.topo_source == "local"
    assert args.topo_file == Path("cli.tif")
    assert args.topo_dir == Path("cli-assets")
    assert args.dem_type == "COP30"
    assert args.api_key == "cli-key"
    assert args.force is True


@pytest.mark.parametrize(
    ("setting_name", "setting_value", "cli_argument", "expected"),
    [
        ("topo", False, "--topo", True),
        ("topo", True, "--no-topo", False),
        ("use_3mf", False, "--3mf", True),
        ("use_3mf", True, "--no-3mf", False),
    ],
)
def test_boolean_cli_flags_override_settings_in_both_directions(
    setting_name: str,
    setting_value: bool,
    cli_argument: str,
    expected: bool,
) -> None:
    args = create_parser({setting_name: setting_value}).parse_args(
        ["route.gpx", cli_argument]
    )
    assert getattr(args, setting_name) is expected


def test_settings_can_supply_required_gpx(
    simple_gpx: Path, tmp_path: Path
) -> None:
    parser = create_parser(
        {
            "gpx_file": str(simple_gpx),
            "topo": False,
        }
    )
    config = config_from_args(parser.parse_args([]), parser)
    assert config.gpx_file == simple_gpx.resolve()


def test_find_settings_searches_parent_directories(tmp_path: Path) -> None:
    settings = tmp_path / SETTINGS_FILENAME
    settings.write_text("{}", encoding="utf-8")
    nested = tmp_path / "one" / "two"
    nested.mkdir(parents=True)
    assert find_settings(nested) == settings


def test_find_settings_ignores_old_filename(tmp_path: Path) -> None:
    (tmp_path / "settings.json").write_text("{}", encoding="utf-8")
    assert find_settings(tmp_path) is None


def test_explicit_missing_settings_file_is_reported(
    simple_gpx: Path, tmp_path: Path
) -> None:
    with pytest.raises(SystemExit) as error:
        gpx2stl.cli.main(
            [
                str(simple_gpx),
                "--settings",
                str(tmp_path / "missing.json"),
            ]
        )
    assert error.value.code == 1


def test_settings_paths_are_relative_to_settings_file(tmp_path: Path) -> None:
    settings = tmp_path / SETTINGS_FILENAME
    settings.write_text(
        '{"gpx_file": "routes/example.gpx", "topo_dir": "asset", '
        '"font_file": "fonts/custom.ttf", "base_stl": "bases/custom.stl"}',
        encoding="utf-8",
    )
    values = load_settings(settings)
    assert values["gpx_file"] == str((tmp_path / "routes" / "example.gpx").resolve())
    assert values["topo_dir"] == str((tmp_path / "asset").resolve())
    assert values["font_file"] == str(
        (tmp_path / "fonts" / "custom.ttf").resolve()
    )
    assert values["base_stl"] == str(
        (tmp_path / "bases" / "custom.stl").resolve()
    )


def test_platform_topo_paths_override_generic_values() -> None:
    settings = {
        "topo_file": "generic.tif",
        "topo_file_windows": r"E:\terrain\windows.tif",
        "topo_file_linux": "/mnt/e/terrain/linux.tif",
        "topo_dir": "generic-assets",
        "topo_dir_windows": r"E:\terrain\windows-assets",
        "topo_dir_linux": "/mnt/e/terrain/linux-assets",
    }

    windows = _select_platform_topo_settings(settings, "win32")
    linux = _select_platform_topo_settings(settings, "linux")

    assert windows["topo_file"] == r"E:\terrain\windows.tif"
    assert windows["topo_dir"] == r"E:\terrain\windows-assets"
    assert linux["topo_file"] == "/mnt/e/terrain/linux.tif"
    assert linux["topo_dir"] == "/mnt/e/terrain/linux-assets"
    assert not set(windows).intersection(gpx2stl.cli.PLATFORM_TOPO_KEYS)
    assert not set(linux).intersection(gpx2stl.cli.PLATFORM_TOPO_KEYS)
    assert settings["topo_dir"] == "generic-assets"


def test_null_platform_topo_paths_fall_back_to_generic_values() -> None:
    settings = {
        "topo_file": "generic.tif",
        "topo_file_windows": None,
        "topo_dir": "generic-assets",
        "topo_dir_windows": None,
    }

    selected = _select_platform_topo_settings(settings, "win32")

    assert selected["topo_file"] == "generic.tif"
    assert selected["topo_dir"] == "generic-assets"


def test_other_platforms_use_generic_topo_paths() -> None:
    settings = {
        "topo_dir": "generic-assets",
        "topo_dir_windows": r"E:\terrain\windows-assets",
        "topo_dir_linux": "/mnt/e/terrain/linux-assets",
    }

    selected = _select_platform_topo_settings(settings, "darwin")

    assert selected["topo_dir"] == "generic-assets"


def test_selected_platform_relative_topo_path_uses_settings_directory(
    tmp_path: Path,
) -> None:
    if gpx2stl.cli.sys.platform.startswith("win"):
        platform_key = "topo_dir_windows"
    elif gpx2stl.cli.sys.platform.startswith("linux"):
        platform_key = "topo_dir_linux"
    else:
        pytest.skip("OS-specific topo settings apply only to Windows and Linux")
    settings = tmp_path / SETTINGS_FILENAME
    settings.write_text(
        json.dumps({platform_key: "platform-assets"}),
        encoding="utf-8",
    )

    values = load_settings(settings)

    assert values["topo_dir"] == str((tmp_path / "platform-assets").resolve())


@pytest.mark.parametrize("value", [123, True, [], {}])
def test_settings_reject_invalid_platform_topo_path_types(
    tmp_path: Path,
    value: object,
) -> None:
    settings = tmp_path / SETTINGS_FILENAME
    settings.write_text(
        json.dumps({"topo_dir_linux": value}),
        encoding="utf-8",
    )

    with pytest.raises(Gpx2StlError, match="topo_dir_linux"):
        load_settings(settings)


def test_cli_topo_dir_overrides_platform_settings(
    simple_gpx: Path,
    tmp_path: Path,
) -> None:
    settings_dir = tmp_path / "settings-assets"
    cli_dir = tmp_path / "cli-assets"
    parser = create_parser(
        {
            "topo": False,
            "topo_dir": str(settings_dir),
        }
    )

    config = config_from_args(
        parser.parse_args(
            [
                str(simple_gpx),
                "--topo-dir",
                str(cli_dir),
            ]
        ),
        parser,
    )

    assert config.topo_dir == cli_dir.resolve()


def test_cli_topo_file_overrides_platform_settings(
    simple_gpx: Path,
    tmp_path: Path,
) -> None:
    settings_file = tmp_path / "settings.tif"
    cli_file = tmp_path / "cli.tif"
    parser = create_parser(
        {
            "topo": False,
            "topo_file": str(settings_file),
        }
    )

    config = config_from_args(
        parser.parse_args(
            [
                str(simple_gpx),
                "--topo-file",
                str(cli_file),
            ]
        ),
        parser,
    )

    assert config.topo_file == cli_file.resolve()


def test_base_stl_is_validated(simple_gpx: Path, tmp_path: Path) -> None:
    parser = create_parser()
    wrong_extension = tmp_path / "base.obj"
    wrong_extension.touch()
    with pytest.raises(SystemExit):
        config_from_args(
            parser.parse_args(
                [str(simple_gpx), "--no-topo", "--base-stl", str(wrong_extension)]
            ),
            parser,
        )


@pytest.mark.parametrize("value", ["-1", "50", "100"])
def test_text_boundary_percent_must_leave_terrain(
    simple_gpx: Path, value: str
) -> None:
    parser = create_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(
            [str(simple_gpx), "--no-topo", "--text-boundary-percent", value]
        )


def test_text_margin_must_be_nonnegative(simple_gpx: Path) -> None:
    parser = create_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([str(simple_gpx), "--no-topo", "--text-margin", "-1"])


def test_settings_allow_automatic_text_margin(simple_gpx: Path) -> None:
    parser = create_parser({"text_margin": None, "topo": False})
    config = config_from_args(parser.parse_args([str(simple_gpx)]), parser)
    assert config.text_margin is None


def test_text_preserves_leading_and_trailing_spaces(simple_gpx: Path) -> None:
    parser = create_parser()
    config = config_from_args(
        parser.parse_args([str(simple_gpx), "--no-topo", "--text", "  TRAIL   "]),
        parser,
    )
    assert config.text == "  TRAIL   "


def test_text_rejects_only_whitespace(simple_gpx: Path) -> None:
    parser = create_parser()
    with pytest.raises(SystemExit):
        config_from_args(
            parser.parse_args([str(simple_gpx), "--no-topo", "--text", "   "]),
            parser,
        )


@pytest.mark.parametrize("value", [-1.0, "wide", True])
def test_settings_reject_invalid_text_margin(simple_gpx: Path, value: object) -> None:
    parser = create_parser({"text_margin": value, "topo": False})
    with pytest.raises(SystemExit):
        config_from_args(parser.parse_args([str(simple_gpx)]), parser)


def test_text_end_gap_must_be_nonnegative(simple_gpx: Path) -> None:
    parser = create_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([str(simple_gpx), "--no-topo", "--text-end-gap", "-1"])


@pytest.mark.parametrize("value", [-1.0, "wide", True])
def test_settings_reject_invalid_text_end_gap(
    simple_gpx: Path, value: object
) -> None:
    parser = create_parser({"text_end_gap": value, "topo": False})
    with pytest.raises(SystemExit):
        config_from_args(parser.parse_args([str(simple_gpx)]), parser)


def test_text_style_options_are_added_to_config(simple_gpx: Path) -> None:
    parser = create_parser()
    config = config_from_args(
        parser.parse_args(
            [
                str(simple_gpx),
                "--no-topo",
                "--text",
                "TRAIL",
                "--font-family",
                "DejaVu Sans",
                "--font-size",
                "4.5",
                "--font-weight",
                "bold",
                "--font-style",
                "italic",
                "--text-align",
                "right",
            ]
        ),
        parser,
    )
    assert config.font_family == "DejaVu Sans"
    assert config.font_size == 4.5
    assert config.font_weight == "bold"
    assert config.font_style == "italic"
    assert config.text_align == "right"


def test_embedded_text_requires_3mf_and_text(simple_gpx: Path) -> None:
    parser = create_parser()
    with pytest.raises(SystemExit):
        config_from_args(
            parser.parse_args(
                [
                    str(simple_gpx),
                    "--no-topo",
                    "--no-3mf",
                    "--text",
                    "TRAIL",
                    "--text-mode",
                    "embedded",
                ]
            ),
            parser,
        )
    with pytest.raises(SystemExit):
        config_from_args(
            parser.parse_args(
                [str(simple_gpx), "--no-topo", "--text-mode", "embedded"]
            ),
            parser,
        )


def test_generated_embedded_depth_must_fit_base(simple_gpx: Path) -> None:
    parser = create_parser()
    with pytest.raises(SystemExit):
        config_from_args(
            parser.parse_args(
                [
                    str(simple_gpx),
                    "--no-topo",
                    "--text",
                    "TRAIL",
                    "--text-mode",
                    "embedded",
                    "--text-depth",
                    "2",
                    "--base-height",
                    "2",
                ]
            ),
            parser,
        )


def test_font_file_rejects_family_variant_options(
    simple_gpx: Path, tmp_path: Path
) -> None:
    font = tmp_path / "font.ttf"
    font.touch()
    parser = create_parser()
    with pytest.raises(SystemExit):
        config_from_args(
            parser.parse_args(
                [
                    str(simple_gpx),
                    "--no-topo",
                    "--font-file",
                    str(font),
                    "--font-weight",
                    "bold",
                ]
            ),
            parser,
        )


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("font_family", None),
        ("font_size", "large"),
        ("font_weight", []),
        ("font_style", 1),
        ("text_align", {}),
        ("text_mode", False),
        ("text_depth", "deep"),
    ],
)
def test_settings_reject_invalid_text_style_types(
    simple_gpx: Path, name: str, value: object
) -> None:
    parser = create_parser({name: value, "topo": False})
    with pytest.raises(SystemExit):
        config_from_args(parser.parse_args([str(simple_gpx)]), parser)


def test_route_width_must_fit_text_inset(simple_gpx: Path) -> None:
    parser = create_parser()
    with pytest.raises(SystemExit):
        config_from_args(
            parser.parse_args(
                [
                    str(simple_gpx),
                    "--no-topo",
                    "--max-size",
                    "20",
                    "--text",
                    "TRAIL",
                    "--text-boundary-percent",
                    "15",
                    "--route-width",
                    "14",
                ]
            ),
            parser,
        )


def test_unknown_setting_is_rejected(tmp_path: Path) -> None:
    settings = tmp_path / SETTINGS_FILENAME
    settings.write_text('{"unknown": true}', encoding="utf-8")
    with pytest.raises(Gpx2StlError, match="Unknown setting"):
        load_settings(settings)


def test_invalid_settings_json_is_rejected(tmp_path: Path) -> None:
    settings = tmp_path / SETTINGS_FILENAME
    settings.write_text("{", encoding="utf-8")
    with pytest.raises(Gpx2StlError, match="Invalid JSON"):
        load_settings(settings)
