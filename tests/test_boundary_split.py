from __future__ import annotations

import json
from dataclasses import fields, replace
from pathlib import Path

import numpy as np
import pytest
import trimesh
from shapely.geometry import LineString, MultiPoint, Polygon

import gpx2stl.pipeline
from gpx2stl.cli import config_from_args, create_parser, load_settings
from gpx2stl.custom_base import prepare_custom_base
from gpx2stl.errors import Gpx2StlError
from gpx2stl.footprint import (
    add_route_clearance,
    create_footprint,
    create_model_transform,
)
from gpx2stl.gpx import project_paths, read_gpx
from gpx2stl.models import Config
from gpx2stl.pipeline import convert


@pytest.mark.parametrize(
    ("name", "values"),
    [
        ("route_boundary_percent", [0.0, 10.0, 50.0, 125.0]),
        ("text_boundary_percent", [0.0, 15.0, 49.999]),
    ],
)
def test_boundary_cli_accepts_valid_values(
    simple_gpx: Path, name: str, values: list[float]
) -> None:
    for value in values:
        parser = create_parser()
        config = config_from_args(
            parser.parse_args(
                [str(simple_gpx), "--no-topo", "--" + name.replace("_", "-"), str(value)]
            ),
            parser,
        )
        assert getattr(config, name) == value


@pytest.mark.parametrize("name", ["route_boundary_percent", "text_boundary_percent"])
@pytest.mark.parametrize("value", ["-1", "nan", "inf", "-inf", "wide"])
def test_boundary_cli_rejects_invalid_values(
    simple_gpx: Path, name: str, value: str
) -> None:
    parser = create_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(
            [str(simple_gpx), "--no-topo", "--" + name.replace("_", "-") + "=" + value]
        )


@pytest.mark.parametrize("value", ["50", "51", "100"])
def test_text_boundary_cli_rejects_collapsed_terrain(
    simple_gpx: Path, value: str
) -> None:
    with pytest.raises(SystemExit):
        create_parser().parse_args(
            [str(simple_gpx), "--no-topo", "--text-boundary-percent", value]
        )


@pytest.mark.parametrize("name", ["route_boundary_percent", "text_boundary_percent"])
@pytest.mark.parametrize(
    "value", [-1, float("nan"), float("inf"), float("-inf"), True, None, "wide", [], {}]
)
def test_boundary_json_rejects_invalid_values(
    simple_gpx: Path, tmp_path: Path, name: str, value: object
) -> None:
    settings = tmp_path / "boundary-settings.json"
    settings.write_text(json.dumps({name: value, "topo": False}), encoding="utf-8")
    parser = create_parser(load_settings(settings))
    with pytest.raises(SystemExit):
        config_from_args(parser.parse_args([str(simple_gpx)]), parser)


@pytest.mark.parametrize("value", [50, 100])
def test_text_boundary_json_rejects_collapsed_terrain(
    simple_gpx: Path, value: float
) -> None:
    parser = create_parser({"topo": False, "text_boundary_percent": value})
    with pytest.raises(SystemExit):
        config_from_args(parser.parse_args([str(simple_gpx)]), parser)


def test_boundary_settings_and_cli_override_independently(
    simple_gpx: Path, tmp_path: Path
) -> None:
    settings = tmp_path / "boundary-settings.json"
    settings.write_text(
        '{"route_boundary_percent": 25, "text_boundary_percent": 20, "topo": false}',
        encoding="utf-8",
    )
    for arguments, expected in [
        ([], (25.0, 20.0)),
        (["--route-boundary-percent", "0"], (0.0, 20.0)),
        (["--text-boundary-percent", "0"], (25.0, 0.0)),
        (
            ["--route-boundary-percent", "35", "--text-boundary-percent", "12.5"],
            (35.0, 12.5),
        ),
    ]:
        parser = create_parser(load_settings(settings))
        config = config_from_args(parser.parse_args([str(simple_gpx), *arguments]), parser)
        assert (config.route_boundary_percent, config.text_boundary_percent) == expected


