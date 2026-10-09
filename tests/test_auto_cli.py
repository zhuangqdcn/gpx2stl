from __future__ import annotations

import json
from pathlib import Path

import pytest

from gpx2stl.cli import config_from_args, configs_from_args, create_parser, load_settings
from gpx2stl.auto_boundary import ValleyCriteria
from gpx2stl.models import Config


VALLEY_DEFAULTS = {
    "auto_valley_max_relief_m": 1000.0,
    "auto_valley_max_slope_percent": 100.0,
    "auto_valley_max_height_m": 1000.0,
    "auto_valley_max_height_percent": 100.0,
}


@pytest.mark.parametrize("topo", [True, False])
def test_omitted_boundary_default_depends_on_topography(simple_gpx: Path, topo: bool) -> None:
    parser = create_parser()
    args = parser.parse_args([str(simple_gpx), "--topo" if topo else "--no-topo"])
    config = config_from_args(args, parser)
    assert config.route_boundary_percent == ("auto" if topo else 10.0)
    assert args.route_boundary_percent is None
    assert args.auto_boundary_max_distance_km == config.auto_boundary_max_distance_km == 10.0
    programmatic = Config(gpx_file=simple_gpx, output=config.output, topo=topo)
    assert programmatic.resolved_route_boundary_percent == ("auto" if topo else 10.0)
    assert programmatic.auto_boundary_max_distance_km == 10.0
    assert args.text_boundary_percent == config.text_boundary_percent == 7.0
    assert programmatic.text_boundary_percent == 7.0
    detector = ValleyCriteria()
    for name, default in VALLEY_DEFAULTS.items():
        assert getattr(args, name) == default
        assert getattr(config, name) == default
        assert getattr(programmatic, name) == default
        assert getattr(detector, name.removeprefix("auto_valley_")) == default


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


def test_city_mode_can_explicitly_select_auto_boundary(simple_gpx: Path) -> None:
    parser = create_parser()
    config = config_from_args(
        parser.parse_args(
            [
                str(simple_gpx),
                "--mode",
                "city",
                "--route-boundary-percent",
                "auto",
            ]
        ),
        parser,
    )

    assert config.resolved_route_boundary_percent == "auto"


@pytest.mark.parametrize("topo", [True, False])
@pytest.mark.parametrize("padding", [0.0, 10.0, 25.0])
def test_explicit_numeric_boundary_overrides_programmatic_default(simple_gpx, topo, padding):
    config = Config(
        gpx_file=simple_gpx, output=simple_gpx.with_suffix(".3mf"),
        topo=topo, route_boundary_percent=padding,
        auto_boundary_max_distance_km=20.0, text_boundary_percent=15.0,
        auto_valley_max_relief_m=1.0, auto_valley_max_slope_percent=0.2,
        auto_valley_max_height_m=20.0, auto_valley_max_height_percent=3.0,
    )
    assert config.resolved_route_boundary_percent == padding
    assert config.auto_boundary_max_distance_km == 20.0
    assert config.text_boundary_percent == 15.0
    assert tuple(getattr(config, name) for name in VALLEY_DEFAULTS) == (1, 0.2, 20, 3)


def test_help_reports_requested_defaults():
    parser = create_parser()
    expected = {
        "auto_boundary_max_distance_km": "default: 10",
        "auto_valley_max_relief_m": "default: 1000",
        "auto_valley_max_slope_percent": "default: 100",
        "auto_valley_max_height_m": "default: 1000",
        "auto_valley_max_height_percent": "default: 100",
        "text_boundary_percent": "default: 7",
        "route_boundary_percent": "default: 10,10,10,10 in city mode",
    }
    for action in parser._actions:
        if action.dest in expected:
            assert expected[action.dest] in action.help


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


def test_directional_settings_array_and_cli_precedence(
    simple_gpx: Path, tmp_path: Path
) -> None:
    path = tmp_path / "directional-settings.json"
    path.write_text(
        json.dumps({"mode": "city", "route_boundary_percent": [1, 2, 3, 4]}),
        encoding="utf-8",
    )
    parser = create_parser(load_settings(path))

    configured = config_from_args(parser.parse_args([str(simple_gpx)]), parser)
    overridden = config_from_args(
        parser.parse_args(
            [str(simple_gpx), "--route-boundary-percent", "5,6,7,8"]
        ),
        parser,
    )

    assert configured.route_boundary_percent == (1.0, 2.0, 3.0, 4.0)
    assert overridden.route_boundary_percent == (5.0, 6.0, 7.0, 8.0)


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


@pytest.mark.parametrize(
    "value",
    [
        "AUTO",
        "automatic",
        "-1",
        "nan",
        "inf",
        "-inf",
        "1,2,3",
        "1,2,3,4,5",
        "1,,3,4",
        "1,2,-3,4",
        "1,2,nan,4",
    ],
)
def test_auto_boundary_rejects_invalid_cli_values(simple_gpx: Path, value: str) -> None:
    with pytest.raises(SystemExit):
        create_parser().parse_args([str(simple_gpx), f"--route-boundary-percent={value}"])


