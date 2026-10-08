from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import rasterio
import shapely
import trimesh
from pyproj import CRS, Transformer
from rasterio.transform import from_bounds
from shapely.geometry import LineString, Polygon, box

import gpx2stl.pipeline as pipeline
from gpx2stl.auto_boundary import AutoBoundaryResult
from gpx2stl.custom_base import _fit_transform, prepare_custom_base
from gpx2stl.dem import DemSource, DemTile
from gpx2stl.errors import Gpx2StlError
from gpx2stl.footprint import (
    add_route_clearance,
    create_footprint,
    create_model_transform,
)
from gpx2stl.models import Config, Footprint, ModelTransform, ProjectedRoute


def _route(points=None) -> ProjectedRoute:
    if points is None:
        points = [(-100.0, -50.0), (100.0, 50.0)]
    path = np.asarray(points, dtype=np.float64)
    return ProjectedRoute(
        (path,),
        (np.linspace(100.0, 120.0, len(path)),),
        Transformer.from_crs(4326, 3857, always_xy=True),
        Transformer.from_crs(3857, 4326, always_xy=True),
    )


def _dem(bounds=(-10000.0, -10000.0, 10000.0, 10000.0), data=None) -> DemSource:
    if data is None:
        data = np.arange(4096, dtype=np.float64).reshape(64, 64)
    return DemSource(
        (
            DemTile(
                data,
                from_bounds(*bounds, data.shape[1], data.shape[0]),
                rasterio.coords.BoundingBox(*bounds),
                CRS.from_epsg(3857),
                "synthetic DEM",
            ),
        ),
        description="synthetic DEM",
    )


def _mock_conversion(monkeypatch, result, route=None, final_dem=None):
    route = _route() if route is None else route
    calls = {"resolve": [], "discover": [], "build": [], "export": []}
    monkeypatch.setattr(pipeline, "read_activity", lambda _: (SimpleNamespace(
        latitude=np.zeros(len(route.points)),
    ),))
    monkeypatch.setattr(pipeline, "project_paths", lambda _: route)

    def discover(actual_route, cap, load_dem, progress):
        calls["discover"].append((actual_route, cap))
        if final_dem is not None:
            load_dem((SimpleNamespace(south=0, north=1, west=0, east=1),))
        return result

    def resolve(config, bounds, progress):
        calls["resolve"].append(bounds)
        return final_dem or result.dem

    def build(*args):
        calls["build"].append(args)
        mesh = trimesh.creation.box()
        return SimpleNamespace(base=mesh, topography=None, route=mesh, text=None)

    monkeypatch.setattr(pipeline, "discover_auto_boundary", discover)
    monkeypatch.setattr(pipeline, "resolve_dem", resolve)
    monkeypatch.setattr(pipeline, "build_geometry", build)
    monkeypatch.setattr(pipeline, "export_geometry", lambda *args: calls["export"].append(args))
    return calls


@pytest.mark.parametrize("shape", ["square", "circle", "hex"])
@pytest.mark.parametrize("text_mode", [None, "raised", "embedded"])
def test_auto_fits_entire_envelope_preserving_shape_and_text(
    monkeypatch, shape, text_mode
) -> None:
    envelope = box(-200.0, -100.0, 1400.0, 700.0)
    result = AutoBoundaryResult(envelope, _dem(), 2, 4000.0)
    calls = _mock_conversion(monkeypatch, result)
    config = Config(
        Path("activity.gpx"), Path("model.3mf"), shape=shape,
        text="TRAIL" if text_mode else None, text_mode=text_mode or "raised",
        max_size=20.0, route_boundary_percent="auto", auto_boundary_max_distance_km=12,
    )
    messages = []
    pipeline.convert(config, messages.append)
    route, footprint, transform, actual_config, dem, custom = calls["build"][0]
    assert actual_config is config
    assert calls["discover"] == [(route, 12)]
    assert custom is None
    assert footprint.shape == ("circle" if text_mode else shape)
    assert footprint.diameter * transform.scale == pytest.approx(config.terrain_size)
    assert transform.footprint.center[0] > route.points[:, 0].max()
    pipeline._validate_auto_fit(envelope, route, footprint, transform, None, config.route_width)
    assert np.all(np.isfinite(dem.sample_projected(route.points, route)))
    assert not calls["resolve"]
    assert len(calls["export"]) == 1
    assert any("Detected 2 mountain regions" in message for message in messages)
    assert any("configured cap 12 km" in message for message in messages)
    padding = next(message for message in messages if "geographic clearances" in message)
    for side in ("west", "east", "south", "north"):
        assert side in padding
    assert "%" in padding


