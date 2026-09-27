from __future__ import annotations

from pathlib import Path

import pytest

import gpx2stl.cli
from gpx2stl.cli import config_from_args, create_parser


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
    assert config.api_key == "test-key"


def test_no_topo_stl_does_not_require_key(simple_gpx: Path) -> None:
    parser = create_parser()
    config = config_from_args(
        parser.parse_args([str(simple_gpx), "--no-topo", "--no-3mf"]),
        parser,
    )
    assert config.output.suffix == ".stl"
    assert config.api_key is None


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
