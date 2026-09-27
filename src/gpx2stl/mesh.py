from __future__ import annotations

import math
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from functools import partial

import numpy as np
import shapely
import trimesh
from numpy.typing import NDArray
from scipy.interpolate import LinearNDInterpolator, NearestNDInterpolator
from shapely.geometry import LineString, MultiPolygon, Point, Polygon

from gpx2stl.dem import DemSource, fill_missing
from gpx2stl.errors import Gpx2StlError
from gpx2stl.models import Config, Footprint, ModelTransform, ProjectedRoute

HeightFunction = Callable[[NDArray[np.float64]], NDArray[np.float64]]


@dataclass(frozen=True)
class Geometry:
    terrain: trimesh.Trimesh
    route: trimesh.Trimesh


def _model_resolution(max_size: float) -> int:
    return max(24, min(256, math.ceil(max_size) + 1))


def _surface_sampler(
    points: NDArray[np.float64], heights: NDArray[np.float64]
) -> HeightFunction:
    linear = LinearNDInterpolator(points, heights, fill_value=np.nan)
    nearest = NearestNDInterpolator(points, heights)

    def sample(query: NDArray[np.float64]) -> NDArray[np.float64]:
        result = np.asarray(linear(query), dtype=np.float64)
        missing = ~np.isfinite(result)
        if np.any(missing):
            result[missing] = np.asarray(nearest(query[missing]), dtype=np.float64)
        return result

    return sample


def _structured_square(
    footprint: Footprint,
    transform: ModelTransform,
    raw_height: Callable[[NDArray[np.float64]], NDArray[np.float64]] | None,
    config: Config,
) -> tuple[trimesh.Trimesh, HeightFunction]:
    resolution = _model_resolution(config.max_size)
    radius = footprint.radius * transform.scale
    axis = np.linspace(-radius, radius, resolution)
    xx, yy = np.meshgrid(axis, axis)
    points = np.column_stack((xx.ravel(), yy.ravel()))
    if raw_height is None:
        top = np.full(len(points), config.base_height, dtype=np.float64)
    else:
        raw = fill_missing(raw_height(points).reshape(resolution, resolution)).ravel()
        span = float(np.ptp(raw))
        relief = np.zeros_like(raw) if span <= 1e-9 else (raw - float(np.min(raw))) / span
        top = config.base_height + relief * config.terrain_height

    count = len(points)
    vertices = np.vstack(
        (
            np.column_stack((points, top)),
            np.column_stack((points, np.zeros(count, dtype=np.float64))),
        )
    )
    faces: list[tuple[int, int, int]] = []
    for row in range(resolution - 1):
        for column in range(resolution - 1):
            a = row * resolution + column
            b = a + 1
            d = (row + 1) * resolution + column
            c = d + 1
            faces.extend(((a, b, c), (a, c, d)))
            faces.extend(((count + a, count + c, count + b), (count + a, count + d, count + c)))

    perimeter: list[int] = []
    perimeter.extend(range(resolution))
    perimeter.extend(row * resolution + resolution - 1 for row in range(1, resolution))
    perimeter.extend(
        (resolution - 1) * resolution + column
        for column in range(resolution - 2, -1, -1)
    )
    perimeter.extend(row * resolution for row in range(resolution - 2, 0, -1))
    _add_side_faces(faces, perimeter, count)
    mesh = trimesh.Trimesh(vertices=vertices, faces=np.asarray(faces), process=True)
    return mesh, _surface_sampler(points, top)


