from __future__ import annotations

from dataclasses import replace
from itertools import product
from pathlib import Path
from types import SimpleNamespace
import xml.etree.ElementTree as ET
from zipfile import ZipFile

import lib3mf
import numpy as np
import pytest
import shapely
import trimesh
from matplotlib.textpath import TextToPath
from matplotlib.textpath import TextPath
from shapely.geometry import LineString, MultiPoint, Polygon, box
from shapely.ops import polygonize

from gpx2stl.errors import Gpx2StlError
from gpx2stl.city import CityRoad
from gpx2stl.export import export_geometry
from gpx2stl.footprint import (
    add_route_clearance,
    create_footprint,
    create_model_transform,
)
from gpx2stl.gpx import interpolate_elevations, project_paths, read_gpx
from gpx2stl.mesh import (
    Geometry,
    _building_mesh,
    _compensate_structure_footprint,
    _font_properties,
    _generated_text_layout,
    _model_outline,
    _resolve_structure_overlaps,
    _road_mesh,
    _structure_printable_clip,
    _variable_extrusion,
    build_geometry,
)
from gpx2stl.models import Config, Footprint, ModelTransform, Shape


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


def _assert_bambu_settings(output: Path, expected: dict[str, int]) -> None:
    with ZipFile(output) as archive:
        model = ET.fromstring(archive.read("3D/3dmodel.model"))
        settings = ET.fromstring(archive.read("Metadata/model_settings.config"))
        content_types = ET.fromstring(archive.read("[Content_Types].xml"))
        relationships = ET.fromstring(archive.read("3D/_rels/3dmodel.model.rels"))
        assert len(archive.namelist()) == len(set(archive.namelist()))
        assert not any("project_settings" in name for name in archive.namelist())
    assert any(
        entry.get("Extension") == "config"
        and entry.get("ContentType") == "application/xml"
        for entry in content_types
    )
    assert any(
        entry.get("Target") == "/Metadata/model_settings.config"
        for entry in relationships
    )
    objects = {obj.get("id"): obj for obj in model.findall(".//{*}object")}
    build = model.find("{*}build")
    assert build is not None and len(build) == 1
    assembly_id = build[0].get("objectid")
    assembly = objects[assembly_id]
    components = assembly.find("{*}components")
    assert components is not None
    assert settings.tag == "config" and len(settings) == 1
    obj = settings[0]
    assert obj.tag == "object" and obj.get("id") == assembly_id
    assert obj.find("metadata").attrib == {
        "key": "name", "value": output.stem,
    }
    parts = obj.findall("part")
    assert [part.get("id") for part in parts] == [
        component.get("objectid") for component in components
    ]
    assignments = {}
    for part in parts:
        assert part.get("subtype") == "normal_part"
        mesh = objects[part.get("id")]
        assert mesh.find("{*}mesh") is not None
        metadata = {entry.get("key"): entry.get("value") for entry in part}
        assert metadata["name"] == mesh.get("name")
        filament = int(metadata["extruder"])
        assignments[metadata["name"]] = filament
        materials = model.find(f".//{{*}}basematerials[@id='{mesh.get('pid')}']")
        assert materials is not None
        assert materials[int(mesh.get("pindex"))].get("name").startswith(
            f"Filament {filament} - "
        )
    assert assignments == expected


@pytest.mark.parametrize(
    "optional_parts", list(product((False, True), repeat=5))
)
def test_bambu_metadata_names_and_filaments_for_optional_parts(
    tmp_path: Path, optional_parts: tuple[bool, ...],
) -> None:
    topography, text, buildings, water, roads = optional_parts
    mesh = trimesh.creation.box()
    geometry = Geometry(
        base=mesh, route=mesh,
        topography=mesh if topography else None,
        text=mesh if text else None,
        buildings=mesh if buildings else None,
        water=mesh if water else None,
        roads=mesh if roads else None,
    )
    output = tmp_path / "Morning & Evening 'Ride'.3mf"
    config = Config(gpx_file=tmp_path / "unused.gpx", output=output)
    export_geometry(geometry, config)
    expected = {"GPX route": 1, "Base": 4}
    for present, name, filament in zip(
        optional_parts,
        ("Topography", "Text", "Buildings", "Water", "Roads"),
        (2, 3, 5, 6, 7),
    ):
        if present:
            expected[name] = filament
    _assert_bambu_settings(output, expected)


