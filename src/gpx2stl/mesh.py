from __future__ import annotations

import math
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from functools import partial
from itertools import pairwise
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
from shapely import affinity
from shapely.geometry import LineString, MultiPolygon, Point, Polygon

from gpx2stl.custom_base import CustomBase
from gpx2stl.city import CityBuilding
from gpx2stl.dem import DemSource, fill_missing
from gpx2stl.errors import Gpx2StlError
from gpx2stl.footprint import footprint_vertices
from gpx2stl.models import Config, Footprint, ModelTransform, ProjectedRoute

HeightFunction = Callable[[NDArray[np.float64]], NDArray[np.float64]]


@dataclass(frozen=True)
class Geometry:
    base: trimesh.Trimesh
    route: trimesh.Trimesh
    topography: trimesh.Trimesh | None = None
    text: trimesh.Trimesh | None = None
    buildings: trimesh.Trimesh | None = None
    water: trimesh.Trimesh | None = None
    roads: trimesh.Trimesh | None = None


@dataclass(frozen=True)
class _PerimeterTextLayout:
    polygons: tuple[Polygon, ...]
    font_size: float
    seam_gap: float
    seam_distance: float
    seam_point: tuple[float, float]
    perimeter: float
    glyph_distances: tuple[float, ...]


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


def _constant_surface(height: float) -> HeightFunction:
    def sample(query: NDArray[np.float64]) -> NDArray[np.float64]:
        return np.full(len(query), height, dtype=np.float64)

    return sample


def _triangulated_surface_sampler(
    points: NDArray[np.float64],
    heights: NDArray[np.float64],
    faces: list[tuple[int, int, int]],
) -> HeightFunction:
    face_array = np.asarray(faces, dtype=np.int64)
    triangles = points[face_array]
    triangle_heights = heights[face_array]
    tree = shapely.STRtree(shapely.polygons(triangles))
    nearest = NearestNDInterpolator(points, heights)

    def sample(query: NDArray[np.float64]) -> NDArray[np.float64]:
        result = np.full(len(query), np.nan, dtype=np.float64)
        matches = tree.query(shapely.points(query), predicate="intersects")
        if matches.shape[1] > 0:
            query_indices, first_matches = np.unique(
                matches[0], return_index=True
            )
            triangle_indices = matches[1, first_matches]
            selected = triangles[triangle_indices]
            selected_heights = triangle_heights[triangle_indices]
            x = query[query_indices, 0]
            y = query[query_indices, 1]
            x0, y0 = selected[:, 0, 0], selected[:, 0, 1]
            x1, y1 = selected[:, 1, 0], selected[:, 1, 1]
            x2, y2 = selected[:, 2, 0], selected[:, 2, 1]
            denominator = (y1 - y2) * (x0 - x2) + (x2 - x1) * (y0 - y2)
            first_weight = (
                (y1 - y2) * (x - x2) + (x2 - x1) * (y - y2)
            ) / denominator
            second_weight = (
                (y2 - y0) * (x - x2) + (x0 - x2) * (y - y2)
            ) / denominator
            third_weight = 1.0 - first_weight - second_weight
            result[query_indices] = (
                first_weight * selected_heights[:, 0]
                + second_weight * selected_heights[:, 1]
                + third_weight * selected_heights[:, 2]
            )
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
    bottom: float,
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
            np.column_stack((points, np.full(count, bottom))),
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


def _triangulated_polygon_points(
    polygon: Polygon,
    spacing: float,
) -> tuple[NDArray[np.float64], list[tuple[int, int, int]], list[list[int]]]:
    polygon = shapely.orient_polygons(polygon)
    minimum_x, minimum_y, maximum_x, maximum_y = polygon.bounds
    x_axis = np.arange(minimum_x, maximum_x + spacing * 0.5, spacing)
    y_axis = np.arange(minimum_y, maximum_y + spacing * 0.5, spacing)
    xx, yy = np.meshgrid(x_axis, y_axis)
    grid = np.column_stack((xx.ravel(), yy.ravel()))
    index_by_coordinate: dict[tuple[float, float], int] = {}
    points: list[tuple[float, float]] = []
    faces: list[tuple[int, int, int]] = []

    def index_for(point: NDArray[np.float64] | tuple[float, float]) -> int:
        key = (round(float(point[0]), 10), round(float(point[1]), 10))
        if key not in index_by_coordinate:
            index_by_coordinate[key] = len(points)
            points.append(key)
        return index_by_coordinate[key]

    def subdivide(
        start: NDArray[np.float64],
        end: NDArray[np.float64],
    ) -> NDArray[np.float64]:
        count = max(1, math.ceil(float(np.linalg.norm(end - start)) / spacing))
        fractions = np.arange(count, dtype=np.float64) / count
        return start + fractions[:, np.newaxis] * (end - start)

    base_triangles = shapely.constrained_delaunay_triangles(polygon)
    for base_triangle in shapely.get_parts(base_triangles):
        inside = shapely.contains_xy(base_triangle, grid[:, 0], grid[:, 1])
        corners = np.asarray(base_triangle.exterior.coords[:3], dtype=np.float64)
        edge_points = np.vstack(
            [
                subdivide(start, end)
                for start, end in zip(corners, np.roll(corners, -1, axis=0))
            ]
        )
        local = np.vstack(
            (
                edge_points,
                grid[inside],
            )
        )
        local = np.unique(local, axis=0)
        simplices = (
            np.array([[0, 1, 2]], dtype=np.int64)
            if len(local) == 3
            else Delaunay(local).simplices
        )
        for simplex in simplices:
            triangle = local[simplex]
            ab = triangle[1] - triangle[0]
            ac = triangle[2] - triangle[0]
            cross = ab[0] * ac[1] - ab[1] * ac[0]
            if abs(float(cross)) <= spacing * spacing * 1e-8:
                continue
            indices = [index_for(point) for point in triangle]
            if cross < 0:
                indices[1], indices[2] = indices[2], indices[1]
            faces.append(tuple(indices))

    rings = [polygon.exterior, *polygon.interiors]
    ring_indices = []
    for ring in rings:
        coordinates = np.asarray(ring.coords, dtype=np.float64)
        ring_points = np.vstack(
            [subdivide(start, end) for start, end in pairwise(coordinates)]
        )
        ring_indices.append([index_for(point) for point in ring_points])
    return np.asarray(points, dtype=np.float64), faces, ring_indices