def _polar_circle(
    footprint: Footprint,
    transform: ModelTransform,
    raw_height: Callable[[NDArray[np.float64]], NDArray[np.float64]] | None,
    config: Config,
) -> tuple[trimesh.Trimesh, HeightFunction]:
    diameter = footprint.diameter * transform.scale
    rings = max(12, min(128, math.ceil(diameter / 2.0)))
    segments = max(48, min(512, rings * 4))
    radius = footprint.radius * transform.scale
    points = [np.array([0.0, 0.0])]
    for ring in range(1, rings + 1):
        ring_radius = radius * ring / rings
        angle = np.linspace(0.0, 2.0 * math.pi, segments, endpoint=False)
        points.extend(
            np.column_stack((ring_radius * np.cos(angle), ring_radius * np.sin(angle)))
        )
    xy = np.asarray(points, dtype=np.float64)
    if raw_height is None:
        top = np.full(len(xy), config.base_height, dtype=np.float64)
    else:
        raw = raw_height(xy)
        missing = ~np.isfinite(raw)
        if np.all(missing):
            raise Gpx2StlError("The selected DEM contains no usable elevation samples.")
        if np.any(missing):
            nearest = NearestNDInterpolator(xy[~missing], raw[~missing])
            raw[missing] = nearest(xy[missing])
        span = float(np.ptp(raw))
        relief = np.zeros_like(raw) if span <= 1e-9 else (raw - float(np.min(raw))) / span
        top = config.base_height + relief * config.terrain_height

    count = len(xy)
    vertices = np.vstack(
        (
            np.column_stack((xy, top)),
            np.column_stack((xy, np.zeros(count, dtype=np.float64))),
        )
    )
    faces: list[tuple[int, int, int]] = []
    first_ring = 1
    for segment in range(segments):
        next_segment = (segment + 1) % segments
        a = first_ring + segment
        b = first_ring + next_segment
        faces.append((0, a, b))
        faces.append((count, count + b, count + a))
    for ring in range(1, rings):
        inner = 1 + (ring - 1) * segments
        outer = 1 + ring * segments
        for segment in range(segments):
            next_segment = (segment + 1) % segments
            a = inner + segment
            b = outer + segment
            c = outer + next_segment
            d = inner + next_segment
            faces.extend(((a, b, c), (a, c, d)))
            faces.extend(
                (
                    (count + a, count + c, count + b),
                    (count + a, count + d, count + c),
                )
            )
    outer = [1 + (rings - 1) * segments + index for index in range(segments)]
    _add_side_faces(faces, outer, count)
    mesh = trimesh.Trimesh(vertices=vertices, faces=np.asarray(faces), process=True)
    return mesh, _surface_sampler(xy, top)


def _add_side_faces(
    faces: list[tuple[int, int, int]], perimeter: list[int], bottom_offset: int
) -> None:
    for index, top_a in enumerate(perimeter):
        top_b = perimeter[(index + 1) % len(perimeter)]
        bottom_a = bottom_offset + top_a
        bottom_b = bottom_offset + top_b
        faces.extend(((bottom_a, bottom_b, top_b), (bottom_a, top_b, top_a)))


def _raw_height_function(
    dem: DemSource,
    route: ProjectedRoute,
    transform: ModelTransform,
) -> Callable[[NDArray[np.float64]], NDArray[np.float64]]:
    def sample(model_points: NDArray[np.float64]) -> NDArray[np.float64]:
        projected = transform.to_projected(model_points)
        return dem.sample_projected(projected, route)

    return sample


def _polygon_parts(geometry: Polygon | MultiPolygon) -> Iterable[Polygon]:
    if isinstance(geometry, Polygon):
        yield geometry
    else:
        yield from geometry.geoms


def _variable_extrusion(
    polygon: Polygon,
    bottom_height: HeightFunction,
    top_height: HeightFunction,
) -> trimesh.Trimesh:
    rings = [polygon.exterior, *polygon.interiors]
    coordinates: list[tuple[float, float]] = []
    for ring in rings:
        coordinates.extend((float(x), float(y)) for x, y in list(ring.coords)[:-1])
    index_by_coordinate: dict[tuple[float, float], int] = {}
    unique: list[tuple[float, float]] = []
    for coordinate in coordinates:
        key = (round(coordinate[0], 10), round(coordinate[1], 10))
        if key not in index_by_coordinate:
            index_by_coordinate[key] = len(unique)
            unique.append(coordinate)
    xy = np.asarray(unique, dtype=np.float64)
    bottom = bottom_height(xy)
    top = top_height(xy)
    if np.any(top <= bottom):
        raise Gpx2StlError("Route geometry has non-positive thickness.")
    count = len(xy)
    vertices = np.vstack((np.column_stack((xy, bottom)), np.column_stack((xy, top))))
    faces: list[tuple[int, int, int]] = []
    triangles = shapely.constrained_delaunay_triangles(polygon)
    for triangle in shapely.get_parts(triangles):
        triangle_xy = list(triangle.exterior.coords)[:3]
        triangle_indices = [
            index_by_coordinate[(round(float(x), 10), round(float(y), 10))]
            for x, y in triangle_xy
        ]
        a, b, c = triangle_indices
        ab = xy[b] - xy[a]
        ac = xy[c] - xy[a]
        cross = ab[0] * ac[1] - ab[1] * ac[0]
        if float(cross) < 0:
            b, c = c, b
        faces.extend(((count + a, count + b, count + c), (a, c, b)))
    offset = count
    for ring in rings:
        ring_indices = [
            index_by_coordinate[(round(float(x), 10), round(float(y), 10))]
            for x, y in list(ring.coords)[:-1]
        ]
        _add_side_faces(faces, ring_indices, offset)
    return trimesh.Trimesh(vertices=vertices, faces=np.asarray(faces), process=True)