@pytest.mark.parametrize("failure", ["missing", "corrupted", "write-error"])
def test_bambu_metadata_failure_preserves_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    output = tmp_path / "existing.3mf"
    output.write_bytes(b"original destination")
    mesh = trimesh.creation.box()
    geometry = Geometry(base=mesh, route=mesh)
    config = Config(gpx_file=tmp_path / "unused.gpx", output=output)
    if failure == "missing":
        monkeypatch.setattr(lib3mf.Reader, "AddRelationToRead", lambda *args: None)
        message = "missing Bambu part metadata"
    elif failure == "corrupted":
        monkeypatch.setattr(
            lib3mf.Attachment, "WriteToBuffer", lambda *args: list(b"<config/>")
        )
        message = "incorrect Bambu part metadata"
    else:
        def fail_write(*args):
            raise RuntimeError("attachment write failed")
        monkeypatch.setattr(lib3mf.Attachment, "ReadFromBuffer", fail_write)
        message = "attachment write failed"
    with pytest.raises(Gpx2StlError, match=message):
        export_geometry(geometry, config)
    assert output.read_bytes() == b"original destination"
    assert list(tmp_path.iterdir()) == [output]


def test_variable_extrusion_normalizes_microscopic_boundary_clearance() -> None:
    polygon = box(0.0, 0.0, 10.0, 10.0).difference(
        box(2.0, 1e-10, 8.0, 8.0)
    )
    mesh = _variable_extrusion(
        polygon,
        lambda points: np.zeros(len(points)),
        lambda points: np.ones(len(points)),
    )
    assert mesh.is_volume
    assert mesh.volume == pytest.approx(polygon.area, abs=1e-6)


@pytest.mark.parametrize("scale", [0.01, 0.02])
@pytest.mark.parametrize("multiplier", [0.5, 1.0, 3.0])
@pytest.mark.parametrize("route_width", [0.1, 1.0])
def test_road_width_uses_source_meters_and_multiplier_not_route_width(
    simple_gpx: Path, scale: float, multiplier: float, route_width: float,
) -> None:
    route = project_paths(read_gpx(simple_gpx))
    route = replace(route, paths=(np.array([[-100.0, 500.0], [100.0, 500.0]]),))
    transform = ModelTransform(Footprint("square", np.zeros(2), 1000.0), scale)
    road = CityRoad(LineString([(-100, 0), (100, 0)]), 6.0)
    surface = lambda points: np.full(len(points), 2.0)
    mesh = _road_mesh(
        (road,), route, transform, surface, route_width, 0.6,
        box(-10, -10, 10, 10), multiplier,
    )
    cavity = _road_mesh(
        (road,), route, transform, surface, route_width, 0.6,
        box(-10, -10, 10, 10), multiplier, cavity_top=3.0,
    )
    assert mesh is not None and cavity is not None
    width = 6.0 * scale * multiplier
    assert mesh.extents == pytest.approx((200 * scale + width, width, 0.6))
    assert cavity.bounds[:, :2] == pytest.approx(mesh.bounds[:, :2])
    assert mesh.is_watertight and cavity.is_watertight


def test_road_width_clipping_and_route_priority(simple_gpx: Path) -> None:
    route = replace(
        project_paths(read_gpx(simple_gpx)),
        paths=(np.array([[-100.0, 0.0], [100.0, 0.0]]),),
    )
    transform = ModelTransform(Footprint("square", np.zeros(2), 1000.0), 0.01)
    road = CityRoad(LineString([(-100, 0), (100, 0)]), 20.0)
    mesh = _road_mesh(
        (road,), route, transform, lambda points: np.full(len(points), 2.0),
        0.1, 0.6, box(-0.5, -0.5, 0.5, 0.5), 1.0,
    )
    assert mesh is not None and mesh.is_watertight
    assert mesh.bounds[:, :2] == pytest.approx(
        np.array([[-0.5, -0.1], [0.5, 0.1]])
    )
    assert len(mesh.split()) == 2
    assert mesh.volume == pytest.approx(1.0 * (0.2 - 0.1) * 0.6)


