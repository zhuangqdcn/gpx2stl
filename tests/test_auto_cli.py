from __future__ import annotations

import json
from pathlib import Path

import pytest

from gpx2stl.cli import config_from_args, configs_from_args, create_parser, load_settings
from gpx2stl.models import Config


@pytest.mark.parametrize("topo", [True, False])
def test_omitted_boundary_default_depends_on_topography(simple_gpx: Path, topo: bool) -> None:
    parser = create_parser()
    config = config_from_args(
        parser.parse_args([str(simple_gpx), "--topo" if topo else "--no-topo"]),
        parser,
    )
    assert config.route_boundary_percent == ("auto" if topo else 10.0)
    assert config.auto_boundary_max_distance_km == 20.0
    programmatic = Config(gpx_file=simple_gpx, output=config.output, topo=topo)
    assert programmatic.resolved_route_boundary_percent == ("auto" if topo else 10.0)


def test_explicit_auto_cli_preserves_independent_text_boundary(simple_gpx: Path) -> None:
    parser = create_parser()
    config = config_from_args(
        parser.parse_args(
            [
                str(simple_gpx),
                "--route-boundary-percent", "auto",
                "--text-boundary-percent", "20",
                "--auto-boundary-max-distance-km", "12.5",
            ]
        ),
        parser,
    )
    assert config.resolved_route_boundary_percent == "auto"
    assert config.text_boundary_percent == 20.0
    assert config.auto_boundary_max_distance_km == 12.5


@pytest.mark.parametrize(
    ("setting", "override", "expected"),
    [("auto", "15", 15.0), (10, "auto", "auto"), ("auto", None, "auto"), (0, None, 0.0)],
)
def test_auto_and_numeric_settings_cli_precedence(
    simple_gpx: Path, tmp_path: Path, setting: str | float,
    override: str | None, expected: str | float,
) -> None:
    path = tmp_path / "auto-settings.json"
    path.write_text(
        json.dumps({"route_boundary_percent": setting, "auto_boundary_max_distance_km": 8}),
        encoding="utf-8",
    )
    parser = create_parser(load_settings(path))
    arguments = [str(simple_gpx)]
    if override is not None:
        arguments.extend(["--route-boundary-percent", override])
    config = config_from_args(parser.parse_args(arguments), parser)
    assert config.resolved_route_boundary_percent == expected
    assert config.auto_boundary_max_distance_km == 8.0


@pytest.mark.parametrize("settings", [{}, {"route_boundary_percent": "auto"}])
def test_explicit_auto_without_topography_is_rejected(
    simple_gpx: Path, settings: dict, capsys: pytest.CaptureFixture[str],
) -> None:
    parser = create_parser(settings)
    arguments = [str(simple_gpx), "--no-topo"]
    if not settings:
        arguments.extend(["--route-boundary-percent", "auto"])
    with pytest.raises(SystemExit):
        config_from_args(parser.parse_args(arguments), parser)
    assert "requires topography" in capsys.readouterr().err


def test_numeric_override_allows_flat_model_with_auto_settings(simple_gpx: Path) -> None:
    parser = create_parser({"route_boundary_percent": "auto"})
    config = config_from_args(
        parser.parse_args([str(simple_gpx), "--no-topo", "--route-boundary-percent", "0"]),
        parser,
    )
    assert config.resolved_route_boundary_percent == 0.0


@pytest.mark.parametrize("value", ["AUTO", "automatic", "-1", "nan", "inf", "-inf"])
def test_auto_boundary_rejects_invalid_cli_values(simple_gpx: Path, value: str) -> None:
    with pytest.raises(SystemExit):
        create_parser().parse_args([str(simple_gpx), f"--route-boundary-percent={value}"])


@pytest.mark.parametrize("value", [None, True, False, [], {}, "AUTO"])
def test_auto_boundary_rejects_invalid_json_values(
    simple_gpx: Path, tmp_path: Path, value: object,
) -> None:
    path = tmp_path / "invalid-auto-settings.json"
    path.write_text(json.dumps({"route_boundary_percent": value}), encoding="utf-8")
    parser = create_parser(load_settings(path))
    with pytest.raises(SystemExit):
        config_from_args(parser.parse_args([str(simple_gpx)]), parser)


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "-inf", "auto"])
def test_auto_search_cap_rejects_invalid_cli_values(simple_gpx: Path, value: str) -> None:
    with pytest.raises(SystemExit):
        create_parser().parse_args([str(simple_gpx), f"--auto-boundary-max-distance-km={value}"])


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf"), True, None, [], {}])
def test_auto_search_cap_rejects_invalid_json_values(
    simple_gpx: Path, tmp_path: Path, value: object,
) -> None:
    path = tmp_path / "invalid-cap-settings.json"
    path.write_text(json.dumps({"auto_boundary_max_distance_km": value}), encoding="utf-8")
    parser = create_parser(load_settings(path))
    with pytest.raises(SystemExit):
        config_from_args(parser.parse_args([str(simple_gpx)]), parser)


def test_search_cap_cli_overrides_settings(simple_gpx: Path) -> None:
    parser = create_parser({"auto_boundary_max_distance_km": 10})
    config = config_from_args(
        parser.parse_args([str(simple_gpx), "--auto-boundary-max-distance-km", "30"]),
        parser,
    )
    assert config.auto_boundary_max_distance_km == 30.0


@pytest.mark.parametrize("activity_fixture", ["simple_gpx", "simple_fit"])
def test_auto_settings_apply_to_batch_activity_inputs(
    request: pytest.FixtureRequest, tmp_path: Path, activity_fixture: str,
) -> None:
    activity = request.getfixturevalue(activity_fixture)
    second = tmp_path / ("second" + activity.suffix)
    second.write_bytes(activity.read_bytes())
    parser = create_parser({"route_boundary_percent": "auto", "auto_boundary_max_distance_km": 10})
    configs = configs_from_args(parser.parse_args([str(tmp_path)]), parser)
    assert len(configs) == 2
    assert all(config.resolved_route_boundary_percent == "auto" for config in configs)
    assert all(config.auto_boundary_max_distance_km == 10 for config in configs)
