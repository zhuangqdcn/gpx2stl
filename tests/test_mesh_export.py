from __future__ import annotations

from pathlib import Path

import lib3mf
import numpy as np
import pytest
import trimesh
from matplotlib.textpath import TextToPath

from gpx2stl.errors import Gpx2StlError
from gpx2stl.export import export_geometry
from gpx2stl.footprint import (
    add_route_clearance,
    create_footprint,
    create_model_transform,
)
from gpx2stl.gpx import interpolate_elevations, project_paths, read_gpx
from gpx2stl.mesh import _font_properties, _generated_text_layout, build_geometry
from gpx2stl.models import Config, Shape


class SlopedDem:
    def sample_projected(self, points, route):
        return points[:, 0] * 0.01 + points[:, 1] * 0.02


def _geometry(simple_gpx: Path, output: Path, use_3mf: bool):
    paths = tuple(interpolate_elevations(path) for path in read_gpx(simple_gpx))
    route = project_paths(paths)
    footprint = create_footprint(route.points, "square", 10.0, 1.0)
    transform = create_model_transform(footprint, 20.0)
    config = Config(
        gpx_file=simple_gpx,
        output=output,
        topo=False,
        use_3mf=use_3mf,
        max_size=20.0,
        route_width=1.0,
    )
    return build_geometry(route, footprint, transform, config, None), config


def test_no_topo_geometry_is_watertight(simple_gpx: Path, tmp_path: Path) -> None:
    geometry, _ = _geometry(simple_gpx, tmp_path / "unused.3mf", True)
    assert geometry.base.is_watertight
    assert geometry.route.is_watertight
    assert geometry.topography is None
    assert np.isclose(np.ptp(geometry.base.vertices[:, 0]), 20.0)


def test_topo_circle_geometry_is_watertight(simple_gpx: Path, tmp_path: Path) -> None:
    route = project_paths(read_gpx(simple_gpx))
    footprint = create_footprint(route.points, "circle", 10.0, 1.0)
    transform = create_model_transform(footprint, 20.0)
    config = Config(
        gpx_file=simple_gpx,
        output=tmp_path / "unused.3mf",
        topo=True,
        shape="circle",
        max_size=20.0,
    )
    geometry = build_geometry(route, footprint, transform, config, SlopedDem())
    assert geometry.base.is_watertight
    assert geometry.topography is not None
    assert geometry.topography.is_watertight
    assert geometry.route.is_watertight
    assert np.isclose(np.ptp(geometry.base.vertices[:, 0]), 20.0)
    assert np.isclose(np.ptp(geometry.topography.vertices[:, 0]), 20.0)
    assert np.min(geometry.topography.vertices[:, 2]) < config.base_height
    top_vertices = geometry.topography.vertices[
        geometry.topography.vertices[:, 2] >= config.base_height
    ]
    projected = transform.to_projected(top_vertices[:, :2])
    raw_relief = np.ptp(SlopedDem().sample_projected(projected, route))
    model_relief = np.ptp(top_vertices[:, 2])
    assert np.isclose(model_relief, raw_relief * transform.scale)


def test_hex_geometry_is_watertight(simple_gpx: Path, tmp_path: Path) -> None:
    route = project_paths(read_gpx(simple_gpx))
    footprint = create_footprint(route.points, "hex", 10.0, 1.0)
    transform = create_model_transform(footprint, 20.0)
    config = Config(
        gpx_file=simple_gpx,
        output=tmp_path / "unused.3mf",
        topo=True,
        shape="hex",
        max_size=20.0,
    )
    geometry = build_geometry(route, footprint, transform, config, SlopedDem())
    assert geometry.base.is_watertight
    assert geometry.topography is not None
    assert geometry.topography.is_watertight
    assert geometry.route.is_watertight
    assert np.isclose(np.ptp(geometry.topography.vertices[:, 0]), 20.0)


def _text_geometry(
    simple_gpx: Path,
    output: Path,
    use_3mf: bool,
    shape: Shape = "hex",
    text: str = "TRAIL O",
    text_margin: float | None = None,
    text_end_gap: float = 0.0,
):
    paths = tuple(interpolate_elevations(path) for path in read_gpx(simple_gpx))
    route = project_paths(paths)
    terrain_size = 14.0
    footprint = create_footprint(route.points, "circle", 10.0, 1.0)
    footprint = add_route_clearance(footprint, 1.0, terrain_size)
    transform = create_model_transform(footprint, terrain_size)
    config = Config(
        gpx_file=simple_gpx,
        output=output,
        topo=False,
        shape=shape,
        text=text,
        text_height=1.0,
        text_margin=text_margin,
        text_end_gap=text_end_gap,
        inner_size_percent=70.0,
        use_3mf=use_3mf,
        max_size=20.0,
        route_width=1.0,
    )
    return build_geometry(route, footprint, transform, config, None), config


