from __future__ import annotations

import os
import tempfile
from pathlib import Path

import lib3mf
import numpy as np
import trimesh

from gpx2stl.errors import Gpx2StlError
from gpx2stl.mesh import Geometry
from gpx2stl.models import Config
from gpx2stl.progress import ProgressCallback, console_progress


def _color(red: int, green: int, blue: int) -> lib3mf.Color:
    color = lib3mf.Color()
    color.Red = red
    color.Green = green
    color.Blue = blue
    color.Alpha = 255
    return color


def _add_mesh(model: object, mesh: trimesh.Trimesh, name: str) -> object:
    mesh_object = model.AddMeshObject()
    mesh_object.SetName(name)
    vertices: list[lib3mf.Position] = []
    for vertex in mesh.vertices:
        position = lib3mf.Position()
        position.Coordinates[0] = float(vertex[0])
        position.Coordinates[1] = float(vertex[1])
        position.Coordinates[2] = float(vertex[2])
        vertices.append(position)
    triangles: list[lib3mf.Triangle] = []
    for face in mesh.faces:
        triangle = lib3mf.Triangle()
        triangle.Indices[0] = int(face[0])
        triangle.Indices[1] = int(face[1])
        triangle.Indices[2] = int(face[2])
        triangles.append(triangle)
    mesh_object.SetGeometry(vertices, triangles)
    return mesh_object


def _write_3mf(path: Path, geometry: Geometry, topo: bool) -> None:
    try:
        wrapper = lib3mf.get_wrapper()
        model = wrapper.CreateModel()
        materials = model.AddBaseMaterialGroup()
        route_material = materials.AddMaterial("Filament 1 - Route", _color(255, 80, 40))
        terrain_label = "Topography" if topo else "Base"
        terrain_material = materials.AddMaterial(
            f"Filament 2 - {terrain_label}", _color(180, 180, 180)
        )
        text_material = None
        if geometry.text is not None:
            text_material = materials.AddMaterial(
                "Filament 3 - Text", _color(40, 100, 220)
            )

        route = _add_mesh(model, geometry.route, "GPX route")
        route.SetObjectLevelProperty(materials.GetResourceID(), route_material)
        terrain = _add_mesh(model, geometry.terrain, terrain_label)
        terrain.SetObjectLevelProperty(materials.GetResourceID(), terrain_material)

        assembly = model.AddComponentsObject()
        assembly.SetName("GPX route and topography")
        identity = wrapper.GetIdentityTransform()
        assembly.AddComponent(route, identity)
        assembly.AddComponent(terrain, identity)
        if geometry.text is not None and text_material is not None:
            text = _add_mesh(model, geometry.text, "Text")
            text.SetObjectLevelProperty(materials.GetResourceID(), text_material)
            assembly.AddComponent(text, identity)
        model.AddBuildItem(assembly, identity)
        model.QueryWriter("3mf").WriteToFile(str(path))

        check_model = wrapper.CreateModel()
        reader = check_model.QueryReader("3mf")
        reader.ReadFromFile(str(path))
        mesh_count = check_model.GetMeshObjects().Count()
        material_groups = check_model.GetBaseMaterialGroups().Count()
        expected_meshes = 3 if geometry.text is not None else 2
        if mesh_count != expected_meshes or material_groups != 1:
            raise Gpx2StlError(
                f"3MF validation failed: expected {expected_meshes} meshes "
                "and one material group."
            )
    except Gpx2StlError:
        raise
    except Exception as exc:
        raise Gpx2StlError(f"Unable to write valid 3MF output: {exc}") from exc


def _write_stl(path: Path, geometry: Geometry) -> None:
    try:
        meshes = [geometry.terrain, geometry.route]
        if geometry.text is not None:
            meshes.append(geometry.text)
        combined = trimesh.boolean.union(
            meshes,
            engine="manifold",
            check_volume=True,
        )
    except Exception as exc:
        raise Gpx2StlError(f"Unable to combine route and terrain for STL: {exc}") from exc
    if not isinstance(combined, trimesh.Trimesh) or combined.is_empty:
        raise Gpx2StlError("Unable to combine route and terrain into one STL mesh.")
    components = combined.split(only_watertight=True)
    if not components:
        raise Gpx2StlError("The combined STL contains no watertight component.")
    combined = max(components, key=lambda component: abs(component.volume))
    combined.merge_vertices(digits_vertex=12)
    combined.update_faces(combined.nondegenerate_faces())
    combined.remove_unreferenced_vertices()
    if not combined.is_watertight or not np.all(np.isfinite(combined.vertices)):
        raise Gpx2StlError("Combined STL mesh is not watertight.")
    try:
        combined.export(path, file_type="stl")
        loaded = trimesh.load_mesh(path, file_type="stl", process=True)
    except Exception as exc:
        raise Gpx2StlError(f"Unable to write or verify STL output: {exc}") from exc
    if not isinstance(loaded, trimesh.Trimesh) or not loaded.is_watertight:
        raise Gpx2StlError("STL validation failed after writing the file.")


def export_geometry(
    geometry: Geometry,
    config: Config,
    progress: ProgressCallback = console_progress,
) -> None:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f".{config.output.stem}-",
            suffix=config.output.suffix,
            dir=config.output.parent,
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
        if config.use_3mf:
            material_count = 3 if geometry.text is not None else 2
            progress(f"Writing {material_count}-material 3MF package")
            _write_3mf(temporary_path, geometry, config.topo)
            progress("Validated 3MF mesh and material resources")
        else:
            progress("Unioning route and terrain into one STL mesh")
            _write_stl(temporary_path, geometry)
            progress("Validated watertight STL output")
        progress("Atomically replacing the destination file")
        os.replace(temporary_path, config.output)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
