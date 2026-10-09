from __future__ import annotations

import numpy as np
from pyproj import Transformer
from shapely.geometry import LineString

from gpx2stl.models import ProjectedRoute
from gpx2stl.road_match import match_route_to_roads


def _route(points: list[tuple[float, float]]) -> ProjectedRoute:
    path = np.asarray(points, dtype=np.float64)
    return ProjectedRoute(
        paths=(path,),
        elevations=(np.arange(len(path), dtype=np.float64),),
        forward=Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True),
        inverse=Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True),
    )


def test_matches_nearby_connected_road() -> None:
    route = _route([(0, 1), (5, 1), (10, 1)])
    roads = (
        LineString([(0, 0), (5, 0)]),
        LineString([(5, 0), (10, 0)]),
    )

    result = match_route_to_roads(route, roads, 5.0)

    assert np.allclose(result.paths[0], [(0, 0), (5, 0), (10, 0)])
    assert len(result.elevations[0]) == len(result.paths[0])


def test_keeps_route_when_road_is_outside_threshold() -> None:
    route = _route([(0, 6), (10, 6)])

    result = match_route_to_roads(route, (LineString([(0, 0), (10, 0)]),), 5.0)

    assert np.array_equal(result.paths[0], route.paths[0])


def test_keeps_segment_when_nearest_roads_are_disconnected() -> None:
    route = _route([(0, 1), (10, 1)])
    roads = (
        LineString([(0, 0), (2, 0)]),
        LineString([(8, 0), (10, 0)]),
    )

    result = match_route_to_roads(route, roads, 5.0)

    assert np.array_equal(result.paths[0], route.paths[0])


def test_zero_distance_disables_matching() -> None:
    route = _route([(0, 1), (10, 1)])

    result = match_route_to_roads(route, (LineString([(0, 0), (10, 0)]),), 0.0)

    assert result is route