def test_dense_custom_route_uses_precomputed_terrain_clearance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = tmp_path / "dense-base.stl"
    trimesh.creation.extrude_polygon(
        box(-30, -20, 30, 20), 5
    ).export(base, file_type="stl")
    x = np.linspace(-100, 100, 30_000)
    route = _route(np.column_stack((x, np.sin(x / 10) * 10)))
    envelope = box(-500, -500, 500, 500)
    original_buffer = shapely.geometry.base.BaseGeometry.buffer

    def buffer_without_dense_ribbon(geometry, *args, **kwargs):
        if geometry.geom_type in {"LineString", "MultiLineString"}:
            pytest.fail("Dense route ribbons must not be buffered during scale search")
        return original_buffer(geometry, *args, **kwargs)

    monkeypatch.setattr(
        shapely.geometry.base.BaseGeometry, "buffer", buffer_without_dense_ribbon
    )
    custom = prepare_custom_base(base, 15, route, 1, projected_envelope=envelope)
    pipeline._validate_auto_fit(
        envelope, route, custom.transform.footprint, custom.transform, custom, 1
    )
    assert custom.transform.scale > 0


def test_auto_loads_final_perimeter_beyond_discovery_dem(monkeypatch) -> None:
    envelope = box(-200.0, -100.0, 1400.0, 700.0)
    result = AutoBoundaryResult(envelope, _dem(envelope.bounds), 1, 2000.0)
    final = _dem()
    calls = _mock_conversion(monkeypatch, result, final_dem=final)
    pipeline.convert(Config(Path("activity.gpx"), Path("model.3mf")), lambda _: None)
    assert len(calls["resolve"]) == 2  # Discovery callback and final terrain coverage.
    route, footprint, transform, _, dem, _ = calls["build"][0]
    perimeter = pipeline._generated_perimeter(footprint)
    assert perimeter[:, 1].min() < envelope.bounds[1]
    assert perimeter[:, 1].max() > envelope.bounds[3]
    assert dem.tiles is final.tiles
    assert np.all(np.isfinite(dem.sample_projected(perimeter, route)))


def test_auto_missing_final_perimeter_fails_before_mesh(monkeypatch) -> None:
    envelope = box(-200.0, -100.0, 1400.0, 700.0)
    partial = _dem(envelope.bounds)
    calls = _mock_conversion(monkeypatch, AutoBoundaryResult(envelope, partial, 1, 2000))
    with pytest.raises(Gpx2StlError, match="coverage is incomplete"):
        pipeline.convert(Config(Path("activity.gpx"), Path("model.3mf")), lambda _: None)
    assert len(calls["resolve"]) == 1
    assert not calls["build"]
    assert not calls["export"]


def test_auto_dem_guard_rejects_interior_nodata_instead_of_mesh_filling(monkeypatch) -> None:
    data = np.ones((64, 64))
    data[30:34, 30:34] = np.nan
    source = _dem(data=data)
    result = AutoBoundaryResult(box(-4000, -4000, 4000, 4000), source, 1, 5000)
    calls = _mock_conversion(monkeypatch, result)

    def build(route, footprint, transform, config, dem, custom):
        # The existing mesh fill path must never receive this missing height.
        dem.sample_projected(np.array([[0.0, 0.0]]), route)
        pytest.fail("Missing elevation must be rejected before nearest-value filling")

    monkeypatch.setattr(pipeline, "build_geometry", build)
    with pytest.raises(Gpx2StlError, match="complete finite DEM coverage"):
        pipeline.convert(Config(Path("activity.gpx"), Path("model.3mf")), lambda _: None)
    assert not calls["export"]


def test_auto_perimeter_nodata_does_not_reuse_discovery_dem(monkeypatch) -> None:
    data = np.full((64, 64), np.nan)
    missing = _dem(data=data)
    final = _dem()
    result = AutoBoundaryResult(box(-200, -100, 1400, 700), missing, 1, 2000)
    calls = _mock_conversion(monkeypatch, result)
    monkeypatch.setattr(pipeline, "resolve_dem", lambda *args: final)
    pipeline.convert(Config(Path("activity.gpx"), Path("model.3mf")), lambda _: None)
    assert calls["build"][0][4].tiles is final.tiles


def test_auto_no_topography_is_programmatic_error(monkeypatch) -> None:
    monkeypatch.setattr(pipeline, "read_activity", lambda _: pytest.fail("Must fail first"))
    with pytest.raises(Gpx2StlError, match="require topography"):
        pipeline.convert(Config(
            Path("activity.gpx"), Path("model.3mf"), topo=False,
            route_boundary_percent="auto",
        ), lambda _: None)


