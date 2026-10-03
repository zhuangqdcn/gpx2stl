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
from matplotlib import tri as matplotlib_tri
from matplotlib.font_manager import FontProperties
from matplotlib.ft2font import FT2Font
from matplotlib.textpath import TextPath, TextToPath
from numpy.typing import NDArray
from scipy.interpolate import LinearNDInterpolator, NearestNDInterpolator
from scipy.spatial import Delaunay
from shapely import affinity
from shapely.geometry import LineString, MultiPolygon, Point, Polygon

from gpx2stl.custom_base import CustomBase
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


def _triangulated_surface_sampler(
    points: NDArray[np.float64],
    heights: NDArray[np.float64],
    faces: list[tuple[int, int, int]],
) -> HeightFunction:
    triangulation = matplotlib_tri.Triangulation(
        points[:, 0],
        points[:, 1],
        triangles=np.asarray(faces, dtype=np.int64),
    )
    linear = matplotlib_tri.LinearTriInterpolator(triangulation, heights)
    nearest = NearestNDInterpolator(points, heights)

    def sample(query: NDArray[np.float64]) -> NDArray[np.float64]:
        interpolated = linear(query[:, 0], query[:, 1])
        result = np.asarray(
            np.ma.filled(interpolated, np.nan),
            dtype=np.float64,
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
    terrain = _union_meshes(
        [custom_base.mesh.copy(), patch],
        "the custom STL base and terrain",
    )
    return terrain, _triangulated_surface_sampler(points, top, top_faces)


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
    margin = (
        config.text_margin
        if config.text_margin is not None
        else max(0.5, frame_width * 0.1)
    )
    available_height = frame_width - 2.0 * margin
    if available_height <= 0:
        raise Gpx2StlError(
            "The text frame is too narrow; reduce --inner-size-percent or "
            "--text-margin."
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


def _custom_text_polygons(
    config: Config,
    custom_base: CustomBase,
) -> list[Polygon]:
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
            "increase --boundary-percent or reduce --text-margin."
        )
    if custom_base.inset_distance <= 1e-6:
        raise Gpx2StlError(
            "The custom base has no flat top border for text; increase "
            "--boundary-percent."
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
    centerline = LineString(centerline_parts[0].exterior.coords)
    perimeter = centerline.length
    if perimeter <= 1e-6:
        raise Gpx2StlError("The custom base margin is too small for text.")

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

    text_to_path = TextToPath()
    advances = np.asarray(
        [
            text_to_path.get_text_width_height_descent(
                character,
                properties,
                ismath=False,
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
    available_width = custom_base.inset_distance - 2.0 * margin
    if available_width <= 0.0:
        raise Gpx2StlError(
            "The custom base margin cannot fit the requested --text-margin; "
            "increase --boundary-percent or reduce --text-margin."
        )
    maximum_size = min(
        available_width * 0.98 / unit_height,
        perimeter * 0.98 / total_advance,
    )

    top_center = Point(
        custom_base.top_polygon.centroid.x,
        custom_base.top_polygon.bounds[3],
    )
    start_distance = centerline.project(top_center)
    slot_length = perimeter / len(config.text)
    phase_offsets = [0.0]
    for step in range(1, 9):
        offset = slot_length * step / 16.0
        phase_offsets.extend((offset, -offset))
    tangent_delta = max(
        1e-5, min(custom_base.inset_distance / 20.0, perimeter / 10000.0)
    )
    containment_border = text_area.buffer(1e-7)

    def layout(font_size: float, phase: float) -> list[Polygon] | None:
        scaled_advances = advances * font_size
        tracking = (perimeter - float(np.sum(scaled_advances))) / len(config.text)
        if tracking < 0:
            return None
        cursor = -scaled_advances[0] / 2.0
        vertical_center = (float(bounds.ymin) + float(bounds.ymax)) * font_size / 2.0
        candidates: list[Polygon] = []
        for character, advance in zip(config.text, scaled_advances):
            midpoint = cursor + advance / 2.0
            cursor += advance + tracking
            if character.isspace():
                continue
            distance = (start_distance + phase + midpoint) % perimeter
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
            rotation = math.degrees(math.atan2(tangent_y, tangent_x))
            glyph = affinity.rotate(glyph, rotation, origin=(0.0, 0.0))
            glyph = affinity.translate(
                glyph,
                xoff=location.x,
                yoff=location.y,
            )
            glyph_parts = list(_polygon_parts(glyph))
            if not glyph_parts or any(
                not containment_border.covers(part) for part in glyph_parts
            ):
                return None
            candidates.extend(glyph_parts)
        return candidates or None

    placed: list[Polygon] | None = None
    lower = 0.0
    upper = maximum_size
    for _ in range(40):
        candidate_size = (lower + upper) / 2.0
        candidate_layout = None
        for phase in phase_offsets:
            candidate_layout = layout(candidate_size, phase)
            if candidate_layout is not None:
                break
        if candidate_layout is not None:
            lower = candidate_size
            placed = candidate_layout
        else:
            upper = candidate_size
    if placed is None or lower < 0.05:
        raise Gpx2StlError(
            "The custom base margin cannot fit the requested text; increase "
            "--boundary-percent, reduce --text-margin, shorten the text, or "
            "use a narrower font."
        )
    return placed


def _custom_text_mesh(config: Config, custom_base: CustomBase) -> trimesh.Trimesh:
    placed = _custom_text_polygons(config, custom_base)
    components: list[trimesh.Trimesh] = []
    for polygon in placed:
        try:
            mesh = trimesh.creation.extrude_polygon(
                polygon,
                height=config.text_height + custom_base.overlap_depth,
            )
        except Exception as exc:
            raise Gpx2StlError(f"Unable to create custom-base text: {exc}") from exc
        mesh.apply_translation(
            (0.0, 0.0, custom_base.top_z - custom_base.overlap_depth)
        )
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
    base_height: float | None = None,
    clamp_bottom_to_zero: bool = True,
    overlap_depth: float = 0.05,
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

        if config.topo:

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
) -> Geometry:
    raw_height = None if dem is None else _raw_height_function(dem, route, transform)
    if custom_base is not None:
        terrain, surface = _custom_terrain(custom_base, raw_height, config)
        text_mesh = (
            _custom_text_mesh(config, custom_base)
            if config.text is not None
            else None
        )
    elif config.text is not None:
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
    route_mesh = _route_mesh(
        route,
        transform,
        surface,
        config,
        custom_base.top_z if custom_base is not None else None,
        custom_base is None,
        custom_base.overlap_depth if custom_base is not None else 0.05,
    )
    meshes = [("terrain/base", terrain), ("route", route_mesh)]
    if text_mesh is not None:
        meshes.append(("text", text_mesh))
    for name, mesh in meshes:
        if not mesh.is_watertight:
            raise Gpx2StlError(f"Generated {name} mesh is not watertight.")
        if not np.all(np.isfinite(mesh.vertices)) or mesh.volume <= 0:
            raise Gpx2StlError(f"Generated {name} mesh is invalid.")
    return Geometry(terrain=terrain, route=route_mesh, text=text_mesh)
