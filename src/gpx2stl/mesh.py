from __future__ import annotations

import math
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from functools import partial
from pathlib import Path

import numpy as np
import shapely
import trimesh
from matplotlib import font_manager
from matplotlib.font_manager import FontProperties
from matplotlib.ft2font import FT2Font
from matplotlib.textpath import TextPath, TextToPath
from numpy.typing import NDArray
from scipy.interpolate import LinearNDInterpolator, NearestNDInterpolator
from scipy.spatial import Delaunay
from shapely.geometry import LineString, MultiPolygon, Point, Polygon

from gpx2stl.dem import DemSource, fill_missing
from gpx2stl.errors import Gpx2StlError
from gpx2stl.models import Config, Footprint, ModelTransform, ProjectedRoute

HeightFunction = Callable[[NDArray[np.float64]], NDArray[np.float64]]


@dataclass(frozen=True)
class Geometry:
    terrain: trimesh.Trimesh
    route: trimesh.Trimesh
    text: trimesh.Trimesh | None = None


def _terrain_relief_height(
    raw: NDArray[np.float64],
    transform: ModelTransform,
    configured_height: float | None,
) -> NDArray[np.float64]:
    span = float(np.ptp(raw))
    if span <= 1e-9:
        return np.zeros_like(raw)
    height = configured_height if configured_height is not None else span * transform.scale
    return (raw - float(np.min(raw))) / span * height


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


def _model_outline(shape: str, diameter: float, segments: int = 128) -> NDArray[np.float64]:
    radius = diameter / 2.0
    if shape == "square":
        return np.array(
            [
                [-radius, -radius],
                [radius, -radius],
                [radius, radius],
                [-radius, radius],
            ],
            dtype=np.float64,
        )
    count = 6 if shape == "hex" else segments
    angles = np.linspace(0.0, 2.0 * math.pi, count, endpoint=False)
    return radius * np.column_stack((np.cos(angles), np.sin(angles)))


def _convex_prism(outline: NDArray[np.float64], height: float) -> trimesh.Trimesh:
    count = len(outline)
    vertices = np.vstack(
        (
            np.column_stack((outline, np.full(count, height))),
            np.column_stack((outline, np.zeros(count))),
        )
    )
    faces: list[tuple[int, int, int]] = []
    for index in range(1, count - 1):
        faces.append((0, index, index + 1))
        faces.append((count, count + index + 1, count + index))
    _add_side_faces(faces, list(range(count)), count)
    return trimesh.Trimesh(vertices=vertices, faces=np.asarray(faces), process=True)


def _polygon_terrain(
    footprint: Footprint,
    transform: ModelTransform,
    raw_height: Callable[[NDArray[np.float64]], NDArray[np.float64]] | None,
    config: Config,
) -> tuple[trimesh.Trimesh, HeightFunction]:
    outline = transform.to_model(
        np.asarray(
            [
                footprint.center
                + footprint.radius * np.array([math.cos(angle), math.sin(angle)])
                for angle in np.deg2rad(np.arange(0.0, 360.0, 60.0))
            ]
        )
    )
    polygon = Polygon(outline)
    resolution = _model_resolution(config.max_size)
    spacing = footprint.diameter * transform.scale / (resolution - 1)
    minimum_x, minimum_y, maximum_x, maximum_y = polygon.bounds
    x_axis = np.arange(minimum_x, maximum_x + spacing * 0.5, spacing)
    y_axis = np.arange(minimum_y, maximum_y + spacing * 0.5, spacing)
    samples = [
        (x, y)
        for y in y_axis
        for x in x_axis
        if polygon.contains(Point(float(x), float(y)))
    ]
    perimeter = [(float(point[0]), float(point[1])) for point in outline]
    points = np.asarray([*perimeter, *samples], dtype=np.float64)
    points = np.unique(np.round(points, decimals=10), axis=0)
    index_by_coordinate = {
        (float(point[0]), float(point[1])): index for index, point in enumerate(points)
    }
    perimeter_indices = [
        index_by_coordinate[(round(x, 10), round(y, 10))] for x, y in perimeter
    ]
    triangulation = Delaunay(points)
    triangles = [
        tuple(int(index) for index in simplex)
        for simplex in triangulation.simplices
        if polygon.covers(Point(np.mean(points[simplex], axis=0)))
    ]
    if raw_height is None:
        top = np.full(len(points), config.base_height, dtype=np.float64)
    else:
        raw = np.asarray(raw_height(points), dtype=np.float64)
        missing = ~np.isfinite(raw)
        if np.all(missing):
            raise Gpx2StlError("The selected DEM contains no usable elevation samples.")
        if np.any(missing):
            nearest = NearestNDInterpolator(points[~missing], raw[~missing])
            raw[missing] = nearest(points[missing])
        top = config.base_height + _terrain_relief_height(
            raw, transform, config.terrain_height
        )

    count = len(points)
    vertices = np.vstack(
        (
            np.column_stack((points, top)),
            np.column_stack((points, np.zeros(count))),
        )
    )
    faces: list[tuple[int, int, int]] = []
    for a, b, c in triangles:
        ab = points[b] - points[a]
        ac = points[c] - points[a]
        cross = ab[0] * ac[1] - ab[1] * ac[0]
        if float(cross) < 0:
            b, c = c, b
        faces.extend(((a, b, c), (count + a, count + c, count + b)))
    _add_side_faces(faces, perimeter_indices, count)
    mesh = trimesh.Trimesh(vertices=vertices, faces=np.asarray(faces), process=True)
    return mesh, _surface_sampler(points, top)


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
        relief = _terrain_relief_height(raw, transform, config.terrain_height)
        top = config.base_height + relief

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
        relief = _terrain_relief_height(raw, transform, config.terrain_height)
        top = config.base_height + relief

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