def test_auto_clearance_zero_span_is_explicit(monkeypatch) -> None:
    route = _route([(0.0, -50.0), (0.0, 50.0)])
    result = AutoBoundaryResult(box(-100, -100, 1000, 1000), _dem(), 1, 2000)
    _mock_conversion(monkeypatch, result, route)
    messages = []
    pipeline.convert(Config(Path("activity.gpx"), Path("model.3mf")), messages.append)
    message = next(item for item in messages if "geographic clearances" in item)
    assert message.count("percentage undefined: zero route span") == 2
    assert "south" in message and "north" in message


@pytest.mark.parametrize("shape", ["square", "circle", "hex"])
def test_numeric_pipeline_geometry_is_unchanged(monkeypatch, shape) -> None:
    route = _route()
    result = AutoBoundaryResult(box(-200, -100, 1400, 700), _dem(), 1, 2000)
    calls = _mock_conversion(monkeypatch, result, route)
    monkeypatch.setattr(pipeline, "discover_auto_boundary", lambda *args: pytest.fail("Numeric"))
    config = Config(
        Path("activity.gpx"), Path("model.3mf"), shape=shape,
        max_size=20, route_boundary_percent=10,
    )
    expected = add_route_clearance(
        create_footprint(route.points, shape, 10, config.route_width / config.terrain_size),
        config.route_width, config.terrain_size,
    )
    pipeline.convert(config, lambda _: None)
    footprint, transform = calls["build"][0][1:3]
    assert footprint.shape == expected.shape
    assert footprint.radius == expected.radius
    assert np.array_equal(footprint.center, expected.center)
    assert transform.scale == create_model_transform(expected, config.terrain_size).scale
    assert type(calls["build"][0][4]) is DemSource


@pytest.mark.parametrize("terrain", [
    box(-30, -20, 30, 20),
    Polygon([(-30, -20), (30, -20), (30, 20), (0, 5), (-30, 20)]),
    Polygon(
        [(-30, -20), (30, -20), (30, 20), (-30, 20)],
        holes=[[(-5, -5), (5, -5), (5, 5), (-5, 5)]],
    ),
])
def test_custom_auto_fits_full_polygon_and_complete_ribbon(terrain) -> None:
    route = _route([(-100, 0), (100, 0), (1000, 200)])
    envelope = box(-200, -500, 1400, 900)
    transform = _fit_transform(route, terrain, 1.0, 0.0, envelope)
    assert terrain.covers(shapely.transform(envelope, transform.to_model))
    ribbon = LineString(transform.to_model(route.points)).buffer(0.5, quad_segs=32)
    assert terrain.covers(ribbon)
    assert not np.array_equal(transform.footprint.center, route.points.mean(axis=0))


def test_custom_auto_checks_edges_and_interior_not_only_envelope_vertices() -> None:
    terrain = box(-30, -20, 30, 20).difference(box(-5, -5, 5, 5))
    route = _route()
    envelope = box(-1000, -1000, 1000, 1000)
    transform = _fit_transform(route, terrain, 1.0, 0.0, envelope)
    model_envelope = shapely.transform(envelope, transform.to_model)
    assert terrain.covers(model_envelope)
    assert not model_envelope.intersects(Polygon(terrain.interiors[0]))
    # A deliberately oversized polygon has valid corner vertices but spans the hole.
    assert all(terrain.covers(shapely.Point(point)) for point in
               [(-20, -10), (20, -10), (20, 10), (-20, 10)])
    assert not terrain.covers(box(-20, -10, 20, 10))


def test_final_fit_guard_rejects_envelope_crossing_custom_hole() -> None:
    terrain = box(-30, -20, 30, 20).difference(box(-5, -5, 5, 5))
    route = _route([(-100, -80), (100, -80)])
    envelope = box(-200, -100, 200, 100)
    footprint = Footprint("circle", np.zeros(2), 250)
    transform = ModelTransform(footprint, 0.1)
    custom = SimpleNamespace(terrain_polygon=terrain)
    with pytest.raises(Gpx2StlError, match="entire detected terrain envelope"):
        pipeline._validate_auto_fit(envelope, route, footprint, transform, custom, 1.0)


def test_custom_auto_impossible_route_width_fails() -> None:
    with pytest.raises(Gpx2StlError, match="too narrow"):
        _fit_transform(_route(), box(-1, -1, 1, 1), 3.0, 0.0, box(-100, -100, 100, 100))