def test_directional_boundary_is_rejected_with_custom_base(
    simple_gpx: Path, tmp_path: Path
) -> None:
    base = tmp_path / "base.stl"
    base.touch()
    parser = create_parser()

    with pytest.raises(SystemExit):
        config_from_args(
            parser.parse_args(
                [
                    str(simple_gpx),
                    "--base-stl",
                    str(base),
                    "--route-boundary-percent",
                    "10,10,10,10",
                ]
            ),
            parser,
        )


@pytest.mark.parametrize(
    ("route_boundary_percent", "text_boundary_percent"),
    [(0, 0), (10, 15), (125, 49.999)],
)
def test_boundary_json_accepts_valid_numeric_values(
    simple_gpx: Path, tmp_path: Path, route_boundary_percent: float,
    text_boundary_percent: float,
) -> None:
    settings = tmp_path / "boundary-settings.json"
    settings.write_text(
        json.dumps(
            {
                "route_boundary_percent": route_boundary_percent,
                "text_boundary_percent": text_boundary_percent,
                "topo": False,
            }
        ),
        encoding="utf-8",
    )
    parser = create_parser(load_settings(settings))
    config = config_from_args(parser.parse_args([str(simple_gpx)]), parser)
    assert config.route_boundary_percent == route_boundary_percent
    assert config.text_boundary_percent == text_boundary_percent


@pytest.mark.parametrize(
    ("retired", "replacement"),
    [
        ("boundary_percent", "route_boundary_percent"),
        ("inner_size_percent", "text_boundary_percent"),
    ],
)
def test_retired_cli_options_report_migration(
    simple_gpx: Path, capsys: pytest.CaptureFixture[str], retired: str, replacement: str
) -> None:
    with pytest.raises(SystemExit):
        create_parser().parse_args(
            [str(simple_gpx), "--" + retired.replace("_", "-"), "10"]
        )
    message = capsys.readouterr().err
    assert retired.replace("_", "-") in message
    assert replacement.replace("_", "-") in message or replacement in message
    assert "retired" in message.lower() or "removed" in message.lower()
    if retired == "inner_size_percent":
        assert "100" in message and "/ 2" in message


@pytest.mark.parametrize("retired", ["boundary_percent", "inner_size_percent"])
def test_retired_json_settings_report_migration(
    tmp_path: Path, retired: str
) -> None:
    settings = tmp_path / "retired-settings.json"
    settings.write_text(
        f'{{"{retired}": 10, "route_boundary_percent": 20, "text_boundary_percent": 15}}',
        encoding="utf-8",
    )
    with pytest.raises(Gpx2StlError, match=retired) as error:
        load_settings(settings)
    assert "retired" in str(error.value).lower() or "removed" in str(error.value).lower()
    assert "text_boundary_percent" in str(error.value)
    if retired == "inner_size_percent":
        assert "100" in str(error.value) and "/ 2" in str(error.value)
    else:
        assert "route_boundary_percent" in str(error.value)


@pytest.mark.parametrize("retired", ["boundary_percent", "inner_size_percent"])
def test_retired_programmatic_settings_are_rejected(retired: str) -> None:
    with pytest.raises(Gpx2StlError, match=retired):
        create_parser({retired: 10})


@pytest.mark.parametrize("topo", [False, True])
def test_config_exposes_only_split_boundary_fields(simple_gpx: Path, topo: bool) -> None:
    config = Config(gpx_file=simple_gpx, output=simple_gpx.with_suffix(".3mf"), topo=topo)
    assert config.route_boundary_percent is None
    assert config.resolved_route_boundary_percent == ("search" if topo else 10.0)
    assert config.auto_boundary_max_distance_km == 10.0
    assert config.text_boundary_percent == 7.0
    names = {field.name for field in fields(Config)}
    assert "boundary_percent" not in names
    assert "inner_size_percent" not in names