def _custom_terrain(
    custom_base: CustomBase,
    raw_height: Callable[[NDArray[np.float64]], NDArray[np.float64]] | None,
    config: Config,
) -> tuple[trimesh.Trimesh, HeightFunction]:
    minimum_x, minimum_y, maximum_x, maximum_y = custom_base.terrain_polygon.bounds
    maximum_span = max(maximum_x - minimum_x, maximum_y - minimum_y)
    resolution = _model_resolution(maximum_span)
    spacing = maximum_span / (resolution - 1)
    points, top_faces, rings = _triangulated_polygon_points(
        custom_base.terrain_polygon,
        spacing,
    )
    if raw_height is None:
        top = np.full(len(points), custom_base.top_z, dtype=np.float64)
    else:
        raw = np.asarray(raw_height(points), dtype=np.float64)
        missing = ~np.isfinite(raw)
        if np.all(missing):
            raise Gpx2StlError("The selected DEM contains no usable elevation samples.")
        if np.any(missing):
            nearest = NearestNDInterpolator(points[~missing], raw[~missing])
            raw[missing] = nearest(points[missing])
        top = custom_base.top_z + _terrain_relief_height(
            raw,
            custom_base.transform,
            config.terrain_height,
        )
    bottom = custom_base.top_z - custom_base.overlap_depth
    count = len(points)
    vertices = np.vstack(
        (
            np.column_stack((points, top)),
            np.column_stack((points, np.full(count, bottom))),
        )
    )
    faces: list[tuple[int, int, int]] = []
    for a, b, c in top_faces:
        faces.extend(((a, b, c), (count + a, count + c, count + b)))
    for ring in rings:
        _add_side_faces(faces, ring, count)
    patch = trimesh.Trimesh(vertices=vertices, faces=np.asarray(faces), process=True)
    return patch, _triangulated_surface_sampler(points, top, top_faces)