def test_road_mesh_preserves_distinct_road_widths(simple_gpx: Path) -> None:
    route = replace(
        project_paths(read_gpx(simple_gpx)),
        paths=(np.array([[-100.0, 500.0], [100.0, 500.0]]),),
    )
    transform = ModelTransform(Footprint("square", np.zeros(2), 1000.0), 0.01)
    roads = (
        CityRoad(LineString([(-100, 0), (100, 0)]), 10.0),
        CityRoad(LineString([(-100, 100), (100, 100)]), 2.5),
    )
    mesh = _road_mesh(
        roads, route, transform, lambda points: np.full(len(points), 2.0),
        1.0, 0.6, box(-10, -10, 10, 10), 1.0,
    )
    assert mesh is not None
    components = sorted(mesh.split(), key=lambda part: part.centroid[1])
    assert len(components) == 2
    assert [part.extents[1] for part in components] == pytest.approx([0.1, 0.025])


def test_nozzle_compensation_expands_and_groups_structure_footprints() -> None:
    clip = box(-1.0, -1.0, 2.0, 2.0)
    left = box(0.0, 0.0, 0.1, 1.0)
    right = box(0.25, 0.0, 0.35, 1.0)

    detailed = _compensate_structure_footprint(left, None, None)
    compensated_left = _compensate_structure_footprint(left, 0.2, clip)
    compensated_right = _compensate_structure_footprint(right, 0.2, clip)

    assert detailed.equals_exact(left, 0.0)
    assert compensated_left.bounds == pytest.approx((-0.1, -0.1, 0.2, 1.1))
    assert compensated_left.bounds[2] - compensated_left.bounds[0] >= 0.2
    assert compensated_left.intersects(compensated_right)


def test_nozzle_compensation_closes_small_holes_and_clips_to_terrain() -> None:
    structure = box(0.0, 0.0, 1.0, 1.0).difference(
        box(0.45, 0.45, 0.55, 0.55)
    )

    compensated = _compensate_structure_footprint(
        structure,
        0.2,
        box(0.0, 0.0, 0.9, 0.9),
    )

    assert compensated.bounds == pytest.approx((0.0, 0.0, 0.9, 0.9))
    assert isinstance(compensated, Polygon)
    assert len(compensated.interiors) == 0


def test_grouped_structures_preserve_stepped_heights(
    simple_gpx: Path, tmp_path: Path
) -> None:
    footprint = Footprint(
        "square",
        np.zeros(2, dtype=np.float64),
        5.0,
    )
    transform = ModelTransform(footprint, 1.0)
    config = Config(
        gpx_file=simple_gpx,
        output=tmp_path / "unused.3mf",
        mode="city",
        building_height_scale=1.0,
        nozzle_diameter=0.2,
    )
    buildings = (
        SimpleNamespace(polygon=box(0.0, 0.0, 0.1, 1.0), height_m=1.0),
        SimpleNamespace(polygon=box(0.25, 0.0, 0.35, 1.0), height_m=2.0),
    )

    mesh = _building_mesh(
        buildings,
        transform,
        lambda points: np.zeros(len(points)),
        config,
        box(-5.0, -5.0, 5.0, 5.0),
    )

    assert mesh is not None and mesh.is_volume
    assert mesh.bounds[:, 0] == pytest.approx([-0.1, 0.45])
    assert np.any(np.isclose(mesh.vertices[:, 2], 1.0))
    assert np.any(np.isclose(mesh.vertices[:, 2], 2.0))


def test_structure_overlap_is_assigned_to_taller_step() -> None:
    short = box(0.0, 0.0, 1.0, 1.0)
    tall = box(0.5, 0.0, 1.5, 1.0)

    resolved = _resolve_structure_overlaps(
        [(short, 1.0), (tall, 2.0)]
    )

    tall_parts = [polygon for polygon, height in resolved if height == 2.0]
    short_parts = [polygon for polygon, height in resolved if height == 1.0]
    assert shapely.union_all(tall_parts).equals(tall)
    assert shapely.union_all(short_parts).area == pytest.approx(0.5)
    assert sum(polygon.area for polygon, _ in resolved) == pytest.approx(
        shapely.union_all([short, tall]).area
    )
    for index, (polygon, _) in enumerate(resolved):
        for other, _ in resolved[index + 1 :]:
            assert polygon.intersection(other).area == pytest.approx(0.0)