def _route_mesh(
    route: ProjectedRoute,
    transform: ModelTransform,
    surface_height: HeightFunction,
    config: Config,
) -> trimesh.Trimesh:
    components: list[trimesh.Trimesh] = []
    model_paths = tuple(transform.to_model(path) for path in route.paths)
    all_elevations = np.concatenate(route.elevations)
    if not config.topo:
        finite = all_elevations[np.isfinite(all_elevations)]
        minimum_height = float(np.min(finite)) * 0.05
        elevation_shift = max(0.2 - minimum_height, 0.0)

    for path, elevations in zip(model_paths, route.elevations):
        line = LineString(path)
        buffered = line.buffer(
            config.route_width / 2.0,
            cap_style="round",
            join_style="round",
            quad_segs=4,
        )
        if buffered.is_empty:
            continue
        cumulative = np.concatenate(([0.0], np.cumsum(np.linalg.norm(np.diff(path, axis=0), axis=1))))

        if config.topo:

            def top_height(points: NDArray[np.float64]) -> NDArray[np.float64]:
                return surface_height(points) + config.route_height

        else:
            top_height = partial(
                _no_topo_route_height,
                line=line,
                cumulative=cumulative,
                elevations=elevations,
                base_height=config.base_height,
                elevation_shift=elevation_shift,
            )

        def bottom_height(points: NDArray[np.float64]) -> NDArray[np.float64]:
            if config.topo:
                return np.maximum(0.0, surface_height(points) - 0.05)
            return np.full(len(points), max(0.0, config.base_height - 0.05))

        for polygon in _polygon_parts(buffered):
            components.append(_variable_extrusion(polygon, bottom_height, top_height))
    if not components:
        raise Gpx2StlError("No printable route geometry could be generated.")
    route_mesh = trimesh.util.concatenate(components)
    route_mesh.remove_unreferenced_vertices()
    return route_mesh


def _no_topo_route_height(
    points: NDArray[np.float64],
    *,
    line: LineString,
    cumulative: NDArray[np.float64],
    elevations: NDArray[np.float64],
    base_height: float,
    elevation_shift: float,
) -> NDArray[np.float64]:
    distance = np.asarray([line.project(Point(point)) for point in points])
    altitude = np.interp(distance, cumulative, elevations)
    return base_height + altitude * 0.05 + elevation_shift


def build_geometry(
    route: ProjectedRoute,
    footprint: Footprint,
    transform: ModelTransform,
    config: Config,
    dem: DemSource | None,
) -> Geometry:
    raw_height = None if dem is None else _raw_height_function(dem, route, transform)
    if footprint.shape == "square":
        terrain, surface = _structured_square(footprint, transform, raw_height, config)
    else:
        terrain, surface = _polar_circle(footprint, transform, raw_height, config)
    route_mesh = _route_mesh(route, transform, surface, config)
    for name, mesh in (("terrain/base", terrain), ("route", route_mesh)):
        if not mesh.is_watertight:
            raise Gpx2StlError(f"Generated {name} mesh is not watertight.")
        if not np.all(np.isfinite(mesh.vertices)) or mesh.volume <= 0:
            raise Gpx2StlError(f"Generated {name} mesh is invalid.")
    return Geometry(terrain=terrain, route=route_mesh)
