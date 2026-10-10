from __future__ import annotations

import os
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

import lib3mf
import numpy as np
import trimesh

from gpx2stl.errors import Gpx2StlError
from gpx2stl.mesh import Geometry
from gpx2stl.models import Config
from gpx2stl.progress import ProgressCallback, console_progress


_BAMBU_SETTINGS_PATH = "/Metadata/model_settings.config"
_BAMBU_SETTINGS_RELATIONSHIP = (
    "https://github.com/zhuangqdcn/gpx2stl/relationships/bambu-model-settings"
)


def _color(red: int, green: int, blue: int) -> lib3mf.Color:
    color = lib3mf.Color()
    color.Red = red
    color.Green = green
    color.Blue = blue
    color.Alpha = 255
    return color


def _add_mesh(
    model: lib3mf.Model, mesh: trimesh.Trimesh, name: str
) -> lib3mf.MeshObject:
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


def _bambu_model_settings(
    assembly: lib3mf.ComponentsObject,
    parts: list[tuple[lib3mf.MeshObject, int]],
) -> bytes:
    root = ET.Element("config")
    obj = ET.SubElement(root, "object", id=str(assembly.GetResourceID()))
    ET.SubElement(obj, "metadata", key="name", value=assembly.GetName())
    for mesh, filament in parts:
        part = ET.SubElement(
            obj, "part", id=str(mesh.GetResourceID()), subtype="normal_part"
        )
        ET.SubElement(part, "metadata", key="name", value=mesh.GetName())
        ET.SubElement(part, "metadata", key="extruder", value=str(filament))
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _write_3mf(path: Path, geometry: Geometry, model_name: str) -> None:
    try:
        wrapper = lib3mf.get_wrapper()
        model = wrapper.CreateModel()
        materials = model.AddBaseMaterialGroup()
        route_material = materials.AddMaterial("Filament 1 - Route", _color(255, 80, 40))
        topography_material = materials.AddMaterial(
            "Filament 2 - Topography", _color(120, 180, 100)
        )
        text_material = materials.AddMaterial(
            "Filament 3 - Text", _color(40, 100, 220)
        )
        base_material = materials.AddMaterial(
            "Filament 4 - Base", _color(180, 180, 180)
        )
        building_material = (
            materials.AddMaterial("Filament 5 - Buildings", _color(210, 190, 160))
            if (
                geometry.buildings is not None
                or geometry.water is not None
                or geometry.roads is not None
            )
            else None
        )
        water_material = (
            materials.AddMaterial("Filament 6 - Water", _color(60, 150, 220))
            if geometry.water is not None or geometry.roads is not None
            else None
        )
        road_material = (
            materials.AddMaterial("Filament 7 - Roads", _color(80, 80, 80))
            if geometry.roads is not None
            else None
        )

        route = _add_mesh(model, geometry.route, "GPX route")
        route.SetObjectLevelProperty(materials.GetResourceID(), route_material)
        base = _add_mesh(model, geometry.base, "Base")
        base.SetObjectLevelProperty(materials.GetResourceID(), base_material)

        assembly = model.AddComponentsObject()
        assembly.SetName(model_name)
        identity = wrapper.GetIdentityTransform()
        parts = [(route, 1)]
        assembly.AddComponent(route, identity)
        if geometry.topography is not None:
            topography = _add_mesh(model, geometry.topography, "Topography")
            topography.SetObjectLevelProperty(
                materials.GetResourceID(),
                topography_material,
            )
            assembly.AddComponent(topography, identity)
            parts.append((topography, 2))
        if geometry.text is not None:
            text = _add_mesh(model, geometry.text, "Text")
            text.SetObjectLevelProperty(materials.GetResourceID(), text_material)
            assembly.AddComponent(text, identity)
            parts.append((text, 3))
        if geometry.buildings is not None:
            buildings = _add_mesh(model, geometry.buildings, "Buildings")
            assert building_material is not None
            buildings.SetObjectLevelProperty(
                materials.GetResourceID(), building_material
            )
            assembly.AddComponent(buildings, identity)
            parts.append((buildings, 5))
        if geometry.water is not None:
            water = _add_mesh(model, geometry.water, "Water")
            assert water_material is not None
            water.SetObjectLevelProperty(
                materials.GetResourceID(), water_material
            )
            assembly.AddComponent(water, identity)
            parts.append((water, 6))
        if geometry.roads is not None:
            roads = _add_mesh(model, geometry.roads, "Roads")
            assert road_material is not None
            roads.SetObjectLevelProperty(
                materials.GetResourceID(), road_material
            )
            assembly.AddComponent(roads, identity)
            parts.append((roads, 7))
        assembly.AddComponent(base, identity)
        parts.append((base, 4))
        model.AddBuildItem(assembly, identity)
        # Bambu reads part names and filament indices here, not from core resources.
        settings = _bambu_model_settings(assembly, parts)
        attachment = model.AddAttachment(
            _BAMBU_SETTINGS_PATH, _BAMBU_SETTINGS_RELATIONSHIP
        )
        attachment.ReadFromBuffer(settings)
        model.AddCustomContentType("config", "application/xml")
        model.QueryWriter("3mf").WriteToFile(str(path))

        check_model = wrapper.CreateModel()
        reader = check_model.QueryReader("3mf")
        reader.AddRelationToRead(_BAMBU_SETTINGS_RELATIONSHIP)
        reader.ReadFromFile(str(path))
        mesh_count = check_model.GetMeshObjects().Count()
        material_groups = check_model.GetBaseMaterialGroups().Count()
        assemblies = check_model.GetComponentsObjects()
        expected_meshes = (
            2
            + int(geometry.topography is not None)
            + int(geometry.text is not None)
            + int(geometry.buildings is not None)
            + int(geometry.water is not None)
            + int(geometry.roads is not None)
        )
        if mesh_count != expected_meshes or material_groups != 1:
            raise Gpx2StlError(
                f"3MF validation failed: expected {expected_meshes} meshes "
                "and one material group."
            )
        if (
            assemblies.Count() != 1
            or not assemblies.MoveNext()
            or assemblies.GetCurrentComponentsObject().GetName() != model_name
        ):
            raise Gpx2StlError(
                f"3MF validation failed: expected model name '{model_name}'."
            )
        for index in range(check_model.GetAttachmentCount()):
            attachment = check_model.GetAttachment(index)
            if attachment.GetPath() == _BAMBU_SETTINGS_PATH:
                if bytes(attachment.WriteToBuffer()) != settings:
                    raise Gpx2StlError(
                        "3MF validation failed: incorrect Bambu part metadata."
                    )
                break
        else:
            raise Gpx2StlError(
                "3MF validation failed: missing Bambu part metadata."
            )
    except Gpx2StlError:
        raise
    except Exception as exc:
        raise Gpx2StlError(f"Unable to write valid 3MF output: {exc}") from exc