def test_programmatic_city_default_uses_directional_padding(
    simple_gpx: Path,
) -> None:
    config = Config(
        gpx_file=simple_gpx,
        output=simple_gpx.with_suffix(".3mf"),
        mode="city",
    )

    assert config.resolved_route_boundary_percent == (10.0, 10.0, 10.0, 10.0)
    for retired in ("boundary_percent", "inner_size_percent"):
        with pytest.raises(TypeError):
            Config(gpx_file=simple_gpx, output=config.output, **{retired: 10})


def test_cli_help_exposes_only_split_boundary_options() -> None:
    help_text = create_parser().format_help()
    assert "--route-boundary-percent" in help_text
    assert "--auto-boundary-max-distance-km" in help_text
    assert "--text-boundary-percent" in help_text
    assert "--boundary-percent" not in help_text
    assert "--inner-size-percent" not in help_text


@pytest.mark.parametrize("text_boundary_percent", [0.0, 15.0, 49.999])
@pytest.mark.parametrize("text", [None, "I"])
def test_config_terrain_size_uses_only_text_boundary_when_text_is_present(
    simple_gpx: Path, text_boundary_percent: float, text: str | None,
) -> None:
    config = Config(
        gpx_file=simple_gpx, output=simple_gpx.with_suffix(".3mf"),
        max_size=40, text=text, text_boundary_percent=text_boundary_percent,
        route_boundary_percent=125,
    )
    expected = 40 if text is None else 40 * (1 - 2 * text_boundary_percent / 100)
    assert config.terrain_size == pytest.approx(expected)


@pytest.mark.parametrize("text_boundary_percent", [0.0, 5.0])
def test_hex_text_boundary_must_fit_inside_frame(
    simple_gpx: Path, capsys: pytest.CaptureFixture[str], text_boundary_percent: float,
) -> None:
    parser = create_parser()
    with pytest.raises(SystemExit):
        config_from_args(
            parser.parse_args(
                [
                    str(simple_gpx), "--no-topo", "--shape", "hex", "--text", "I",
                    "--text-boundary-percent", str(text_boundary_percent),
                ]
            ),
            parser,
        )
    assert "--text-boundary-percent" in capsys.readouterr().err


def test_hex_without_text_ignores_zero_text_boundary(simple_gpx: Path) -> None:
    parser = create_parser()
    config = config_from_args(
        parser.parse_args(
            [str(simple_gpx), "--no-topo", "--shape", "hex", "--text-boundary-percent", "0"]
        ),
        parser,
    )
    assert config.terrain_size == config.max_size


class FlatDem:
    def sample_projected(self, points, route):
        return np.zeros(len(points))


def _capture_conversion(config: Config, monkeypatch: pytest.MonkeyPatch):
    captured = {}
    original_build = gpx2stl.pipeline.build_geometry

    def build(route, footprint, transform, config, dem, custom_base):
        geometry = original_build(route, footprint, transform, config, dem, custom_base)
        captured.update(
            route=route, footprint=footprint, transform=transform,
            custom=custom_base, geometry=geometry,
        )
        return geometry

    with monkeypatch.context() as patch:
        patch.setattr(gpx2stl.pipeline, "resolve_dem", lambda *args: FlatDem())
        patch.setattr(gpx2stl.pipeline, "build_geometry", build)
        patch.setattr(gpx2stl.pipeline, "export_geometry", lambda *args: None)
        convert(config, progress=lambda message: None)
    geometry = captured["geometry"]
    for mesh in (geometry.base, geometry.route, geometry.topography, geometry.text):
        if mesh is not None:
            assert mesh.is_watertight
            assert mesh.volume > 0
    assert geometry.topography is not None
    return captured


