from __future__ import annotations

import heapq
import math
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray
from shapely.geometry import LineString, Point
from shapely.strtree import STRtree

from gpx2stl.models import ProjectedRoute

Coordinate = tuple[float, float]


@dataclass(frozen=True)
class _Edge:
    start: Coordinate
    end: Coordinate
    length: float


@dataclass(frozen=True)
class _Candidate:
    point: Coordinate
    edge: _Edge
    from_start: float
    distance: float


def _coordinate(value: NDArray[np.float64] | tuple[float, float]) -> Coordinate:
    return round(float(value[0]), 9), round(float(value[1]), 9)


def _road_graph(
    roads: tuple[LineString, ...],
) -> tuple[list[LineString], list[_Edge], dict[Coordinate, list[tuple[Coordinate, float]]]]:
    segments: list[LineString] = []
    edges: list[_Edge] = []
    graph: dict[Coordinate, list[tuple[Coordinate, float]]] = {}
    for road in roads:
        coordinates = list(road.coords)
        for first, second in zip(coordinates, coordinates[1:], strict=False):
            start = _coordinate(first)
            end = _coordinate(second)
            length = math.dist(start, end)
            if length <= 1e-9:
                continue
            segments.append(LineString((start, end)))
            edges.append(_Edge(start, end, length))
            graph.setdefault(start, []).append((end, length))
            graph.setdefault(end, []).append((start, length))
    return segments, edges, graph


def _candidate(
    point: NDArray[np.float64],
    tree: STRtree,
    segments: list[LineString],
    edges: list[_Edge],
    maximum_distance: float,
) -> _Candidate | None:
    source = Point(float(point[0]), float(point[1]))
    index = int(tree.nearest(source))
    segment = segments[index]
    distance = float(source.distance(segment))
    if distance > maximum_distance:
        return None
    along = float(segment.project(source))
    snapped = segment.interpolate(along)
    return _Candidate(
        (float(snapped.x), float(snapped.y)),
        edges[index],
        along,
        distance,
    )


def _node_path(
    graph: dict[Coordinate, list[tuple[Coordinate, float]]],
    start: Coordinate,
    end: Coordinate,
    maximum_length: float,
) -> tuple[float, list[Coordinate]] | None:
    queue: list[tuple[float, float, Coordinate]] = [
        (math.dist(start, end), 0.0, start)
    ]
    distances = {start: 0.0}
    previous: dict[Coordinate, Coordinate] = {}
    while queue:
        _, distance, node = heapq.heappop(queue)
        if distance != distances.get(node):
            continue
        if node == end:
            path = [end]
            while path[-1] != start:
                path.append(previous[path[-1]])
            path.reverse()
            return distance, path
        for neighbor, edge_length in graph.get(node, ()):
            candidate_distance = distance + edge_length
            if candidate_distance > maximum_length:
                continue
            if candidate_distance >= distances.get(neighbor, math.inf):
                continue
            distances[neighbor] = candidate_distance
            previous[neighbor] = node
            estimate = candidate_distance + math.dist(neighbor, end)
            heapq.heappush(queue, (estimate, candidate_distance, neighbor))
    return None


def _matched_segment(
    first: _Candidate,
    second: _Candidate,
    graph: dict[Coordinate, list[tuple[Coordinate, float]]],
    maximum_length: float,
) -> list[Coordinate] | None:
    if first.edge == second.edge:
        if abs(first.from_start - second.from_start) <= maximum_length:
            return [first.point, second.point]
        return None
    choices: list[tuple[float, list[Coordinate]]] = []
    first_ends = (
        (first.edge.start, first.from_start),
        (first.edge.end, first.edge.length - first.from_start),
    )
    second_ends = (
        (second.edge.start, second.from_start),
        (second.edge.end, second.edge.length - second.from_start),
    )
    for first_node, first_cost in first_ends:
        for second_node, second_cost in second_ends:
            remaining = maximum_length - first_cost - second_cost
            if remaining < 0:
                continue
            result = _node_path(graph, first_node, second_node, remaining)
            if result is None:
                continue
            graph_cost, nodes = result
            choices.append(
                (
                    first_cost + graph_cost + second_cost,
                    [first.point, *nodes, second.point],
                )
            )
    if not choices:
        return None
    length, path = min(choices, key=lambda item: item[0])
    if length > maximum_length:
        return None
    deduplicated = [path[0]]
    deduplicated.extend(point for point in path[1:] if point != deduplicated[-1])
    return deduplicated


def match_route_to_roads(
    route: ProjectedRoute,
    roads: tuple[LineString, ...],
    maximum_distance: float,
) -> ProjectedRoute:
    if maximum_distance <= 0 or not roads:
        return route
    segments, edges, graph = _road_graph(roads)
    if not segments:
        return route
    tree = STRtree(segments)
    matched_paths: list[NDArray[np.float64]] = []
    matched_elevations: list[NDArray[np.float64]] = []
    for path, elevations in zip(route.paths, route.elevations, strict=True):
        candidates = [
            _candidate(point, tree, segments, edges, maximum_distance)
            for point in path
        ]
        result_points: list[Coordinate] = [_coordinate(path[0])]
        result_elevations: list[float] = [float(elevations[0])]
        for index in range(len(path) - 1):
            source_start = _coordinate(path[index])
            source_end = _coordinate(path[index + 1])
            direct_length = math.dist(source_start, source_end)
            maximum_length = max(
                direct_length * 1.5,
                direct_length + 2.0 * maximum_distance,
            )
            first = candidates[index]
            second = candidates[index + 1]
            matched = (
                _matched_segment(first, second, graph, maximum_length)
                if first is not None and second is not None
                else None
            )
            segment = matched or [source_start, source_end]
            if index == 0 and matched is not None:
                result_points[0] = segment[0]
            if result_points[-1] != segment[0]:
                result_points.append(segment[0])
                result_elevations.append(float(elevations[index]))
            segment_lengths = np.asarray(
                [math.dist(a, b) for a, b in zip(segment, segment[1:], strict=False)]
            )
            total = float(np.sum(segment_lengths))
            fractions = (
                np.concatenate(([0.0], np.cumsum(segment_lengths))) / total
                if total > 0
                else np.linspace(0.0, 1.0, len(segment))
            )
            start_elevation = float(elevations[index])
            end_elevation = float(elevations[index + 1])
            for point, fraction in zip(segment[1:], fractions[1:], strict=True):
                if point == result_points[-1]:
                    continue
                result_points.append(point)
                result_elevations.append(
                    start_elevation
                    + float(fraction) * (end_elevation - start_elevation)
                )
        matched_paths.append(np.asarray(result_points, dtype=np.float64))
        matched_elevations.append(np.asarray(result_elevations, dtype=np.float64))
    return ProjectedRoute(
        paths=tuple(matched_paths),
        elevations=tuple(matched_elevations),
        forward=route.forward,
        inverse=route.inverse,
    )