def _union_meshes(meshes: list[trimesh.Trimesh], description: str) -> trimesh.Trimesh:
    try:
        combined = trimesh.boolean.union(
            meshes,
            engine="manifold",
            check_volume=True,
        )
    except Exception as exc:
        raise Gpx2StlError(f"Unable to combine {description}: {exc}") from exc
    if not isinstance(combined, trimesh.Trimesh) or combined.is_empty:
        raise Gpx2StlError(f"Unable to combine {description}.")
    combined.remove_unreferenced_vertices()
    return combined


def _font_properties(config: Config) -> tuple[FontProperties, Path]:
    if config.font_file is not None:
        path = config.font_file
        properties = FontProperties(fname=str(path), size=1.0)
    else:
        properties = FontProperties(family="DejaVu Sans", size=1.0)
        path = Path(font_manager.findfont(properties, fallback_to_default=False))
    return properties, path


def _glyph_geometry(path: TextPath) -> Polygon | MultiPolygon:
    contours = []
    for coordinates in path.to_polygons():
        if len(coordinates) >= 3:
            polygon = Polygon(coordinates)
            if polygon.is_valid and polygon.area > 1e-12:
                contours.append(polygon)
    if not contours:
        return MultiPolygon()
    geometry = contours[0]
    for contour in contours[1:]:
        geometry = geometry.symmetric_difference(contour)
    if not geometry.is_valid:
        geometry = shapely.make_valid(geometry)
    polygons = list(_polygon_parts(geometry))
    return MultiPolygon(polygons) if len(polygons) > 1 else polygons[0]