@pytest.mark.parametrize("shape", ["square", "circle", "hex"])
@pytest.mark.parametrize("text", [None, "I"])
def test_generated_route_padding_preserves_terrain_and_text_size(
    simple_gpx: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shape: str,
    text: str | None,
) -> None:
    config = Config(
        gpx_file=simple_gpx, output=tmp_path / "unused.3mf", shape=shape,
        max_size=40, text=text, route_boundary_percent=0, text_boundary_percent=15,
    )
    unpadded = _capture_conversion(config, monkeypatch)
    padded = _capture_conversion(replace(config, route_boundary_percent=30), monkeypatch)
    assert padded["transform"].scale < unpadded["transform"].scale
    for name in ("base", "topography", "text"):
        first = getattr(unpadded["geometry"], name)
        second = getattr(padded["geometry"], name)
        if first is not None:
            assert np.allclose(first.bounds, second.bounds)
    terrain_size = 28.0 if text else 40.0
    assert np.ptp(padded["geometry"].topography.bounds[:, 0]) == pytest.approx(terrain_size)
    expected = create_footprint(
        padded["route"].points, "circle" if text else shape, 30,
        config.route_width / terrain_size,
    )
    expected = add_route_clearance(expected, config.route_width, terrain_size)
    assert padded["transform"].scale == pytest.approx(
        create_model_transform(expected, terrain_size).scale
    )
    terrain = MultiPoint(padded["geometry"].topography.vertices[:, :2]).convex_hull
    for path in padded["route"].paths:
        assert terrain.buffer(1e-6).covers(
            LineString(padded["transform"].to_model(path)).buffer(config.route_width / 2)
        )


@pytest.mark.parametrize("text_boundary_percent", [10.0, 15.0, 25.0])
def test_generated_text_boundary_sets_per_side_inset(
    simple_gpx: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    text_boundary_percent: float,
) -> None:
    config = Config(
        gpx_file=simple_gpx, output=tmp_path / "unused.3mf",
        max_size=40, shape="circle", text="I", text_boundary_percent=text_boundary_percent,
        route_boundary_percent=10,
    )
    captured = _capture_conversion(config, monkeypatch)
    diameter = 40 * (1 - 2 * text_boundary_percent / 100)
    assert np.ptp(captured["geometry"].topography.bounds[:, 0]) == pytest.approx(diameter)
    assert np.ptp(captured["geometry"].base.bounds[:, 0]) == pytest.approx(40)
    assert config.route_boundary_percent == 10


@pytest.mark.parametrize("custom", [False, True])
def test_without_text_text_boundary_does_not_change_geometry(
    simple_gpx: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, custom: bool,
) -> None:
    base = None
    if custom:
        base = tmp_path / "base.stl"
        trimesh.creation.extrude_polygon(
            Polygon([(-30, -20), (30, -20), (30, 20), (-30, 20)]), 5
        ).export(base, file_type="stl")
    config = Config(
        gpx_file=simple_gpx, output=tmp_path / "unused.3mf", max_size=40,
        base_stl=base, text_boundary_percent=0,
        route_boundary_percent=10,
    )
    first = _capture_conversion(config, monkeypatch)
    second = _capture_conversion(replace(config, text_boundary_percent=49), monkeypatch)
    assert first["transform"].scale == pytest.approx(second["transform"].scale)
    for name in ("base", "route", "topography"):
        assert np.allclose(
            getattr(first["geometry"], name).vertices,
            getattr(second["geometry"], name).vertices,
        )
    if custom:
        assert second["custom"].inset_distance == 0
        assert second["custom"].terrain_polygon.equals(second["custom"].top_polygon)


@pytest.mark.parametrize("text", [None, "I"])
def test_custom_route_padding_changes_fit_not_terrain_or_base(
    simple_gpx: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str | None,
) -> None:
    base = tmp_path / "base.stl"
    trimesh.creation.extrude_polygon(
        Polygon([(-30, -20), (30, -20), (30, 20), (-30, 20)]), 5
    ).export(base, file_type="stl")
    config = Config(
        gpx_file=simple_gpx, output=tmp_path / "unused.3mf", base_stl=base,
        text=text, route_boundary_percent=0, text_boundary_percent=15,
    )
    first = _capture_conversion(config, monkeypatch)
    second = _capture_conversion(replace(config, route_boundary_percent=25), monkeypatch)
    assert second["transform"].scale == pytest.approx(first["transform"].scale / 1.25)
    assert first["custom"].terrain_polygon.equals(second["custom"].terrain_polygon)
    assert second["custom"].inset_distance == pytest.approx(6 if text else 0)
    assert np.allclose(first["geometry"].base.bounds, second["geometry"].base.bounds)
    assert np.allclose(first["geometry"].topography.bounds, second["geometry"].topography.bounds)
    if text:
        assert np.allclose(first["geometry"].text.bounds, second["geometry"].text.bounds)
    for path in second["route"].paths:
        assert second["custom"].terrain_polygon.buffer(1e-6).covers(
            LineString(second["transform"].to_model(path)).buffer(config.route_width / 2)
        )