@pytest.mark.parametrize(
    "value",
    [
        None,
        True,
        False,
        [],
        [1, 2, 3],
        [1, 2, 3, 4, 5],
        [1, 2, "3", 4],
        [1, 2, -3, 4],
        {},
        "AUTO",
        "1,2,3,4",
    ],
)
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
    thresholds = {
        "auto_valley_max_relief_m": 2.0,
        "auto_valley_max_slope_percent": 0.4,
        "auto_valley_max_height_m": 40.0,
        "auto_valley_max_height_percent": 6.0,
    }
    parser = create_parser({
        "route_boundary_percent": "auto",
        "auto_boundary_max_distance_km": 10,
        **thresholds,
    })
    configs = configs_from_args(
        parser.parse_args([str(tmp_path), "--auto-valley-max-relief-m", "4"]),
        parser,
    )
    assert len(configs) == 2
    assert all(config.resolved_route_boundary_percent == "auto" for config in configs)
    assert all(config.auto_boundary_max_distance_km == 10 for config in configs)
    thresholds["auto_valley_max_relief_m"] = 4.0
    for name, value in thresholds.items():
        assert all(getattr(config, name) == value for config in configs)


@pytest.mark.parametrize("name", VALLEY_DEFAULTS)
@pytest.mark.parametrize("value", [0, 0.5, 100])
def test_valley_thresholds_accept_numeric_json(
    simple_gpx: Path, tmp_path: Path, name: str, value: float,
) -> None:
    path = tmp_path / "valley-settings.json"
    path.write_text(json.dumps({name: value}), encoding="utf-8")
    parser = create_parser(load_settings(path))
    config = config_from_args(parser.parse_args([str(simple_gpx)]), parser)
    assert getattr(config, name) == value


@pytest.mark.parametrize("name", VALLEY_DEFAULTS)
@pytest.mark.parametrize("value", ["0", "0.5", "100"])
def test_valley_thresholds_cli_override_json(
    simple_gpx: Path, tmp_path: Path, name: str, value: str,
) -> None:
    path = tmp_path / "valley-settings.json"
    path.write_text(json.dumps({name: 2}), encoding="utf-8")
    parser = create_parser(load_settings(path))
    config = config_from_args(
        parser.parse_args([str(simple_gpx), "--" + name.replace("_", "-"), value]),
        parser,
    )
    assert getattr(config, name) == float(value)


@pytest.mark.parametrize("name", VALLEY_DEFAULTS)
@pytest.mark.parametrize("value", ["-1", "nan", "inf", "-inf", "true", "auto"])
def test_valley_thresholds_reject_invalid_cli(
    simple_gpx: Path, name: str, value: str,
) -> None:
    parser = create_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([str(simple_gpx), f"--{name.replace('_', '-')}={value}"])


@pytest.mark.parametrize("name", VALLEY_DEFAULTS)
@pytest.mark.parametrize(
    "value",
    [-1, float("nan"), float("inf"), -float("inf"), True, False, None, [], {}, "bad", "1"],
)
def test_valley_thresholds_reject_invalid_json(
    simple_gpx: Path, tmp_path: Path, name: str, value: object,
) -> None:
    path = tmp_path / "invalid-valley-settings.json"
    path.write_text(json.dumps({name: value}), encoding="utf-8")
    parser = create_parser(load_settings(path))
    with pytest.raises(SystemExit):
        config_from_args(parser.parse_args([str(simple_gpx)]), parser)


@pytest.mark.parametrize("value", [100.01, 101, 1000])
@pytest.mark.parametrize("source", ["cli", "json"])
def test_valley_height_percentage_cannot_exceed_100(
    simple_gpx: Path, tmp_path: Path, value: float, source: str,
) -> None:
    settings = tmp_path / "height-settings.json"
    settings.write_text(
        json.dumps({"auto_valley_max_height_percent": value}), encoding="utf-8",
    )
    parser = create_parser(load_settings(settings) if source == "json" else {})
    arguments = [str(simple_gpx)]
    if source == "cli":
        arguments.extend(["--auto-valley-max-height-percent", str(value)])
    with pytest.raises(SystemExit):
        config_from_args(parser.parse_args(arguments), parser)


@pytest.mark.parametrize("source", ["cli", "json"])
def test_valley_slope_percentage_can_exceed_100(
    simple_gpx: Path, source: str,
) -> None:
    parser = create_parser({"auto_valley_max_slope_percent": 125} if source == "json" else {})
    arguments = [str(simple_gpx)]
    if source == "cli":
        arguments.extend(["--auto-valley-max-slope-percent", "125"])
    config = config_from_args(parser.parse_args(arguments), parser)
    assert config.auto_valley_max_slope_percent == 125


@pytest.mark.parametrize("topo", [True, False])
def test_explicit_valley_controls_do_not_gate_numeric_boundary(
    simple_gpx: Path, topo: bool,
) -> None:
    parser = create_parser({name: 0 for name in VALLEY_DEFAULTS})
    config = config_from_args(
        parser.parse_args([
            str(simple_gpx), "--route-boundary-percent", "10",
            "--topo" if topo else "--no-topo",
        ]),
        parser,
    )
    assert config.resolved_route_boundary_percent == 10
    assert all(getattr(config, name) == 0 for name in VALLEY_DEFAULTS)


@pytest.mark.parametrize("name", VALLEY_DEFAULTS)
def test_valley_cli_can_override_invalid_json_type(simple_gpx: Path, name: str) -> None:
    parser = create_parser({name: "1"})
    config = config_from_args(
        parser.parse_args([str(simple_gpx), "--" + name.replace("_", "-"), "2"]),
        parser,
    )
    assert getattr(config, name) == 2