def test_structure_overlap_discards_sub_precision_remainders() -> None:
    almost_covered = box(0.0, 0.0, 1.0000004, 1.0)
    taller = box(0.0, 0.0, 1.0, 1.0)

    resolved = _resolve_structure_overlaps(
        [(almost_covered, 1.0), (taller, 2.0)]
    )

    assert len(resolved) == 1
    assert resolved[0][1] == 2.0
    assert resolved[0][0].equals(taller)


def test_structure_printable_clip_supports_circle_and_custom_base() -> None:
    footprint = Footprint(
        "circle",
        np.array([10.0, 20.0]),
        5.0,
    )
    transform = ModelTransform(
        footprint,
        2.0,
        np.array([3.0, 4.0]),
    )

    circle = _structure_printable_clip(footprint, transform, None)
    custom_polygon = box(-2.0, -1.0, 2.0, 1.0)
    custom = _structure_printable_clip(
        footprint,
        transform,
        SimpleNamespace(terrain_polygon=custom_polygon),
    )

    assert circle.bounds == pytest.approx((-7.0, -6.0, 13.0, 14.0))
    assert circle.area == pytest.approx(np.pi * 100.0, rel=5e-4)
    assert custom.equals_exact(custom_polygon, 0.0)


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
    road = LineString(
        [
            footprint.center + (-half_size * 4, half_size * 4),
            footprint.center + (half_size * 4, half_size * 4),
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
        roads=(CityRoad(road, 6.0),),
        water=(water_polygon,),
    )

    assert geometry.base.is_watertight
    assert geometry.topography is not None and geometry.topography.is_watertight
    assert geometry.route.is_watertight
    assert geometry.buildings is not None and geometry.buildings.is_watertight
    assert geometry.water is not None and geometry.water.is_watertight
    assert geometry.roads is not None and geometry.roads.is_watertight
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
        "Roads",
        "Topography",
        "Water",
    }
    assert _mesh_material_ids(model)["Buildings"] == 5
    assert _mesh_material_ids(model)["Water"] == 6
    assert _mesh_material_ids(model)["Roads"] == 7
    _assert_bambu_settings(config.output, {
        "GPX route": 1, "Base": 4, "Topography": 2,
        "Buildings": 5, "Water": 6, "Roads": 7,
    })


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
    footprint = create_footprint(route.points, shape, 10.0, 1.0)
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


@pytest.mark.parametrize("shape", ["square", "circle", "hex"])
def test_text_margin_controls_generated_frame_clearance(
    simple_gpx: Path, tmp_path: Path, shape: Shape,
) -> None:
    geometry, config = _text_geometry(
        simple_gpx,
        tmp_path / "unused.3mf",
        True,
        shape=shape,
        text_margin=0.25,
    )
    assert geometry.text is not None
    outer = Polygon(_model_outline(shape, config.max_size))
    inner = Polygon(_model_outline(shape, config.terrain_size, segments=256))
    layout = _generated_text_layout(config, config.terrain_size / 2)
    for glyph in layout.polygons:
        assert outer.covers(glyph)
        assert not inner.intersects(glyph)
        assert glyph.distance(outer.boundary) >= config.text_margin - 1e-7
        assert glyph.distance(inner) >= config.text_margin - 1e-7


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


def _assembly_names(model: object) -> set[str]:
    names = set()
    assemblies = model.GetComponentsObjects()
    while assemblies.MoveNext():
        names.add(assemblies.GetCurrentComponentsObject().GetName())
    return names


