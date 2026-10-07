from __future__ import annotations

from pathlib import Path

import pytest

import gpx2stl.cli
from gpx2stl.cli import (
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
    config = config_from_args(parser.parse_args([str(simple_gpx)]), parser)

    assert config.output == simple_gpx.with_suffix(".3mf")
    assert config.topo is True
    assert config.use_3mf is True
    assert config.route_width == 1.0
    assert config.route_height == 2.0
    assert config.boundary_percent == 10.0
    assert config.shape == "square"
    assert config.text is None
    assert config.text_height == 1.0
    assert config.text_margin is None
    assert config.text_end_gap == 0.0
    assert config.inner_size_percent == 70.0
    assert config.font_file is None
    assert config.base_stl is None
    assert config.max_size == 200.0
    assert config.terrain_height is None
    assert config.base_height == 2.0
    assert config.topo_source == "auto"
    assert config.topo_file is None
    assert config.topo_dir == (Path.cwd() / "asset").resolve()
    assert config.api_key == "test-key"


def test_no_topo_stl_does_not_require_key(simple_gpx: Path) -> None:
    parser = create_parser()
    config = config_from_args(
        parser.parse_args([str(simple_gpx), "--no-topo", "--no-3mf"]),
        parser,
    )
    assert config.output.suffix == ".stl"
    assert config.api_key is None


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


def test_settings_supply_defaults_and_cli_wins(
    simple_gpx: Path, tmp_path: Path
) -> None:
    settings_path = tmp_path / "settings.json"
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
        "boundary_percent": 10.0,
        "shape": "square",
        "text": "SETTINGS",
        "text_height": 1.0,
        "text_margin": 0.5,
        "text_end_gap": 1.0,
        "inner_size_percent": 70.0,
        "font_file": "settings.ttf",
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
            "--output",
            "cli.stl",
            "--route-width",
            "3",
            "--route-height",
            "4",
            "--no-topo",
            "--boundary-percent",
            "25",
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
            "--inner-size-percent",
            "65",
            "--font-file",
            "cli.otf",
            "--base-stl",
            "cli.stl",
            "--no-3mf",
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
    assert args.output == Path("cli.stl")
    assert args.route_width == 3.0
    assert args.route_height == 4.0
    assert args.topo is False
    assert args.boundary_percent == 25.0
    assert args.shape == "hex"
    assert args.text == "CLI"
    assert args.text_height == 1.5
    assert args.text_margin == 2.0
    assert args.text_end_gap == 3.0
    assert args.inner_size_percent == 65.0
    assert args.font_file == Path("cli.otf")
    assert args.base_stl == Path("cli.stl")
    assert args.use_3mf is False
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
    settings = tmp_path / "settings.json"
    settings.write_text("{}", encoding="utf-8")
    nested = tmp_path / "one" / "two"
    nested.mkdir(parents=True)
    assert find_settings(nested) == settings


def test_settings_paths_are_relative_to_settings_file(tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
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


@pytest.mark.parametrize("value", ["0", "100"])
def test_inner_size_percent_must_leave_a_frame(
    simple_gpx: Path, value: str
) -> None:
    parser = create_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(
            [str(simple_gpx), "--no-topo", "--inner-size-percent", value]
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
                    "--inner-size-percent",
                    "70",
                    "--route-width",
                    "14",
                ]
            ),
            parser,
        )


def test_unknown_setting_is_rejected(tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    settings.write_text('{"unknown": true}', encoding="utf-8")
    with pytest.raises(Gpx2StlError, match="Unknown setting"):
        load_settings(settings)


def test_invalid_settings_json_is_rejected(tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    settings.write_text("{", encoding="utf-8")
    with pytest.raises(Gpx2StlError, match="Invalid JSON"):
        load_settings(settings)