def _text_mesh(config: Config, inner_radius: float) -> trimesh.Trimesh:
    assert config.text is not None
    properties, font_path = _font_properties(config)
    try:
        font = FT2Font(font_path)
    except Exception as exc:
        raise Gpx2StlError(f"Unable to load font '{font_path}': {exc}") from exc
    missing = sorted(
        {
            character
            for character in config.text
            if not character.isspace() and font.get_char_index(ord(character)) == 0
        }
    )
    if missing:
        rendered = ", ".join(repr(character) for character in missing)
        raise Gpx2StlError(f"Font '{font_path}' does not contain glyphs for: {rendered}")

    outer_radius = config.max_size / 2.0
    outer_apothem = (
        outer_radius * math.sqrt(3.0) / 2.0
        if config.shape == "hex"
        else outer_radius
    )
    frame_width = outer_apothem - inner_radius
    margin = max(0.5, frame_width * 0.1)
    available_height = frame_width - 2.0 * margin
    if available_height <= 0:
        raise Gpx2StlError(
            "The text frame is too narrow; reduce --inner-size-percent."
        )

    text_to_path = TextToPath()
    advances = np.asarray(
        [
            text_to_path.get_text_width_height_descent(
                character, properties, ismath=False
            )[0]
            for character in config.text
        ],
        dtype=np.float64,
    )
    total_advance = float(np.sum(advances))
    unit_path = TextPath((0.0, 0.0), config.text, size=1.0, prop=properties)
    bounds = unit_path.get_extents()
    unit_height = float(bounds.height)
    if total_advance <= 0 or unit_height <= 0:
        raise Gpx2StlError("The requested text has no printable glyph outlines.")

    arc_radius = inner_radius + frame_width / 2.0
    maximum_arc = math.radians(140.0)
    font_size = min(
        available_height / unit_height,
        arc_radius * maximum_arc / total_advance,
    ) * 0.9
    if not math.isfinite(font_size) or font_size <= 0:
        raise Gpx2StlError("Unable to fit text in the available frame.")

    scaled_advances = advances * font_size
    cursor = -float(np.sum(scaled_advances)) / 2.0
    vertical_center = (float(bounds.ymin) + float(bounds.ymax)) * font_size / 2.0
    components: list[trimesh.Trimesh] = []
    for character, advance in zip(config.text, scaled_advances):
        midpoint = cursor + advance / 2.0
        cursor += advance
        if character.isspace():
            continue
        glyph_path = TextPath(
            (-advance / 2.0, -vertical_center),
            character,
            size=font_size,
            prop=properties,
        )
        glyph = _glyph_geometry(glyph_path)
        angle = math.pi / 2.0 - midpoint / arc_radius
        rotation = angle - math.pi / 2.0
        for polygon in _polygon_parts(glyph):
            try:
                mesh = trimesh.creation.extrude_polygon(
                    polygon,
                    height=config.text_height + 0.05,
                )
            except Exception as exc:
                raise Gpx2StlError(
                    f"Unable to create text geometry for {character!r}: {exc}"
                ) from exc
            transform = trimesh.transformations.rotation_matrix(
                rotation, [0.0, 0.0, 1.0]
            )
            transform[:2, 3] = [
                arc_radius * math.cos(angle),
                arc_radius * math.sin(angle),
            ]
            transform[2, 3] = config.base_height - 0.05
            mesh.apply_transform(transform)
            components.append(mesh)
    if not components:
        raise Gpx2StlError("The requested text has no printable glyph outlines.")
    text_mesh = trimesh.util.concatenate(components)
    text_mesh.remove_unreferenced_vertices()
    return text_mesh


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


def _polygon_parts(geometry: object) -> Iterable[Polygon]:
    if isinstance(geometry, Polygon):
        yield geometry
    elif isinstance(geometry, MultiPolygon):
        yield from geometry.geoms
    elif hasattr(geometry, "geoms"):
        for part in geometry.geoms:
            yield from _polygon_parts(part)


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
    if config.text is not None:
        if footprint.shape != "circle":
            raise Gpx2StlError("Text layout requires a circular terrain footprint.")
        inner_terrain, surface = _polar_circle(
            footprint, transform, raw_height, config
        )
        outer_base = _convex_prism(
            _model_outline(config.shape, config.max_size),
            config.base_height,
        )
        terrain = _union_meshes(
            [outer_base, inner_terrain],
            "the outer frame and terrain",
        )
        text_mesh = _text_mesh(
            config,
            footprint.radius * transform.scale,
        )
    elif footprint.shape == "square":
        terrain, surface = _structured_square(footprint, transform, raw_height, config)
        text_mesh = None
    elif footprint.shape == "circle":
        terrain, surface = _polar_circle(footprint, transform, raw_height, config)
        text_mesh = None
    else:
        terrain, surface = _polygon_terrain(footprint, transform, raw_height, config)
        text_mesh = None
    route_mesh = _route_mesh(route, transform, surface, config)
    meshes = [("terrain/base", terrain), ("route", route_mesh)]
    if text_mesh is not None:
        meshes.append(("text", text_mesh))
    for name, mesh in meshes:
        if not mesh.is_watertight:
            raise Gpx2StlError(f"Generated {name} mesh is not watertight.")
        if not np.all(np.isfinite(mesh.vertices)) or mesh.volume <= 0:
            raise Gpx2StlError(f"Generated {name} mesh is invalid.")
    return Geometry(terrain=terrain, route=route_mesh, text=text_mesh)