def _write_stl(path: Path, geometry: Geometry) -> None:
    try:
        meshes = [geometry.base, geometry.route]
        if geometry.topography is not None:
            meshes.append(geometry.topography)
        if geometry.text is not None:
            meshes.append(geometry.text)
        if geometry.buildings is not None:
            meshes.append(geometry.buildings)
        if geometry.water is not None:
            meshes.append(geometry.water)
        if geometry.roads is not None:
            meshes.append(geometry.roads)
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
            object_count = (
                2
                + int(geometry.topography is not None)
                + int(geometry.text is not None)
                + int(geometry.buildings is not None)
                + int(geometry.water is not None)
                + int(geometry.roads is not None)
            )
            material_count = (
                7
                if geometry.roads is not None
                else 6
                if geometry.water is not None
                else 5 if geometry.buildings is not None else 4
            )
            progress(
                f"Writing {object_count}-object, {material_count}-material 3MF package"
            )
            _write_3mf(temporary_path, geometry, config.output.stem)
            progress("Validated 3MF mesh and material resources")
        else:
            progress("Unioning base, route, and optional features into one STL mesh")
            _write_stl(temporary_path, geometry)
            progress("Validated watertight STL output")
        progress("Atomically replacing the destination file")
        os.replace(temporary_path, config.output)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
