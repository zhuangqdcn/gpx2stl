from __future__ import annotations

from pathlib import Path

import lib3mf
import numpy as np
import pytest
import trimesh
from shapely.geometry import LineString, Point, Polygon

from gpx2stl.custom_base import prepare_custom_base
from gpx2stl.errors import Gpx2StlError
from gpx2stl.export import export_geometry
from gpx2stl.gpx import interpolate_elevations, project_paths, read_gpx
from gpx2stl.mesh import (
    _add_side_faces,
    _custom_text_layout,
    _custom_text_polygons,
    _triangulated_polygon_points,
    _triangulated_surface_sampler,
    build_geometry,
)
from gpx2stl.models import Config


class SlopedDem:
    def sample_projected(self, points, route):
        return points[:, 0] * 0.01 + points[:, 1] * 0.02


def test_surface_sampler_uses_custom_terrain_faces() -> None:
    points = np.array(
        [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]],
        dtype=np.float64,
    )
    heights = np.array([0.0, 0.0, 0.0, 1.0], dtype=np.float64)
    sampler = _triangulated_surface_sampler(
        points,
        heights,
        [(0, 1, 2), (0, 2, 3)],
    )
    result = sampler(np.array([[0.75, 0.25], [0.25, 0.75]]))
    assert np.allclose(result, [0.0, 0.5])


def test_custom_triangulation_has_no_long_rooted_face_fans() -> None:
    polygon = Polygon(
        [
            (11.1814089393, 49.7500003179),
            (34.1599199760, 89.5500068665),
            (80.1170575631, 89.5500068665),
            (103.0955685997, 49.7500003179),
            (80.1170579302, 9.9500007629),
            (34.1599196089, 9.9500007629),
        ]
    )
    spacing = 1.0
    points, faces, rings = _triangulated_polygon_points(polygon, spacing)
    face_array = np.asarray(faces, dtype=np.int64)
    triangles = points[face_array]
    edge_lengths = np.concatenate(
        (
            np.linalg.norm(triangles[:, 1] - triangles[:, 0], axis=1),
            np.linalg.norm(triangles[:, 2] - triangles[:, 1], axis=1),
            np.linalg.norm(triangles[:, 0] - triangles[:, 2], axis=1),
        )
    )
    degree = np.bincount(face_array.ravel(), minlength=len(points))
    count = len(points)
    vertices = np.vstack(
        (
            np.column_stack((points, np.ones(count))),
            np.column_stack((points, np.zeros(count))),
        )
    )
    prism_faces: list[tuple[int, int, int]] = []
    for a, b, c in faces:
        prism_faces.extend(((a, b, c), (count + a, count + c, count + b)))
    for ring in rings:
        _add_side_faces(prism_faces, ring, count)
    mesh = trimesh.Trimesh(
        vertices=vertices,
        faces=np.asarray(prism_faces),
        process=True,
    )
    assert np.max(edge_lengths) <= spacing * 2.0
    assert np.max(degree) < 20
    assert mesh.is_watertight
    assert mesh.is_winding_consistent
    assert mesh.volume > 0


def _write_base(path: Path, polygon: Polygon, height: float = 5.0) -> Path:
    mesh = trimesh.creation.extrude_polygon(polygon, height)
    mesh.export(path, file_type="stl")
    return path


def _route(simple_gpx: Path):
    paths = tuple(interpolate_elevations(path) for path in read_gpx(simple_gpx))
    return project_paths(paths)


def test_custom_base_preserves_dimensions_and_insets_exact_top(
    simple_gpx: Path, tmp_path: Path
) -> None:
    path = _write_base(
        tmp_path / "base.stl",
        Polygon([(-30, -20), (30, -20), (30, 20), (-30, 20)]),
    )
    custom = prepare_custom_base(path, 10.0, _route(simple_gpx), 1.0)
    assert np.allclose(custom.mesh.extents, [60.0, 40.0, 5.0])
    assert custom.top_z == 5.0
    assert np.allclose(custom.terrain_polygon.bounds, [-26.0, -16.0, 26.0, 16.0])
    model_route = [
        custom.transform.to_model(path_points)
        for path_points in _route(simple_gpx).paths
    ]
    for path_points in model_route:
        assert custom.terrain_polygon.covers(
            LineString(path_points).buffer(
                0.5,
                cap_style="round",
                join_style="round",
                quad_segs=4,
            )
        )