def test_custom_numeric_padding_semantics_are_unchanged() -> None:
    route = _route()
    terrain = box(-30, -20, 30, 20)
    unpadded = _fit_transform(route, terrain, 1.0, 0)
    padded = _fit_transform(route, terrain, 1.0, 10)
    assert padded.scale == pytest.approx(unpadded.scale / 1.1)
    assert np.array_equal(padded.footprint.center, unpadded.footprint.center)
    assert padded.footprint.radius == unpadded.footprint.radius


def test_custom_auto_keeps_mesh_and_text_inset_dimensions(tmp_path) -> None:
    path = tmp_path / "auto-base.stl"
    trimesh.creation.extrude_polygon(box(-30, -20, 30, 20), 5).export(path)
    custom = prepare_custom_base(
        path, 15, _route(), 1.0, projected_envelope=box(-200, -100, 1400, 700)
    )
    assert np.allclose(custom.mesh.extents, [60, 40, 5])
    assert np.allclose(custom.terrain_polygon.bounds, [-24, -14, 24, 14])
    assert custom.inset_distance == 6
    assert custom.top_z == 5


def test_custom_auto_resolves_dem_for_actual_inset_perimeter(monkeypatch, tmp_path) -> None:
    path = tmp_path / "custom.stl"
    trimesh.creation.extrude_polygon(box(-30, -20, 30, 20), 5).export(path)
    envelope = box(-200, -100, 1400, 700)
    result = AutoBoundaryResult(envelope, _dem(envelope.bounds), 1, 2000)
    calls = _mock_conversion(monkeypatch, result, final_dem=_dem())
    pipeline.convert(Config(
        Path("activity.gpx"), Path("model.3mf"), base_stl=path,
        text="TRAIL", text_boundary_percent=15, max_size=999,
    ), lambda _: None)
    route, footprint, transform, config, dem, custom = calls["build"][0]
    assert len(calls["resolve"]) == 2
    assert np.allclose(custom.mesh.extents, [60, 40, 5])
    assert np.allclose(custom.terrain_polygon.bounds, [-24, -14, 24, 14])
    perimeter = transform.to_projected(pipeline.sample_exterior(custom.terrain_polygon))
    assert np.all(np.isfinite(dem.sample_projected(perimeter, route)))
    pipeline._validate_auto_fit(envelope, route, footprint, transform, custom, config.route_width)


@pytest.mark.parametrize("shape", ["square", "circle", "hex"])
def test_auto_real_mesh_stays_watertight_and_has_expected_model_size(monkeypatch, shape) -> None:
    from gpx2stl.mesh import build_geometry

    result = AutoBoundaryResult(box(-200, -100, 1400, 700), _dem(), 1, 2000)
    calls = _mock_conversion(monkeypatch, result)
    built = []

    def build(*args):
        geometry = build_geometry(*args)
        built.append(geometry)
        return geometry

    monkeypatch.setattr(pipeline, "build_geometry", build)
    pipeline.convert(Config(
        Path("activity.gpx"), Path("model.3mf"), shape=shape, max_size=20,
        terrain_height=3,
    ), lambda _: None)
    assert len(calls["export"]) == 1
    geometry = built[0]
    assert geometry.base.is_watertight
    assert geometry.topography.is_watertight
    assert geometry.route.is_watertight
    assert geometry.base.extents[0] == pytest.approx(20, abs=0.01)
    assert geometry.base.extents[1] == pytest.approx(
        20 * (np.sqrt(3) / 2 if shape == "hex" else 1), abs=0.01
    )


@pytest.mark.parametrize("text_mode", ["raised", "embedded"])
def test_auto_real_text_keeps_hex_frame_and_independent_circular_inset(
    monkeypatch, text_mode
) -> None:
    from gpx2stl.mesh import build_geometry

    result = AutoBoundaryResult(box(-200, -100, 1400, 700), _dem(), 1, 2000)
    _mock_conversion(monkeypatch, result)
    built = []

    def build(*args):
        assert args[1].shape == "circle"
        assert args[1].diameter * args[2].scale == pytest.approx(28)
        geometry = build_geometry(*args)
        built.append(geometry)
        return geometry

    monkeypatch.setattr(pipeline, "build_geometry", build)
    pipeline.convert(Config(
        Path("activity.gpx"), Path("model.3mf"), shape="hex", max_size=40,
        text="TRAIL", text_mode=text_mode, text_boundary_percent=15,
        terrain_height=3,
    ), lambda _: None)
    geometry = built[0]
    assert geometry.base.is_watertight
    assert geometry.topography.is_watertight
    assert geometry.text is not None and geometry.text.is_watertight
    assert geometry.base.extents[0] == pytest.approx(40, abs=0.01)
    assert geometry.base.extents[1] == pytest.approx(40 * np.sqrt(3) / 2, abs=0.01)