@pytest.mark.parametrize("shape", ["square", "circle", "hex"])
def test_text_layout_has_watertight_frame_route_and_text(
    simple_gpx: Path, tmp_path: Path, shape: Shape
) -> None:
    geometry, config = _text_geometry(
        simple_gpx,
        tmp_path / "unused.3mf",
        True,
        shape,
    )
    assert geometry.base.is_watertight
    assert geometry.route.is_watertight
    assert geometry.topography is None
    assert geometry.text is not None
    assert geometry.text.is_watertight
    assert np.isclose(np.ptp(geometry.base.vertices[:, 0]), config.max_size)
    assert np.max(geometry.text.vertices[:, 2]) > config.base_height
    assert np.min(geometry.text.vertices[:, 2]) < config.base_height


@pytest.mark.parametrize("shape", ["square", "circle", "hex"])
def test_generated_text_wraps_full_perimeter_with_bottom_seam(
    simple_gpx: Path, tmp_path: Path, shape: Shape
) -> None:
    _, config = _text_geometry(
        simple_gpx,
        tmp_path / "unused.3mf",
        True,
        shape=shape,
    )
    layout = _generated_text_layout(config, 7.0)
    bounds = np.asarray(
        [
            min(polygon.bounds[0] for polygon in layout.polygons),
            min(polygon.bounds[1] for polygon in layout.polygons),
            max(polygon.bounds[2] for polygon in layout.polygons),
            max(polygon.bounds[3] for polygon in layout.polygons),
        ]
    )
    assert np.ptp(bounds[[0, 2]]) > config.max_size * 0.6
    assert np.ptp(bounds[[1, 3]]) > config.max_size * 0.6
    assert layout.seam_point[0] == pytest.approx(0.0, abs=1e-6)
    assert layout.seam_point[1] < 0.0


def test_text_seam_combines_default_spaces_edge_spaces_and_millimeters(
    simple_gpx: Path, tmp_path: Path
) -> None:
    _, config = _text_geometry(
        simple_gpx,
        tmp_path / "unused.3mf",
        True,
        shape="circle",
        text="  TRAIL   ",
        text_end_gap=2.0,
    )
    layout = _generated_text_layout(config, 7.0)
    properties, _ = _font_properties(config)
    space_advance = TextToPath().get_text_width_height_descent(
        " ", properties, ismath=False
    )[0]
    expected = 13.0 * space_advance * layout.font_size + 2.0
    assert layout.seam_gap == pytest.approx(expected)


def test_text_end_gap_reports_when_perimeter_is_too_short(
    simple_gpx: Path, tmp_path: Path
) -> None:
    with pytest.raises(Gpx2StlError, match="--text-end-gap"):
        _text_geometry(
            simple_gpx,
            tmp_path / "unused.3mf",
            True,
            shape="circle",
            text_end_gap=100.0,
        )


def test_text_margin_controls_generated_frame_clearance(
    simple_gpx: Path, tmp_path: Path
) -> None:
    geometry, config = _text_geometry(
        simple_gpx,
        tmp_path / "unused.3mf",
        True,
        shape="circle",
        text_margin=1.0,
    )
    assert geometry.text is not None
    radii = np.linalg.norm(geometry.text.vertices[:, :2], axis=1)
    inner_radius = config.max_size * config.inner_size_percent / 200.0
    assert np.min(radii) >= inner_radius + config.text_margin - 1e-7
    assert np.max(radii) <= config.max_size / 2.0 - config.text_margin + 1e-7


def test_text_margin_reports_when_frame_is_too_narrow(
    simple_gpx: Path, tmp_path: Path
) -> None:
    with pytest.raises(Gpx2StlError, match="--text-margin"):
        _text_geometry(
            simple_gpx,
            tmp_path / "unused.3mf",
            True,
            shape="circle",
            text_margin=1.5,
        )


def test_missing_default_font_glyph_is_reported(
    simple_gpx: Path, tmp_path: Path
) -> None:
    with pytest.raises(Gpx2StlError, match="does not contain glyphs"):
        _text_geometry(
            simple_gpx,
            tmp_path / "unused.3mf",
            True,
            text="路线",
        )


def test_explicit_terrain_height_overrides_automatic_scale(
    simple_gpx: Path, tmp_path: Path
) -> None:
    route = project_paths(read_gpx(simple_gpx))
    footprint = create_footprint(route.points, "square", 10.0, 1.0)
    transform = create_model_transform(footprint, 20.0)
    config = Config(
        gpx_file=simple_gpx,
        output=tmp_path / "unused.3mf",
        topo=True,
        max_size=20.0,
        terrain_height=30.0,
    )
    geometry = build_geometry(route, footprint, transform, config, SlopedDem())
    assert geometry.topography is not None
    top_vertices = geometry.topography.vertices[
        geometry.topography.vertices[:, 2] >= config.base_height
    ]
    assert np.isclose(np.ptp(top_vertices[:, 2]), 30.0)


def _mesh_names(model: object) -> set[str]:
    names = set()
    meshes = model.GetMeshObjects()
    while meshes.MoveNext():
        names.add(meshes.GetCurrentMeshObject().GetName())
    return names


def _mesh_material_ids(model: object) -> dict[str, int]:
    material_ids = {}
    meshes = model.GetMeshObjects()
    while meshes.MoveNext():
        mesh = meshes.GetCurrentMeshObject()
        _, material_id, has_property = mesh.GetObjectLevelProperty()
        assert has_property
        material_ids[mesh.GetName()] = material_id
    return material_ids


