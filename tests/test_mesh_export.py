from __future__ import annotations

from pathlib import Path

import lib3mf
import numpy as np
import trimesh

from gpx2stl.export import export_geometry
from gpx2stl.footprint import create_footprint, create_model_transform
from gpx2stl.gpx import interpolate_elevations, project_paths, read_gpx
from gpx2stl.mesh import build_geometry
from gpx2stl.models import Config


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
    assert geometry.terrain.is_watertight
    assert geometry.route.is_watertight
    assert np.isclose(np.ptp(geometry.terrain.vertices[:, 0]), 20.0)


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
    assert geometry.terrain.is_watertight
    assert geometry.route.is_watertight
    assert np.isclose(np.ptp(geometry.terrain.vertices[:, 0]), 20.0)
    assert geometry.terrain.bounds[1, 2] >= config.base_height + config.terrain_height


def test_3mf_round_trip_has_two_meshes_and_materials(
    simple_gpx: Path, tmp_path: Path
) -> None:
    output = tmp_path / "route.3mf"
    geometry, config = _geometry(simple_gpx, output, True)
    export_geometry(geometry, config)
    wrapper = lib3mf.get_wrapper()
    model = wrapper.CreateModel()
    model.QueryReader("3mf").ReadFromFile(str(output))
    assert model.GetMeshObjects().Count() == 2
    groups = model.GetBaseMaterialGroups()
    assert groups.MoveNext()
    group = groups.GetCurrentBaseMaterialGroup()
    assert group.GetAllPropertyIDs() == [1, 2]


def test_stl_round_trip_is_watertight(simple_gpx: Path, tmp_path: Path) -> None:
    output = tmp_path / "route.stl"
    geometry, config = _geometry(simple_gpx, output, False)
    export_geometry(geometry, config)
    loaded = trimesh.load_mesh(output, process=True)
    assert isinstance(loaded, trimesh.Trimesh)
    assert loaded.is_watertight
