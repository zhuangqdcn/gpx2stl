from __future__ import annotations

from pathlib import Path

import pytest

import gpx2stl.cli
from gpx2stl.cli import (
    config_from_args,
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
    assert config.max_size == 200.0
    assert config.terrain_height == 20.0
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
        '{"gpx_file": "routes/example.gpx", "topo_dir": "asset"}',
        encoding="utf-8",
    )
    values = load_settings(settings)
    assert values["gpx_file"] == str((tmp_path / "routes" / "example.gpx").resolve())
    assert values["topo_dir"] == str((tmp_path / "asset").resolve())


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