def test_3mf_round_trip_has_two_meshes_and_four_material_slots(
    simple_gpx: Path, tmp_path: Path
) -> None:
    output = tmp_path / "route.3mf"
    geometry, config = _geometry(simple_gpx, output, True)
    export_geometry(geometry, config)
    wrapper = lib3mf.get_wrapper()
    model = wrapper.CreateModel()
    model.QueryReader("3mf").ReadFromFile(str(output))
    assert model.GetMeshObjects().Count() == 2
    assert _mesh_names(model) == {"Base", "GPX route"}
    assert _mesh_material_ids(model) == {"Base": 4, "GPX route": 1}
    groups = model.GetBaseMaterialGroups()
    assert groups.MoveNext()
    group = groups.GetCurrentBaseMaterialGroup()
    assert group.GetAllPropertyIDs() == [1, 2, 3, 4]


def test_3mf_with_text_has_three_meshes_and_four_material_slots(
    simple_gpx: Path, tmp_path: Path
) -> None:
    output = tmp_path / "route-text.3mf"
    geometry, config = _text_geometry(simple_gpx, output, True)
    export_geometry(geometry, config)
    wrapper = lib3mf.get_wrapper()
    model = wrapper.CreateModel()
    model.QueryReader("3mf").ReadFromFile(str(output))
    assert model.GetMeshObjects().Count() == 3
    assert _mesh_names(model) == {"Base", "GPX route", "Text"}
    assert _mesh_material_ids(model) == {"Base": 4, "GPX route": 1, "Text": 3}
    groups = model.GetBaseMaterialGroups()
    assert groups.MoveNext()
    group = groups.GetCurrentBaseMaterialGroup()
    assert group.GetAllPropertyIDs() == [1, 2, 3, 4]


def test_3mf_with_topography_and_text_has_four_named_objects(
    simple_gpx: Path, tmp_path: Path
) -> None:
    output = tmp_path / "route-topo-text.3mf"
    geometry, config = _text_geometry(simple_gpx, output, True)
    config = Config(**{**config.__dict__, "topo": True})
    paths = read_gpx(simple_gpx)
    route = project_paths(paths)
    footprint = create_footprint(route.points, "circle", 10.0, 1.0)
    footprint = add_route_clearance(footprint, 1.0, 14.0)
    transform = create_model_transform(footprint, 14.0)
    geometry = build_geometry(route, footprint, transform, config, SlopedDem())

    export_geometry(geometry, config)

    wrapper = lib3mf.get_wrapper()
    model = wrapper.CreateModel()
    model.QueryReader("3mf").ReadFromFile(str(output))
    assert model.GetMeshObjects().Count() == 4
    assert _mesh_names(model) == {"Base", "GPX route", "Text", "Topography"}
    assert _mesh_material_ids(model) == {
        "Base": 4,
        "GPX route": 1,
        "Text": 3,
        "Topography": 2,
    }
    groups = model.GetBaseMaterialGroups()
    assert groups.MoveNext()
    group = groups.GetCurrentBaseMaterialGroup()
    assert group.GetAllPropertyIDs() == [1, 2, 3, 4]
    assert [group.GetName(index) for index in range(1, 5)] == [
        "Filament 1 - Route",
        "Filament 2 - Topography",
        "Filament 3 - Text",
        "Filament 4 - Base",
    ]


def test_stl_round_trip_is_watertight(simple_gpx: Path, tmp_path: Path) -> None:
    output = tmp_path / "route.stl"
    geometry, config = _geometry(simple_gpx, output, False)
    export_geometry(geometry, config)
    loaded = trimesh.load_mesh(output, process=True)
    assert isinstance(loaded, trimesh.Trimesh)
    assert loaded.is_watertight


def test_stl_with_text_is_watertight(simple_gpx: Path, tmp_path: Path) -> None:
    output = tmp_path / "route-text.stl"
    geometry, config = _text_geometry(simple_gpx, output, False)
    export_geometry(geometry, config)
    loaded = trimesh.load_mesh(output, process=True)
    assert isinstance(loaded, trimesh.Trimesh)
    assert loaded.is_watertight


def test_stl_unions_separate_base_topography_route_and_text(
    simple_gpx: Path, tmp_path: Path
) -> None:
    output = tmp_path / "route-topo-text.stl"
    _, original_config = _text_geometry(simple_gpx, output, False)
    config = Config(**{**original_config.__dict__, "topo": True})
    route = project_paths(read_gpx(simple_gpx))
    footprint = create_footprint(route.points, "circle", 10.0, 1.0)
    footprint = add_route_clearance(footprint, 1.0, 14.0)
    transform = create_model_transform(footprint, 14.0)
    geometry = build_geometry(route, footprint, transform, config, SlopedDem())

    export_geometry(geometry, config)

    loaded = trimesh.load_mesh(output, process=True)
    assert isinstance(loaded, trimesh.Trimesh)
    assert loaded.is_watertight
