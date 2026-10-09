from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import lib3mf
import numpy as np
import pytest
import trimesh
from matplotlib.textpath import TextToPath
from matplotlib.textpath import TextPath
from shapely.geometry import Polygon

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
        route_boundary_percent=10.0,
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
        route_boundary_percent=10.0,
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


def test_city_geometry_has_flush_route_cavity_and_complete_building(
    simple_gpx: Path, tmp_path: Path
) -> None:
    route = project_paths(read_gpx(simple_gpx))
    footprint = create_footprint(route.points, "square", 10.0, 1.0)
    transform = create_model_transform(footprint, 20.0)
    center = route.points[len(route.points) // 2]
    half_size = footprint.radius * 0.05
    polygon = Polygon(
        [
            center + (-half_size, -half_size),
            center + (half_size, -half_size),
            center + (half_size, half_size),
            center + (-half_size, half_size),
        ]
    )
    water_polygon = Polygon(
        [
            footprint.center + (-half_size * 3, half_size * 2),
            footprint.center + (-half_size, half_size * 2),
            footprint.center + (-half_size, half_size * 4),
            footprint.center + (-half_size * 3, half_size * 4),
        ]
    )
    config = Config(
        gpx_file=simple_gpx,
        output=tmp_path / "city.3mf",
        mode="city",
        topo=True,
        shape="square",
        route_boundary_percent=10.0,
        max_size=20.0,
        route_width=1.0,
        route_depth=0.6,
    )

    geometry = build_geometry(
        route,
        footprint,
        transform,
        config,
        SlopedDem(),
        buildings=(SimpleNamespace(polygon=polygon, height_m=10.0),),
        water=(water_polygon,),
    )

    assert geometry.base.is_watertight
    assert geometry.topography is not None and geometry.topography.is_watertight
    assert geometry.route.is_watertight
    assert geometry.buildings is not None and geometry.buildings.is_watertight
    assert geometry.water is not None and geometry.water.is_watertight
    expected_bounds = Polygon(transform.to_model(np.asarray(polygon.exterior.coords))).bounds
    actual_bounds = (
        geometry.buildings.bounds[0, 0],
        geometry.buildings.bounds[0, 1],
        geometry.buildings.bounds[1, 0],
        geometry.buildings.bounds[1, 1],
    )
    assert actual_bounds == pytest.approx(expected_bounds)

    export_geometry(geometry, config)
    wrapper = lib3mf.get_wrapper()
    model = wrapper.CreateModel()
    model.QueryReader("3mf").ReadFromFile(str(config.output))
    assert _mesh_names(model) == {
        "Base",
        "Buildings",
        "GPX route",
        "Topography",
        "Water",
    }
    assert _mesh_material_ids(model)["Buildings"] == 5
    assert _mesh_material_ids(model)["Water"] == 6


def test_hex_geometry_is_watertight(simple_gpx: Path, tmp_path: Path) -> None:
    route = project_paths(read_gpx(simple_gpx))
    footprint = create_footprint(route.points, "hex", 10.0, 1.0)
    transform = create_model_transform(footprint, 20.0)
    config = Config(
        gpx_file=simple_gpx,
        output=tmp_path / "unused.3mf",
        topo=True,
        shape="hex",
        route_boundary_percent=10.0,
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
    text_align: str = "center",
    text_mode: str = "raised",
    text_depth: float = 0.6,
    font_size: float | None = None,
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
        route_boundary_percent=10.0,
        text=text,
        text_height=1.0,
        text_margin=text_margin,
        text_end_gap=text_end_gap,
        text_align=text_align,
        text_mode=text_mode,
        text_depth=text_depth,
        text_boundary_percent=15.0,
        font_size=font_size,
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
def test_generated_text_uses_compact_run_with_bottom_seam(
    simple_gpx: Path, tmp_path: Path, shape: Shape
) -> None:
    _, config = _text_geometry(
        simple_gpx,
        tmp_path / "unused.3mf",
        True,
        shape=shape,
    )
    layout = _generated_text_layout(config, 7.0)
    gaps = np.diff(np.asarray(layout.glyph_distances))
    assert np.max(gaps) < layout.perimeter / 4.0
    assert layout.seam_point[0] == pytest.approx(0.0, abs=1e-6)
    assert layout.seam_point[1] < 0.0


def test_text_alignment_positions_compact_run_relative_to_seam(
    simple_gpx: Path, tmp_path: Path
) -> None:
    _, config = _text_geometry(
        simple_gpx,
        tmp_path / "unused.3mf",
        True,
        shape="circle",
    )
    first_offsets = []
    for alignment in ("left", "center", "right"):
        layout = _generated_text_layout(replace(config, text_align=alignment), 7.0)
        first_offsets.append(
            (layout.glyph_distances[0] - layout.seam_distance) % layout.perimeter
        )
    assert first_offsets[0] < first_offsets[1] < first_offsets[2]


def test_explicit_font_size_sets_glyph_height(
    simple_gpx: Path, tmp_path: Path
) -> None:
    _, config = _text_geometry(
        simple_gpx,
        tmp_path / "unused.3mf",
        True,
        shape="circle",
        font_size=1.25,
    )
    layout = _generated_text_layout(config, 7.0)
    properties, _ = _font_properties(config)
    unit_height = TextPath(
        (0.0, 0.0),
        config.text.strip(),
        size=1.0,
        prop=properties,
    ).get_extents().height
    assert layout.font_size * unit_height == pytest.approx(1.25)


def test_installed_bold_italic_font_variant_resolves(
    simple_gpx: Path, tmp_path: Path
) -> None:
    _, config = _text_geometry(simple_gpx, tmp_path / "unused.3mf", True)
    properties, path = _font_properties(
        replace(config, font_weight="bold", font_style="italic")
    )
    assert path.is_file()
    assert properties.get_file() == str(path)


def test_missing_font_family_is_reported(
    simple_gpx: Path, tmp_path: Path
) -> None:
    _, config = _text_geometry(simple_gpx, tmp_path / "unused.3mf", True)
    with pytest.raises(Gpx2StlError, match="Unable to find installed font family"):
        _font_properties(
            replace(config, font_family="Definitely Missing Font Family 12345")
        )


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
    inner_radius = config.max_size * (1.0 - 2.0 * config.text_boundary_percent / 100.0) / 2.0
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


def test_embedded_text_is_flush_and_replaces_base_volume(
    simple_gpx: Path, tmp_path: Path
) -> None:
    geometry, config = _text_geometry(
        simple_gpx,
        tmp_path / "embedded.3mf",
        True,
        shape="circle",
        text_mode="embedded",
        text_depth=0.6,
    )
    assert geometry.text is not None
    assert geometry.base.is_watertight
    assert geometry.text.is_watertight
    assert geometry.text.bounds[1, 2] == pytest.approx(config.base_height)
    assert geometry.text.bounds[0, 2] == pytest.approx(
        config.base_height - config.text_depth
    )
    raised_geometry, _ = _text_geometry(
        simple_gpx,
        tmp_path / "raised.3mf",
        True,
        shape="circle",
    )
    full_base_volume = raised_geometry.base.volume
    assert geometry.base.volume < full_base_volume
    assert geometry.base.volume + geometry.text.volume == pytest.approx(
        full_base_volume,
        rel=1e-5,
    )
    export_geometry(geometry, config)
    wrapper = lib3mf.get_wrapper()
    model = wrapper.CreateModel()
    model.QueryReader("3mf").ReadFromFile(str(config.output))
    assert _mesh_names(model) == {"Base", "GPX route", "Text"}


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
        route_boundary_percent=10.0,
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