@pytest.mark.parametrize(
    "polygon",
    [
        Polygon([(-30, -20), (30, -20), (30, 20), (0, 12), (-30, 20)]),
        Polygon(
            [(-30, -20), (30, -20), (30, 20), (-30, 20)],
            holes=[[(-5, -5), (5, -5), (5, 5), (-5, 5)]],
        ),
    ],
)
def test_custom_base_supports_concave_tops_and_holes(
    simple_gpx: Path, tmp_path: Path, polygon: Polygon
) -> None:
    path = _write_base(tmp_path / "base.stl", polygon)
    custom = prepare_custom_base(path, 5.0, _route(simple_gpx), 1.0)
    assert custom.top_polygon.geom_type == "Polygon"
    assert custom.terrain_polygon.geom_type == "Polygon"


def test_custom_terrain_preserves_a_top_hole(
    simple_gpx: Path, tmp_path: Path
) -> None:
    polygon = Polygon(
        [(-30, -20), (30, -20), (30, 20), (-30, 20)],
        holes=[[(-5, -5), (5, -5), (5, 5), (-5, 5)]],
    )
    path = _write_base(tmp_path / "holed.stl", polygon)
    route = _route(simple_gpx)
    custom = prepare_custom_base(path, 5.0, route, 1.0)
    config = Config(
        gpx_file=simple_gpx,
        output=tmp_path / "unused.3mf",
        topo=False,
        base_stl=path,
    )
    geometry = build_geometry(
        route,
        custom.transform.footprint,
        custom.transform,
        config,
        None,
        custom,
    )
    assert geometry.base.is_watertight
    assert geometry.topography is None
    assert len(custom.terrain_polygon.interiors) == 1


@pytest.mark.parametrize(
    "polygon",
    [
        Polygon(
            [
                (-30.25, -20.75),
                (30.4, -20.75),
                (30.4, 20.3),
                (3.2, 12.6),
                (-30.25, 20.3),
            ]
        ),
        Point(0.35, -0.2).buffer(28.75, quad_segs=6),
    ],
)
def test_custom_terrain_handles_fractional_boundaries(
    simple_gpx: Path, tmp_path: Path, polygon: Polygon
) -> None:
    base = _write_base(tmp_path / "fractional.stl", polygon)
    route = _route(simple_gpx)
    custom = prepare_custom_base(base, 5.0, route, 1.0)
    config = Config(
        gpx_file=simple_gpx,
        output=tmp_path / "unused.3mf",
        topo=False,
        base_stl=base,
    )
    geometry = build_geometry(
        route,
        custom.transform.footprint,
        custom.transform,
        config,
        None,
        custom,
    )
    assert geometry.base.is_watertight
    assert geometry.topography is None


def test_custom_base_rejects_disconnected_highest_regions(
    simple_gpx: Path, tmp_path: Path
) -> None:
    left = trimesh.creation.box(extents=(20.0, 20.0, 5.0))
    right = left.copy()
    right.apply_translation((30.0, 0.0, 0.0))
    path = tmp_path / "disconnected.stl"
    trimesh.util.concatenate((left, right)).export(path, file_type="stl")
    with pytest.raises(Gpx2StlError, match="exactly one connected"):
        prepare_custom_base(path, 5.0, _route(simple_gpx), 1.0)


def test_custom_base_rejects_nonhorizontal_highest_surface(
    simple_gpx: Path, tmp_path: Path
) -> None:
    mesh = trimesh.creation.box(extents=(40.0, 30.0, 5.0))
    mesh.apply_transform(
        trimesh.transformations.rotation_matrix(
            np.deg2rad(10.0),
            [1.0, 0.0, 0.0],
        )
    )
    path = tmp_path / "sloped.stl"
    mesh.export(path, file_type="stl")
    with pytest.raises(Gpx2StlError, match="no horizontal upward-facing"):
        prepare_custom_base(path, 5.0, _route(simple_gpx), 1.0)


def _custom_geometry(
    simple_gpx: Path,
    tmp_path: Path,
    *,
    text: str | None,
    use_3mf: bool,
    topo: bool = False,
    text_mode: str = "raised",
    text_depth: float = 0.6,
):
    base = _write_base(
        tmp_path / "base.stl",
        Polygon([(-30, -20), (30, -20), (30, 20), (-30, 20)]),
    )
    route = _route(simple_gpx)
    custom = prepare_custom_base(base, 15.0, route, 1.0)
    output = tmp_path / ("custom.3mf" if use_3mf else "custom.stl")
    config = Config(
        gpx_file=simple_gpx,
        output=output,
        topo=topo,
        text=text,
        text_mode=text_mode,
        text_depth=text_depth,
        base_stl=base,
        use_3mf=use_3mf,
    )
    geometry = build_geometry(
        route,
        custom.transform.footprint,
        custom.transform,
        config,
        SlopedDem() if topo else None,
        custom,
    )
    return geometry, config