def _structured_square(
    footprint: Footprint,
    transform: ModelTransform,
    raw_height: Callable[[NDArray[np.float64]], NDArray[np.float64]] | None,
    config: Config,
    bottom: float,
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
            np.column_stack((points, np.full(count, bottom, dtype=np.float64))),
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
    bottom: float,
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
            np.column_stack((xy, np.full(count, bottom, dtype=np.float64))),
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


def _font_properties(config: Config) -> tuple[FontProperties, Path]:
    if config.font_file is not None:
        path = config.font_file
        properties = FontProperties(fname=str(path), size=1.0)
    else:
        requested = FontProperties(
            family=config.font_family,
            weight=config.font_weight,
            style=config.font_style,
            size=1.0,
        )
        try:
            path = Path(font_manager.findfont(requested, fallback_to_default=False))
        except ValueError as exc:
            variant = f"{config.font_weight} {config.font_style}"
            raise Gpx2StlError(
                f"Unable to find installed font family '{config.font_family}' "
                f"with {variant} style."
            ) from exc
        properties = FontProperties(fname=str(path), size=1.0)
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


def _clockwise_centerline(polygon: Polygon) -> LineString:
    coordinates = list(polygon.exterior.coords)
    line = LineString(coordinates)
    if shapely.is_ccw(line):
        line = LineString(coordinates[::-1])
    return line


def _perimeter_text_layout(
    config: Config,
    *,
    text_area: object,
    centerline_polygon: Polygon,
    outer_polygon: Polygon,
    available_height: float,
    description: str,
    fit_guidance: str,
) -> _PerimeterTextLayout:
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

    if available_height <= 0:
        raise Gpx2StlError(
            f"The {description} is too narrow for text; reduce --text-margin "
            f"or {fit_guidance}."
        )

    text = config.text
    first = 0
    while first < len(text) and text[first].isspace():
        first += 1
    last = len(text)
    while last > first and text[last - 1].isspace():
        last -= 1
    core_text = text[first:last]
    if not core_text:
        raise Gpx2StlError("The requested text has no printable glyph outlines.")
    edge_whitespace = text[:first] + text[last:]

    centerline = _clockwise_centerline(centerline_polygon)
    perimeter = centerline.length
    if perimeter <= 1e-6:
        raise Gpx2StlError(f"The {description} is too small for text.")
    bottom_center = Point(outer_polygon.centroid.x, outer_polygon.bounds[1])
    seam_distance = centerline.project(bottom_center)

    text_to_path = TextToPath()
    advances = np.asarray(
        [
            text_to_path.get_text_width_height_descent(
                character, properties, ismath=False
            )[0]
            for character in core_text
        ],
        dtype=np.float64,
    )
    total_advance = float(np.sum(advances))
    space_advance = text_to_path.get_text_width_height_descent(
        " ", properties, ismath=False
    )[0]
    space_advance = max(space_advance, 0.25)
    edge_advance = sum(
        text_to_path.get_text_width_height_descent(
            character, properties, ismath=False
        )[0]
        for character in edge_whitespace
    )
    seam_advance = 8.0 * space_advance + edge_advance
    unit_path = TextPath((0.0, 0.0), core_text, size=1.0, prop=properties)
    bounds = unit_path.get_extents()
    unit_height = float(bounds.height)
    if total_advance <= 0 or unit_height <= 0:
        raise Gpx2StlError("The requested text has no printable glyph outlines.")
    available_perimeter = perimeter - config.text_end_gap
    if available_perimeter <= 0:
        raise Gpx2StlError(
            f"The requested --text-end-gap is too large for the {description}."
        )
    maximum_size = min(
        available_height * 0.98 / unit_height,
        available_perimeter * 0.98 / (total_advance + seam_advance),
    )
    tangent_delta = max(
        1e-5,
        min(available_height / 20.0, perimeter / 10000.0),
    )
    containment_area = shapely.buffer(text_area, 1e-7)

    def place(font_size: float) -> _PerimeterTextLayout | None:
        scaled_advances = advances * font_size
        seam_gap = seam_advance * font_size + config.text_end_gap
        remaining = perimeter - seam_gap - float(np.sum(scaled_advances))
        if remaining < 0:
            return None
        alignment_offset = {
            "left": 0.0,
            "center": remaining / 2.0,
            "right": remaining,
        }[config.text_align]
        cursor = seam_distance + seam_gap / 2.0 + alignment_offset
        vertical_center = (float(bounds.ymin) + float(bounds.ymax)) * font_size / 2.0
        polygons: list[Polygon] = []
        glyph_distances: list[float] = []
        for character, advance in zip(core_text, scaled_advances):
            distance = (cursor + advance / 2.0) % perimeter
            cursor += advance
            if character.isspace():
                continue
            location = centerline.interpolate(distance)
            before = centerline.interpolate((distance - tangent_delta) % perimeter)
            after = centerline.interpolate((distance + tangent_delta) % perimeter)
            tangent_x = after.x - before.x
            tangent_y = after.y - before.y
            if math.hypot(tangent_x, tangent_y) <= 1e-12:
                return None
            glyph = _glyph_geometry(
                TextPath(
                    (-advance / 2.0, -vertical_center),
                    character,
                    size=font_size,
                    prop=properties,
                )
            )
            glyph = affinity.rotate(
                glyph,
                math.degrees(math.atan2(tangent_y, tangent_x)),
                origin=(0.0, 0.0),
            )
            glyph = affinity.translate(glyph, xoff=location.x, yoff=location.y)
            glyph_parts = list(_polygon_parts(glyph))
            if not glyph_parts or any(
                not containment_area.covers(part) for part in glyph_parts
            ):
                return None
            polygons.extend(glyph_parts)
            glyph_distances.append(distance)
        if not polygons:
            return None
        return _PerimeterTextLayout(
            tuple(polygons),
            font_size,
            seam_gap,
            seam_distance,
            (
                float(centerline.interpolate(seam_distance).x),
                float(centerline.interpolate(seam_distance).y),
            ),
            perimeter,
            tuple(glyph_distances),
        )

    if config.font_size is not None:
        requested_size = config.font_size / unit_height
        placed = place(requested_size)
        if placed is None:
            raise Gpx2StlError(
                f"The requested --font-size cannot fit the {description}; reduce "
                "--font-size or --text-margin, shorten the text, increase the "
                f"frame width ({fit_guidance}), or use a narrower font."
            )
        return placed

    placed: _PerimeterTextLayout | None = None
    lower = 0.0
    upper = maximum_size
    for _ in range(40):
        candidate_size = (lower + upper) / 2.0
        candidate = place(candidate_size)
        if candidate is None:
            upper = candidate_size
        else:
            lower = candidate_size
            placed = candidate
    if placed is None or lower < 0.05:
        raise Gpx2StlError(
            f"The {description} cannot fit the requested text and bottom seam; "
            "reduce --text-margin or --text-end-gap, shorten the text, increase "
            f"the frame width ({fit_guidance}), or use a narrower font."
        )
    return placed


def _extrude_text(
    polygons: Iterable[Polygon],
    *,
    height: float,
    bottom: float,
    description: str,
) -> trimesh.Trimesh:
    components: list[trimesh.Trimesh] = []
    for polygon in polygons:
        try:
            mesh = trimesh.creation.extrude_polygon(polygon, height=height)
        except Exception as exc:
            raise Gpx2StlError(f"Unable to create {description}: {exc}") from exc
        mesh.apply_translation((0.0, 0.0, bottom))
        components.append(mesh)
    if not components:
        raise Gpx2StlError("The requested text has no printable glyph outlines.")
    text_mesh = trimesh.util.concatenate(components)
    text_mesh.remove_unreferenced_vertices()
    return text_mesh


def _generated_text_layout(config: Config, inner_radius: float) -> _PerimeterTextLayout:
    outer_polygon = shapely.orient_polygons(
        Polygon(_model_outline(config.shape, config.max_size))
    )
    inner_polygon = (
        Point(0.0, 0.0).buffer(inner_radius, quad_segs=64)
        if config.shape == "circle"
        else Polygon(_model_outline(config.shape, inner_radius * 2.0))
    )
    frame = outer_polygon.difference(inner_polygon)
    outer_apothem = (
        config.max_size * math.sqrt(3.0) / 4.0
        if config.shape == "hex"
        else config.max_size / 2.0
    )
    inner_apothem = (
        inner_radius * math.sqrt(3.0) / 2.0
        if config.shape == "hex"
        else inner_radius
    )
    frame_width = outer_apothem - inner_apothem
    if frame_width <= 0.0:
        raise Gpx2StlError(
            "The terrain inset leaves no text band inside the generated frame; "
            "increase --text-boundary-percent."
        )
    # Keep automatic clearance from consuming narrow bands, especially on hex frames.
    margin = (
        config.text_margin
        if config.text_margin is not None
        else min(max(0.5, frame_width * 0.1), frame_width * 0.25)
    )
    available_height = frame_width - 2.0 * margin
    text_area = (
        frame.buffer(-margin, join_style="mitre")
        if margin > 0.0
        else frame
    )
    if text_area.is_empty:
        raise Gpx2StlError(
            "The text frame cannot fit the requested --text-margin; reduce "
            "--text-margin or increase --text-boundary-percent."
        )
    centerline_geometry = outer_polygon.buffer(
        -frame_width / 2.0,
        join_style="mitre",
    )
    if not isinstance(centerline_geometry, Polygon) or centerline_geometry.is_empty:
        raise Gpx2StlError("The generated frame cannot form one continuous text path.")
    return _perimeter_text_layout(
        config,
        text_area=text_area,
        centerline_polygon=centerline_geometry,
        outer_polygon=outer_polygon,
        available_height=available_height,
        description="generated text frame",
        fit_guidance="increase --text-boundary-percent",
    )


def _text_mesh(config: Config, inner_radius: float) -> trimesh.Trimesh:
    layout = _generated_text_layout(config, inner_radius)
    if config.text_mode == "embedded":
        return _extrude_text(
            layout.polygons,
            height=config.text_depth,
            bottom=config.base_height - config.text_depth,
            description="embedded text geometry",
        )
    return _extrude_text(
        layout.polygons,
        height=config.text_height + 0.05,
        bottom=config.base_height - 0.05,
        description="text geometry",
    )


def _custom_text_layout(
    config: Config,
    custom_base: CustomBase,
) -> _PerimeterTextLayout:
    assert config.text is not None
    border = custom_base.top_polygon.difference(custom_base.terrain_polygon)
    margin = config.text_margin or 0.0
    text_area = (
        border.buffer(-margin, join_style="mitre")
        if margin > 0.0
        else border
    )
    if text_area.is_empty:
        raise Gpx2StlError(
            "The custom base margin cannot fit the requested --text-margin; "
            "increase --text-boundary-percent or reduce --text-margin."
        )
    if custom_base.inset_distance <= 1e-6:
        raise Gpx2StlError(
            "The custom base has no flat top border for text; increase "
            "--text-boundary-percent."
        )
    centerline_geometry = custom_base.top_polygon.buffer(
        -custom_base.inset_distance / 2.0,
        join_style="mitre",
    )
    centerline_parts = [
        geometry
        for geometry in shapely.get_parts(centerline_geometry)
        if isinstance(geometry, Polygon) and geometry.area > 1e-9
    ]
    if len(centerline_parts) != 1:
        raise Gpx2StlError(
            "The custom base margin cannot form one continuous text path."
        )
    available_width = custom_base.inset_distance - 2.0 * margin
    if available_width <= 0.0:
        raise Gpx2StlError(
            "The custom base margin cannot fit the requested --text-margin; "
            "increase --text-boundary-percent or reduce --text-margin."
        )
    return _perimeter_text_layout(
        config,
        text_area=text_area,
        centerline_polygon=centerline_parts[0],
        outer_polygon=custom_base.top_polygon,
        available_height=available_width,
        description="custom base margin",
        fit_guidance="increase --text-boundary-percent",
    )


def _custom_text_polygons(
    config: Config,
    custom_base: CustomBase,
) -> list[Polygon]:
    return list(_custom_text_layout(config, custom_base).polygons)


def _custom_text_mesh(config: Config, custom_base: CustomBase) -> trimesh.Trimesh:
    if config.text_mode == "embedded":
        thickness = custom_base.top_z - float(custom_base.mesh.bounds[0, 2])
        if config.text_depth >= thickness:
            raise Gpx2StlError(
                "--text-depth must be smaller than the custom base thickness."
            )
        return _extrude_text(
            _custom_text_polygons(config, custom_base),
            height=config.text_depth,
            bottom=custom_base.top_z - config.text_depth,
            description="custom-base embedded text",
        )
    return _extrude_text(
        _custom_text_polygons(config, custom_base),
        height=config.text_height + custom_base.overlap_depth,
        bottom=custom_base.top_z - custom_base.overlap_depth,
        description="custom-base text",
    )


def _subtract_text_cavity(
    base: trimesh.Trimesh,
    text: trimesh.Trimesh,
    top_z: float,
) -> trimesh.Trimesh:
    cavity = text.copy()
    top = float(cavity.bounds[1, 2])
    if not math.isclose(top, top_z, abs_tol=1e-7):
        raise Gpx2StlError("Embedded text does not end at the base surface.")
    cavity.vertices[cavity.vertices[:, 2] >= top - 1e-7, 2] = top_z + 0.05
    try:
        result = trimesh.boolean.difference(
            [base, cavity],
            engine="manifold",
            check_volume=True,
        )
    except Exception as exc:
        raise Gpx2StlError(f"Unable to cut the embedded text cavity: {exc}") from exc
    if not isinstance(result, trimesh.Trimesh) or result.is_empty:
        raise Gpx2StlError("Unable to cut the embedded text cavity from the base.")
    result.remove_unreferenced_vertices()
    return result


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
    normalized = shapely.set_precision(polygon, grid_size=1e-6)
    parts = tuple(_polygon_parts(normalized))
    if not parts:
        raise Gpx2StlError("Variable-height extrusion has no printable area.")
    components = [
        _variable_extrusion_part(part, bottom_height, top_height)
        for part in parts
    ]
    result = (
        components[0]
        if len(components) == 1
        else trimesh.util.concatenate(components)
    )
    result.remove_unreferenced_vertices()
    return result


def _variable_extrusion_part(
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
        raise Gpx2StlError("Variable-height extrusion has non-positive thickness.")
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
    base_height: float | None = None,
    clamp_bottom_to_zero: bool = True,
    overlap_depth: float = 0.05,
    city_cavity_top: float | None = None,
) -> trimesh.Trimesh:
    components: list[trimesh.Trimesh] = []
    route_base_height = config.base_height if base_height is None else base_height
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

        if config.mode == "city":

            def top_height(points: NDArray[np.float64]) -> NDArray[np.float64]:
                if city_cavity_top is not None:
                    return np.full(len(points), city_cavity_top, dtype=np.float64)
                return surface_height(points)

        elif config.topo:

            def top_height(points: NDArray[np.float64]) -> NDArray[np.float64]:
                return surface_height(points) + config.route_height

        else:
            top_height = partial(
                _no_topo_route_height,
                line=line,
                cumulative=cumulative,
                elevations=elevations,
                base_height=route_base_height,
                elevation_shift=elevation_shift,
            )

        def bottom_height(points: NDArray[np.float64]) -> NDArray[np.float64]:
            if config.mode == "city":
                bottom = surface_height(points) - config.route_depth
                return np.maximum(0.0, bottom) if clamp_bottom_to_zero else bottom
            if config.topo:
                bottom = surface_height(points) - overlap_depth
                return np.maximum(0.0, bottom) if clamp_bottom_to_zero else bottom
            bottom = route_base_height - overlap_depth
            if clamp_bottom_to_zero:
                bottom = max(0.0, bottom)
            return np.full(len(points), bottom)

        for polygon in _polygon_parts(buffered):
            components.append(_variable_extrusion(polygon, bottom_height, top_height))
    if not components:
        raise Gpx2StlError("No printable route geometry could be generated.")
    route_mesh = trimesh.util.concatenate(components)
    route_mesh.remove_unreferenced_vertices()
    return route_mesh


def _subtract_route_cavity(
    target: trimesh.Trimesh,
    cavity: trimesh.Trimesh,
    description: str,
) -> trimesh.Trimesh:
    if (
        cavity.bounds[0, 2] >= target.bounds[1, 2]
        or cavity.bounds[1, 2] <= target.bounds[0, 2]
    ):
        return target
    try:
        result = trimesh.boolean.difference(
            [target, cavity],
            engine="manifold",
            check_volume=True,
        )
    except Exception as exc:
        raise Gpx2StlError(
            f"Unable to cut the embedded route cavity from {description}: {exc}"
        ) from exc
    if not isinstance(result, trimesh.Trimesh) or result.is_empty:
        raise Gpx2StlError(
            f"Unable to cut the embedded route cavity from {description}."
        )
    result.remove_unreferenced_vertices()
    return result


def _subtract_optional_inlay(
    target: trimesh.Trimesh,
    cavity: trimesh.Trimesh,
    description: str,
) -> trimesh.Trimesh | None:
    if (
        cavity.bounds[0, 2] >= target.bounds[1, 2]
        or cavity.bounds[1, 2] <= target.bounds[0, 2]
    ):
        return target
    try:
        result = trimesh.boolean.difference(
            [target, cavity],
            engine="manifold",
            check_volume=True,
        )
    except Exception as exc:
        raise Gpx2StlError(
            f"Unable to cut the higher-priority inlay from {description}: {exc}"
        ) from exc
    if isinstance(result, trimesh.Trimesh) and result.is_empty:
        return None
    if not isinstance(result, trimesh.Trimesh):
        raise Gpx2StlError(
            f"Unable to cut the higher-priority inlay from {description}."
        )
    result.remove_unreferenced_vertices()
    return result


def _building_mesh(
    buildings: tuple[CityBuilding, ...],
    transform: ModelTransform,
    surface_height: HeightFunction,
    config: Config,
    printable_clip: Polygon | None = None,
) -> trimesh.Trimesh | None:
    components: list[trimesh.Trimesh] = []
    candidates: list[tuple[Polygon, float]] = []

    def append_component(polygon: Polygon, top_z: float) -> None:
        def bottom_height(points: NDArray[np.float64]) -> NDArray[np.float64]:
            return surface_height(points) - 0.05

        def top_height(points: NDArray[np.float64]) -> NDArray[np.float64]:
            return np.full(len(points), top_z, dtype=np.float64)

        components.append(
            _variable_extrusion(polygon, bottom_height, top_height)
        )

    for building in buildings:
        projected_polygon = building.polygon
        height_m = building.height_m
        model_geometry = _compensate_structure_footprint(
            shapely.transform(projected_polygon, transform.to_model),
            config.nozzle_diameter,
            printable_clip,
        )
        model_geometry = shapely.orient_polygons(
            model_geometry, exterior_cw=True
        )
        model_height = height_m * transform.scale * config.building_height_scale
        for polygon in _polygon_parts(model_geometry):
            if polygon.is_empty or polygon.area <= 1e-9:
                continue
            boundary_points = np.asarray(
                [
                    (float(x), float(y))
                    for ring in (polygon.exterior, *polygon.interiors)
                    for x, y in list(ring.coords)[:-1]
                ],
                dtype=np.float64,
            )
            terrain_heights = surface_height(boundary_points)
            top_z = float(np.max(terrain_heights)) + model_height
            if config.nozzle_diameter is None:
                append_component(polygon, top_z)
            else:
                candidates.append((polygon, top_z))
    if candidates:
        for polygon, top_z in _resolve_structure_overlaps(candidates):
            append_component(polygon, top_z)
    if not components:
        return None
    result = trimesh.util.concatenate(components)
    result.remove_unreferenced_vertices()
    return result


def _compensate_structure_footprint(
    geometry: Polygon | MultiPolygon,
    nozzle_diameter: float | None,
    printable_clip: Polygon | None,
) -> Polygon | MultiPolygon:
    if nozzle_diameter is None:
        return geometry
    if printable_clip is None:
        raise Gpx2StlError(
            "No printable terrain boundary is available for nozzle compensation."
        )
    expanded = geometry.buffer(
        nozzle_diameter / 2.0,
        join_style="mitre",
        mitre_limit=2.0,
    )
    return (
        expanded
        if printable_clip.covers(expanded)
        else expanded.intersection(printable_clip)
    )


def _resolve_structure_overlaps(
    candidates: list[tuple[Polygon, float]],
) -> tuple[tuple[Polygon, float], ...]:
    polygons = [polygon for polygon, _ in candidates]
    order = sorted(
        range(len(candidates)),
        key=lambda index: (-candidates[index][1], index),
    )
    rank = np.empty(len(order), dtype=np.int64)
    rank[order] = np.arange(len(order), dtype=np.int64)
    tree = shapely.STRtree(polygons)
    resolved: list[tuple[Polygon, float]] = []
    for index in order:
        polygon, top_z = candidates[index]
        blocker_indices = [
            int(other)
            for other in tree.query(polygon, predicate="intersects")
            if rank[int(other)] < rank[index]
        ]
        visible = (
            polygon.difference(
                shapely.union_all([polygons[other] for other in blocker_indices])
            )
            if blocker_indices
            else polygon
        )
        for part in _polygon_parts(visible):
            normalized = shapely.set_precision(part, grid_size=1e-6)
            for printable in _polygon_parts(normalized):
                if not printable.is_empty and printable.area > 1e-9:
                    resolved.append((printable, top_z))
    return tuple(resolved)


def _structure_printable_clip(
    footprint: Footprint,
    transform: ModelTransform,
    custom_base: CustomBase | None,
) -> Polygon:
    if custom_base is not None:
        return custom_base.terrain_polygon
    if footprint.shape == "circle":
        center = transform.to_model(
            np.asarray([footprint.center], dtype=np.float64)
        )[0]
        return Point(center).buffer(
            footprint.radius * transform.scale,
            quad_segs=64,
        )
    return Polygon(transform.to_model(footprint_vertices(footprint)))


def _water_mesh(
    water: tuple[Polygon, ...],
    transform: ModelTransform,
    surface_height: HeightFunction,
    depth: float,
    cavity_top: float | None = None,
) -> trimesh.Trimesh | None:
    if not water:
        return None
    model_water = shapely.unary_union(
        [
            shapely.transform(polygon, transform.to_model)
            for polygon in water
        ]
    )
    if model_water.is_empty:
        return None
    model_water = shapely.orient_polygons(model_water, exterior_cw=True)
    components: list[trimesh.Trimesh] = []
    for polygon in _polygon_parts(model_water):
        if polygon.is_empty or polygon.area <= 1e-9:
            continue

        def bottom_height(points: NDArray[np.float64]) -> NDArray[np.float64]:
            return np.maximum(0.0, surface_height(points) - depth)

        def top_height(points: NDArray[np.float64]) -> NDArray[np.float64]:
            if cavity_top is not None:
                return np.full(len(points), cavity_top, dtype=np.float64)
            return surface_height(points)

        components.append(
            _variable_extrusion(polygon, bottom_height, top_height)
        )
    if not components:
        return None
    result = trimesh.util.concatenate(components)
    result.remove_unreferenced_vertices()
    return result


def _road_mesh(
    roads: tuple[LineString, ...],
    route: ProjectedRoute,
    transform: ModelTransform,
    surface_height: HeightFunction,
    width: float,
    depth: float,
    printable_clip: Polygon,
    cavity_top: float | None = None,
) -> trimesh.Trimesh | None:
    if not roads:
        return None
    model_roads = shapely.unary_union(
        [
            shapely.transform(road, transform.to_model).buffer(
                width / 2.0,
                cap_style="round",
                join_style="round",
                quad_segs=4,
            )
            for road in roads
        ]
    ).intersection(printable_clip)
    model_route = shapely.unary_union(
        [
            LineString(transform.to_model(path)).buffer(
                width / 2.0,
                cap_style="round",
                join_style="round",
                quad_segs=4,
            )
            for path in route.paths
        ]
    )
    model_roads = shapely.orient_polygons(
        model_roads.difference(model_route),
        exterior_cw=True,
    )
    components: list[trimesh.Trimesh] = []
    for polygon in _polygon_parts(model_roads):
        if polygon.is_empty or polygon.area <= 1e-9:
            continue

        def bottom_height(points: NDArray[np.float64]) -> NDArray[np.float64]:
            return np.maximum(0.0, surface_height(points) - depth)

        def top_height(points: NDArray[np.float64]) -> NDArray[np.float64]:
            if cavity_top is not None:
                return np.full(len(points), cavity_top, dtype=np.float64)
            return surface_height(points)

        components.append(
            _variable_extrusion(polygon, bottom_height, top_height)
        )
    if not components:
        return None
    result = trimesh.util.concatenate(components)
    result.remove_unreferenced_vertices()
    return result


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
    custom_base: CustomBase | None = None,
    buildings: tuple[CityBuilding, ...] = (),
    roads: tuple[LineString, ...] = (),
    water: tuple[Polygon, ...] = (),
) -> Geometry:
    raw_height = None if dem is None else _raw_height_function(dem, route, transform)
    if custom_base is not None:
        custom_thickness = (
            custom_base.top_z - float(custom_base.mesh.bounds[0, 2])
        )
        if config.mode == "city" and config.route_depth >= custom_thickness:
            raise Gpx2StlError(
                "--route-depth must be smaller than the custom base thickness."
            )
        if config.mode == "city" and config.water_depth >= custom_thickness:
            raise Gpx2StlError(
                "--water-depth must be smaller than the custom base thickness."
            )
        base = custom_base.mesh.copy()
        if config.topo:
            topography, surface = _custom_terrain(custom_base, raw_height, config)
        else:
            topography = None
            surface = _constant_surface(custom_base.top_z)
        text_mesh = (
            _custom_text_mesh(config, custom_base)
            if config.text is not None
            else None
        )
        if text_mesh is not None and config.text_mode == "embedded":
            base = _subtract_text_cavity(base, text_mesh, custom_base.top_z)
    else:
        base = _convex_prism(
            _model_outline(config.shape, config.max_size),
            config.base_height,
        )
        overlap_depth = min(0.05, config.base_height / 2.0)
        topography_bottom = config.base_height - overlap_depth
        if config.topo:
            if footprint.shape == "square":
                topography, surface = _structured_square(
                    footprint,
                    transform,
                    raw_height,
                    config,
                    topography_bottom,
                )
            elif footprint.shape == "circle":
                topography, surface = _polar_circle(
                    footprint,
                    transform,
                    raw_height,
                    config,
                    topography_bottom,
                )
            else:
                topography, surface = _polygon_terrain(
                    footprint,
                    transform,
                    raw_height,
                    config,
                    topography_bottom,
                )
        else:
            topography = None
            surface = _constant_surface(config.base_height)
        text_mesh = (
            _text_mesh(
                config,
                footprint.radius * transform.scale,
            )
            if config.text is not None
            else None
        )
        if text_mesh is not None and config.text_mode == "embedded":
            base = _subtract_text_cavity(base, text_mesh, config.base_height)
    route_mesh = _route_mesh(
        route,
        transform,
        surface,
        config,
        custom_base.top_z if custom_base is not None else None,
        custom_base is None,
        custom_base.overlap_depth if custom_base is not None else 0.05,
    )
    water_mesh = None
    road_mesh = None
    if config.mode == "city":
        cavity_top = max(
            float(base.bounds[1, 2]),
            float(topography.bounds[1, 2]) if topography is not None else 0.0,
        ) + 1.0
        printable_clip = _structure_printable_clip(
            footprint,
            transform,
            custom_base,
        )
        water_mesh = _water_mesh(
            water,
            transform,
            surface,
            config.water_depth,
        )
        water_cavity = _water_mesh(
            water,
            transform,
            surface,
            config.water_depth,
            cavity_top,
        )
        if water_cavity is not None:
            base = _subtract_route_cavity(base, water_cavity, "the base")
            if topography is not None:
                topography = _subtract_route_cavity(
                    topography, water_cavity, "the topography"
                )
        road_mesh = _road_mesh(
            roads,
            route,
            transform,
            surface,
            config.route_width,
            config.route_depth,
            printable_clip,
        )
        road_cavity = _road_mesh(
            roads,
            route,
            transform,
            surface,
            config.route_width,
            config.route_depth,
            printable_clip,
            cavity_top,
        )
        if road_cavity is not None:
            base = _subtract_route_cavity(base, road_cavity, "the base")
            if topography is not None:
                topography = _subtract_route_cavity(
                    topography, road_cavity, "the topography"
                )
            if water_mesh is not None:
                water_mesh = _subtract_optional_inlay(
                    water_mesh, road_cavity, "the water inlay"
                )
        cavity = _route_mesh(
            route,
            transform,
            surface,
            config,
            custom_base.top_z if custom_base is not None else None,
            custom_base is None,
            custom_base.overlap_depth if custom_base is not None else 0.05,
            city_cavity_top=cavity_top,
        )
        base = _subtract_route_cavity(base, cavity, "the base")
        if topography is not None:
            topography = _subtract_route_cavity(
                topography, cavity, "the topography"
            )
        if water_mesh is not None:
            water_mesh = _subtract_optional_inlay(
                water_mesh, cavity, "the water inlay"
            )
    building_mesh = (
        _building_mesh(
            buildings,
            transform,
            surface,
            config,
            (
                _structure_printable_clip(footprint, transform, custom_base)
                if config.nozzle_diameter is not None
                else None
            ),
        )
        if config.mode == "city"
        else None
    )
    meshes = [("base", base), ("route", route_mesh)]
    if topography is not None:
        meshes.append(("topography", topography))
    if text_mesh is not None:
        meshes.append(("text", text_mesh))
    if building_mesh is not None:
        meshes.append(("buildings", building_mesh))
    if water_mesh is not None:
        meshes.append(("water", water_mesh))
    if road_mesh is not None:
        meshes.append(("roads", road_mesh))
    for name, mesh in meshes:
        if not mesh.is_watertight:
            raise Gpx2StlError(f"Generated {name} mesh is not watertight.")
        if not np.all(np.isfinite(mesh.vertices)) or mesh.volume <= 0:
            raise Gpx2StlError(f"Generated {name} mesh is invalid.")
    return Geometry(
        base=base,
        route=route_mesh,
        topography=topography,
        text=text_mesh,
        buildings=building_mesh,
        water=water_mesh,
        roads=road_mesh,
    )