def test_custom_text_boundary_changes_inset_without_resizing_base(
    simple_gpx: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = tmp_path / "base.stl"
    trimesh.creation.extrude_polygon(
        Polygon([(-30, -20), (30, -20), (30, 20), (-30, 20)]), 5
    ).export(base, file_type="stl")
    config = Config(
        gpx_file=simple_gpx, output=tmp_path / "unused.3mf", base_stl=base,
        text="I", text_boundary_percent=10, route_boundary_percent=25,
    )
    first = _capture_conversion(config, monkeypatch)
    second = _capture_conversion(replace(config, text_boundary_percent=20), monkeypatch)
    assert first["custom"].inset_distance == pytest.approx(4)
    assert second["custom"].inset_distance == pytest.approx(8)
    assert first["custom"].terrain_polygon.bounds == pytest.approx((-26, -16, 26, 16))
    assert second["custom"].terrain_polygon.bounds == pytest.approx((-22, -12, 22, 12))
    assert np.allclose(first["geometry"].base.bounds, second["geometry"].base.bounds)
    assert second["transform"].scale < first["transform"].scale
    route = second["route"]
    maximum_fit = prepare_custom_base(base, 20, route, config.route_width)
    assert second["transform"].scale == pytest.approx(maximum_fit.transform.scale / 1.25)


@pytest.mark.parametrize("route_width", [1.0, 4.0])
@pytest.mark.parametrize("route_boundary_percent", [0.0, 10.0, 100.0])
def test_custom_helper_default_is_unpadded_and_padding_preserves_width_clearance(
    simple_gpx: Path, tmp_path: Path, route_width: float, route_boundary_percent: float,
) -> None:
    base = tmp_path / "base.stl"
    trimesh.creation.extrude_polygon(
        Polygon([(-30, -20), (30, -20), (30, 20), (-30, 20)]), 5
    ).export(base, file_type="stl")
    route = project_paths(read_gpx(simple_gpx))
    unpadded = prepare_custom_base(base, 15, route, route_width)
    padded = prepare_custom_base(
        base, 15, route, route_width, route_boundary_percent=route_boundary_percent,
    )
    assert padded.transform.scale == pytest.approx(
        unpadded.transform.scale / (1 + route_boundary_percent / 100)
    )
    assert padded.terrain_polygon.equals(unpadded.terrain_polygon)
    for path in route.paths:
        assert padded.terrain_polygon.buffer(1e-6).covers(
            LineString(padded.transform.to_model(path)).buffer(route_width / 2)
        )


@pytest.mark.parametrize("route_boundary_percent", [10.0, 100.0])
def test_custom_padding_preserves_containment_on_concave_top(
    simple_gpx: Path, tmp_path: Path, route_boundary_percent: float,
) -> None:
    base = tmp_path / "concave-base.stl"
    trimesh.creation.extrude_polygon(
        Polygon([(-30, -20), (30, -20), (30, 20), (0, 12), (-30, 20)]), 5
    ).export(base, file_type="stl")
    route = project_paths(read_gpx(simple_gpx))
    custom = prepare_custom_base(
        base, 5, route, 1, route_boundary_percent=route_boundary_percent,
    )
    for path in route.paths:
        assert custom.terrain_polygon.buffer(1e-6).covers(
            LineString(custom.transform.to_model(path)).buffer(0.5)
        )