def test_custom_base_geometry_is_watertight_and_preserves_bounds(
    simple_gpx: Path, tmp_path: Path
) -> None:
    geometry, _ = _custom_geometry(
        simple_gpx,
        tmp_path,
        text=None,
        use_3mf=True,
    )
    assert geometry.base.is_watertight
    assert geometry.route.is_watertight
    assert geometry.topography is None
    assert np.allclose(geometry.base.bounds[0, :2], [-30.0, -20.0])
    assert np.allclose(geometry.base.bounds[1, :2], [30.0, 20.0])
    assert np.min(geometry.route.vertices[:, 2]) >= 4.9


def test_custom_base_preserves_negative_z_placement(
    simple_gpx: Path, tmp_path: Path
) -> None:
    polygon = Polygon([(-30, -20), (30, -20), (30, 20), (-30, 20)])
    mesh = trimesh.creation.extrude_polygon(polygon, 5.0)
    mesh.apply_translation((0.0, 0.0, -10.0))
    base = tmp_path / "negative-z.stl"
    mesh.export(base, file_type="stl")
    route = _route(simple_gpx)
    custom = prepare_custom_base(base, 10.0, route, 1.0)
    config = Config(
        gpx_file=simple_gpx,
        output=tmp_path / "unused.3mf",
        topo=False,
        base_stl=base,
    )
    geometry = build_geometry(
        route,
        custom.transform.footprint,
        custom.transform,
        config,
        None,
        custom,
    )
    assert custom.top_z == -5.0
    assert np.min(geometry.route.vertices[:, 2]) < -5.0
    assert geometry.route.is_watertight


def test_custom_base_adapts_overlap_for_thin_mesh(
    simple_gpx: Path, tmp_path: Path
) -> None:
    polygon = Polygon([(-30, -20), (30, -20), (30, 20), (-30, 20)])
    base = _write_base(tmp_path / "thin.stl", polygon, height=0.02)
    route = _route(simple_gpx)
    custom = prepare_custom_base(base, 10.0, route, 1.0)
    config = Config(
        gpx_file=simple_gpx,
        output=tmp_path / "unused.3mf",
        topo=False,
        text="TRAIL",
        base_stl=base,
    )
    geometry = build_geometry(
        route,
        custom.transform.footprint,
        custom.transform,
        config,
        None,
        custom,
    )
    assert custom.overlap_depth == pytest.approx(0.01)
    assert np.min(geometry.route.vertices[:, 2]) == pytest.approx(0.01)
    assert geometry.text is not None
    assert np.min(geometry.text.vertices[:, 2]) == pytest.approx(0.01)


def test_custom_base_supports_dem_relief(
    simple_gpx: Path, tmp_path: Path
) -> None:
    base = _write_base(
        tmp_path / "base.stl",
        Polygon([(-30, -20), (30, -20), (30, 20), (-30, 20)]),
    )
    route = _route(simple_gpx)
    custom = prepare_custom_base(base, 10.0, route, 1.0)
    config = Config(
        gpx_file=simple_gpx,
        output=tmp_path / "unused.3mf",
        topo=True,
        base_stl=base,
    )
    geometry = build_geometry(
        route,
        custom.transform.footprint,
        custom.transform,
        config,
        SlopedDem(),
        custom,
    )
    assert geometry.base.is_watertight
    assert geometry.topography is not None
    assert geometry.topography.is_watertight
    assert np.max(geometry.topography.vertices[:, 2]) > custom.top_z
    assert np.min(geometry.topography.vertices[:, 2]) < custom.top_z


def test_custom_base_text_uses_separate_base_object(
    simple_gpx: Path, tmp_path: Path
) -> None:
    geometry, config = _custom_geometry(
        simple_gpx,
        tmp_path,
        text="TRAIL",
        use_3mf=True,
    )
    assert geometry.text is not None
    assert geometry.text.is_watertight
    export_geometry(geometry, config)
    wrapper = lib3mf.get_wrapper()
    model = wrapper.CreateModel()
    model.QueryReader("3mf").ReadFromFile(str(config.output))
    assert model.GetMeshObjects().Count() == 3
    meshes = model.GetMeshObjects()
    names = set()
    while meshes.MoveNext():
        names.add(meshes.GetCurrentMeshObject().GetName())
    assert names == {"Base", "GPX route", "Text"}


