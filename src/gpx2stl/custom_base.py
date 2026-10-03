from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import shapely
import trimesh
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import polylabel

from gpx2stl.errors import Gpx2StlError
from gpx2stl.models import Footprint, ModelTransform, ProjectedRoute


@dataclass(frozen=True)
class CustomBase:
    mesh: trimesh.Trimesh
    top_polygon: Polygon
    terrain_polygon: Polygon
    inset_distance: float
    top_z: float
    transform: ModelTransform

    @property
    def overlap_depth(self) -> float:
        thickness = self.top_z - float(self.mesh.bounds[0, 2])
        return min(0.05, thickness / 2.0)


def _load_mesh(path: Path) -> trimesh.Trimesh:
    try:
        loaded = trimesh.load(path, file_type="stl", force="mesh", process=True)
    except Exception as exc:
        raise Gpx2StlError(f"Unable to load base STL '{path}': {exc}") from exc
    if not isinstance(loaded, trimesh.Trimesh) or loaded.is_empty:
        raise Gpx2StlError(f"Base STL '{path}' contains no mesh geometry.")
    loaded.remove_unreferenced_vertices()
    if not np.all(np.isfinite(loaded.vertices)):
        raise Gpx2StlError(f"Base STL '{path}' contains non-finite vertices.")
    if not loaded.is_watertight or not loaded.is_winding_consistent:
        raise Gpx2StlError(f"Base STL '{path}' must be a watertight, consistently wound mesh.")
    if not np.isfinite(loaded.volume) or loaded.volume <= 0:
        raise Gpx2StlError(f"Base STL '{path}' must have positive volume.")
    if np.any(loaded.extents <= 1e-6):
        raise Gpx2StlError(f"Base STL '{path}' has degenerate dimensions.")
    return loaded


def _extract_top_polygon(mesh: trimesh.Trimesh, path: Path) -> tuple[Polygon, float]:
    top_z = float(mesh.bounds[1, 2])
    tolerance = max(1e-7, float(np.max(mesh.extents)) * 1e-7)
    face_vertices = mesh.vertices[mesh.faces]
    at_top = np.all(np.abs(face_vertices[:, :, 2] - top_z) <= tolerance, axis=1)
    upward = mesh.face_normals[:, 2] >= 1.0 - 1e-7
    face_indices = np.flatnonzero(at_top & upward)
    if not len(face_indices):
        raise Gpx2StlError(
            f"Base STL '{path}' has no horizontal upward-facing region at its maximum Z."
        )
    triangles = [
        Polygon(mesh.vertices[mesh.faces[index], :2])
        for index in face_indices
    ]
    top = shapely.union_all(triangles)
    if not top.is_valid:
        top = shapely.make_valid(top)
    polygons = [
        geometry
        for geometry in shapely.get_parts(top)
        if isinstance(geometry, Polygon) and geometry.area > tolerance * tolerance
    ]
    if len(polygons) != 1:
        raise Gpx2StlError(
            f"Base STL '{path}' must have exactly one connected highest flat top region."
        )
    return shapely.orient_polygons(polygons[0]), top_z


def _inset_top(
    top: Polygon,
    boundary_percent: float,
    path: Path,
) -> tuple[Polygon, float]:
    minimum_x, minimum_y, maximum_x, maximum_y = top.bounds
    minimum_span = min(maximum_x - minimum_x, maximum_y - minimum_y)
    distance = boundary_percent / 100.0 * minimum_span
    inset = top if distance == 0.0 else top.buffer(-distance, join_style="mitre")
    if not inset.is_valid:
        inset = shapely.make_valid(inset)
    polygons = [
        geometry
        for geometry in shapely.get_parts(inset)
        if isinstance(geometry, Polygon) and geometry.area > 1e-9
    ]
    if len(polygons) != 1:
        raise Gpx2StlError(
            f"Insetting the top of base STL '{path}' by {boundary_percent:g}% "
            "collapsed or disconnected the terrain region."
        )
    return shapely.orient_polygons(polygons[0]), distance


def _fit_transform(
    route: ProjectedRoute,
    terrain: Polygon,
    route_width: float,
) -> ModelTransform:
    route_minimum = route.points.min(axis=0)
    route_maximum = route.points.max(axis=0)
    source_center = (route_minimum + route_maximum) / 2.0
    target_point = polylabel(terrain, tolerance=max(1e-6, np.sqrt(terrain.area) * 1e-6))
    target_center = np.array([target_point.x, target_point.y], dtype=np.float64)
    clearance = terrain.buffer(
        -route_width / 2.0,
        join_style="round",
    )
    if clearance.is_empty or not clearance.covers(Point(target_center)):
        raise Gpx2StlError(
            "The custom base terrain region is too narrow for the requested route width."
        )
    centered_route = shapely.union_all(
        [LineString(path - source_center) for path in route.paths]
    )

    def fits(scale: float) -> bool:
        transformed = shapely.transform(
            centered_route,
            lambda coordinates: coordinates * scale + target_center,
        )
        return bool(clearance.covers(transformed))

    terrain_width = terrain.bounds[2] - terrain.bounds[0]
    terrain_height = terrain.bounds[3] - terrain.bounds[1]
    route_span = np.maximum(route_maximum - route_minimum, 1e-12)
    high = float(min(terrain_width / route_span[0], terrain_height / route_span[1]))
    if high <= 0 or not np.isfinite(high):
        raise Gpx2StlError("Unable to determine a scale for the custom base.")
    while fits(high):
        high *= 2.0
    low = 0.0
    for _ in range(48):
        middle = (low + high) / 2.0
        if fits(middle):
            low = middle
        else:
            high = middle
    if low <= 1e-12:
        raise Gpx2StlError("Unable to fit the GPX route on the custom base top.")
    radius = max(float(np.max(np.linalg.norm(route.points - source_center, axis=1))), 1e-9)
    footprint = Footprint("circle", source_center, radius)
    return ModelTransform(footprint, low * (1.0 - 1e-9), target_center)


def prepare_custom_base(
    path: Path,
    boundary_percent: float,
    route: ProjectedRoute,
    route_width: float,
) -> CustomBase:
    mesh = _load_mesh(path)
    top, top_z = _extract_top_polygon(mesh, path)
    terrain, inset_distance = _inset_top(top, boundary_percent, path)
    transform = _fit_transform(route, terrain, route_width)
    return CustomBase(mesh, top, terrain, inset_distance, top_z, transform)


def sample_exterior(polygon: Polygon, minimum_samples: int = 180) -> np.ndarray:
    coordinates = np.asarray(polygon.exterior.coords, dtype=np.float64)
    lengths = np.linalg.norm(np.diff(coordinates, axis=0), axis=1)
    perimeter = float(np.sum(lengths))
    points: list[np.ndarray] = []
    for start, end, length in zip(coordinates[:-1], coordinates[1:], lengths):
        count = max(1, int(np.ceil(minimum_samples * float(length) / perimeter)))
        fractions = np.arange(count, dtype=np.float64) / count
        points.extend(start + (end - start) * fraction for fraction in fractions)
    return np.asarray(points, dtype=np.float64)