def test_3mf_round_trip_has_two_meshes_and_four_material_slots(
    simple_gpx: Path, tmp_path: Path
) -> None:
    output = tmp_path / "Morning Ride.3mf"
    geometry, config = _geometry(simple_gpx, output, True)
    export_geometry(geometry, config)
    wrapper = lib3mf.get_wrapper()
    model = wrapper.CreateModel()
    model.QueryReader("3mf").ReadFromFile(str(output))
    assert model.GetMeshObjects().Count() == 2
    assert _assembly_names(model) == {"Morning Ride"}
    assert _mesh_names(model) == {"Base", "GPX route"}
    assert _mesh_material_ids(model) == {"Base": 4, "GPX route": 1}
    _assert_bambu_settings(output, {"Base": 4, "GPX route": 1})
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
    _assert_bambu_settings(output, {"Base": 4, "GPX route": 1, "Text": 3})
    groups = model.GetBaseMaterialGroups()
    assert groups.MoveNext()
    group = groups.GetCurrentBaseMaterialGroup()
    assert group.GetAllPropertyIDs() == [1, 2, 3, 4]


@pytest.mark.parametrize("shape", ["square", "circle", "hex"])
@pytest.mark.parametrize("text_mode", ["raised", "embedded"])
def test_3mf_with_topography_and_text_has_four_named_objects(
    simple_gpx: Path, tmp_path: Path, shape: Shape, text_mode: str,
) -> None:
    output = tmp_path / "route-topo-text.3mf"
    geometry, config = _text_geometry(
        simple_gpx, output, True, shape=shape, text_mode=text_mode,
    )
    config = Config(**{**config.__dict__, "topo": True})
    paths = read_gpx(simple_gpx)
    route = project_paths(paths)
    footprint = create_footprint(route.points, config.shape, 10.0, 1.0)
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
    _assert_bambu_settings(output, {
        "Base": 4, "GPX route": 1, "Text": 3, "Topography": 2,
    })
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
    objects = model.GetMeshObjects()
    while objects.MoveNext():
        mesh = objects.GetCurrentMeshObject()
        if mesh.GetName() == "Topography":
            points = np.array([vertex.Coordinates[:] for vertex in mesh.GetVertices()])
            outline = MultiPoint(points[:, :2]).convex_hull
            if shape == "circle":
                radii = np.linalg.norm(np.asarray(outline.exterior.coords), axis=1)
                assert radii == pytest.approx(config.terrain_size / 2, abs=1e-6)
                assert outline.area == pytest.approx(
                    np.pi * (config.terrain_size / 2) ** 2, rel=0.003,
                )
            else:
                expected = Polygon(_model_outline(shape, config.terrain_size))
                assert outline.symmetric_difference(expected).area < 1e-4
                assert len(outline.exterior.coords) - 1 == (6 if shape == "hex" else 4)


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


@pytest.mark.parametrize("shape", ["square", "circle", "hex"])
def test_stl_unions_separate_base_topography_route_and_text(
    simple_gpx: Path, tmp_path: Path, shape: Shape,
) -> None:
    output = tmp_path / "route-topo-text.stl"
    _, original_config = _text_geometry(simple_gpx, output, False, shape=shape)
    config = replace(original_config, topo=True, terrain_height=3.0)
    route = project_paths(read_gpx(simple_gpx))
    footprint = create_footprint(route.points, config.shape, 10.0, 1.0)
    footprint = add_route_clearance(footprint, 1.0, 14.0)
    transform = create_model_transform(footprint, 14.0)

    class BowlDem:
        def sample_projected(self, points, route):
            return np.linalg.norm(points - points.mean(axis=0), axis=1)

    geometry = build_geometry(route, footprint, transform, config, BowlDem())

    export_geometry(geometry, config)

    loaded = trimesh.load_mesh(output, process=True)
    assert isinstance(loaded, trimesh.Trimesh)
    assert loaded.is_watertight
    section = loaded.section(
        plane_origin=[0, 0, config.base_height + 0.1],
        plane_normal=[0, 0, 1],
    )
    assert section is not None
    outlines = list(polygonize([
        LineString(section.vertices[entity.points, :2])
        for entity in section.entities
    ]))
    terrain = max(outlines, key=lambda polygon: polygon.area).convex_hull
    expected = (
        MultiPoint(geometry.topography.vertices[:, :2]).convex_hull
        if shape == "circle"
        else Polygon(_model_outline(shape, config.terrain_size))
    )
    assert terrain.symmetric_difference(expected).area < 1e-4