def test_custom_base_topography_and_text_export_as_four_objects(
    simple_gpx: Path, tmp_path: Path
) -> None:
    geometry, config = _custom_geometry(
        simple_gpx,
        tmp_path,
        text="TRAIL",
        use_3mf=True,
        topo=True,
    )
    export_geometry(geometry, config)
    wrapper = lib3mf.get_wrapper()
    model = wrapper.CreateModel()
    model.QueryReader("3mf").ReadFromFile(str(config.output))
    meshes = model.GetMeshObjects()
    names = set()
    material_ids = {}
    while meshes.MoveNext():
        mesh = meshes.GetCurrentMeshObject()
        names.add(mesh.GetName())
        _, material_id, has_property = mesh.GetObjectLevelProperty()
        assert has_property
        material_ids[mesh.GetName()] = material_id
    assert names == {"Base", "GPX route", "Text", "Topography"}
    assert material_ids == {
        "Base": 4,
        "GPX route": 1,
        "Text": 3,
        "Topography": 2,
    }


def test_custom_base_text_uses_compact_run_inside_flat_border(
    simple_gpx: Path, tmp_path: Path
) -> None:
    base = _write_base(
        tmp_path / "base.stl",
        Polygon([(-30, -20), (30, -20), (30, 20), (-30, 20)]),
    )
    route = _route(simple_gpx)
    custom = prepare_custom_base(base, 15.0, route, 1.0)
    config = Config(
        gpx_file=simple_gpx,
        output=tmp_path / "unused.3mf",
        topo=False,
        text="TRAIL",
        base_stl=base,
    )
    polygons = _custom_text_polygons(config, custom)
    bounds = np.asarray(
        [
            min(polygon.bounds[0] for polygon in polygons),
            min(polygon.bounds[1] for polygon in polygons),
            max(polygon.bounds[2] for polygon in polygons),
            max(polygon.bounds[3] for polygon in polygons),
        ]
    )
    border = custom.top_polygon.difference(custom.terrain_polygon).buffer(1e-7)
    assert bounds[2] > bounds[0]
    assert bounds[3] > bounds[1]
    assert all(border.covers(polygon) for polygon in polygons)


def test_custom_base_embedded_text_is_flush_and_cuts_cavity(
    simple_gpx: Path, tmp_path: Path
) -> None:
    geometry, config = _custom_geometry(
        simple_gpx,
        tmp_path,
        text="TRAIL",
        use_3mf=True,
        text_mode="embedded",
        text_depth=0.8,
    )
    assert geometry.text is not None
    assert geometry.base.is_watertight
    assert geometry.text.is_watertight
    original = trimesh.load_mesh(config.base_stl, process=True)
    assert isinstance(original, trimesh.Trimesh)
    assert geometry.text.bounds[1, 2] == pytest.approx(original.bounds[1, 2])
    assert np.ptp(geometry.text.bounds[:, 2]) == pytest.approx(config.text_depth)
    assert geometry.base.volume < original.volume
    assert geometry.base.volume + geometry.text.volume == pytest.approx(
        original.volume,
        rel=1e-6,
    )


def test_custom_base_text_seam_is_bottom_centered(
    simple_gpx: Path, tmp_path: Path
) -> None:
    base = _write_base(
        tmp_path / "base.stl",
        Polygon([(-30, -20), (30, -20), (30, 20), (-30, 20)]),
    )
    route = _route(simple_gpx)
    custom = prepare_custom_base(base, 15.0, route, 1.0)
    config = Config(
        gpx_file=simple_gpx,
        output=tmp_path / "unused.3mf",
        topo=False,
        text="TRAIL",
        base_stl=base,
    )
    layout = _custom_text_layout(config, custom)
    assert layout.seam_point[0] == pytest.approx(0.0, abs=1e-6)
    assert layout.seam_point[1] < 0.0
    assert layout.seam_gap > 0.0


def test_custom_base_text_margin_controls_border_clearance(
    simple_gpx: Path, tmp_path: Path
) -> None:
    base = _write_base(
        tmp_path / "base.stl",
        Polygon([(-30, -20), (30, -20), (30, 20), (-30, 20)]),
    )
    route = _route(simple_gpx)
    custom = prepare_custom_base(base, 15.0, route, 1.0)
    config = Config(
        gpx_file=simple_gpx,
        output=tmp_path / "unused.3mf",
        topo=False,
        text="TRAIL",
        text_margin=1.0,
        base_stl=base,
    )
    polygons = _custom_text_polygons(config, custom)
    text_area = custom.top_polygon.difference(custom.terrain_polygon).buffer(
        -config.text_margin,
        join_style="mitre",
    )
    assert all(text_area.buffer(1e-7).covers(polygon) for polygon in polygons)


def test_custom_base_stl_round_trip_is_watertight(
    simple_gpx: Path, tmp_path: Path
) -> None:
    geometry, config = _custom_geometry(
        simple_gpx,
        tmp_path,
        text="TRAIL",
        use_3mf=False,
    )
    export_geometry(geometry, config)
    loaded = trimesh.load_mesh(config.output, process=True)
    assert isinstance(loaded, trimesh.Trimesh)
    assert loaded.is_watertight
