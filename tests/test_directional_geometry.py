"""Directional support math and independent spatial detection contracts."""

import numpy as np
import pytest
from pyproj import Transformer
from shapely.geometry import LineString, Point

from gpx2stl import auto_boundary as boundary
from gpx2stl.dem import DemSource
from gpx2stl.directions import DIRECTIONS, route_projections, support_polygon
from gpx2stl.errors import Gpx2StlError
from gpx2stl.models import ProjectedRoute


def activity(points=((-100, -50), (100, 50))):
    forward = Transformer.from_crs(4326, 32610, always_xy=True)
    inverse = Transformer.from_crs(32610, 4326, always_xy=True)
    points = np.asarray(points, dtype=np.float64)
    return ProjectedRoute((points,), (np.zeros(len(points)),), forward, inverse)


class FlatDem(DemSource):
    def sample_projected(self, points, route):
        return np.zeros(len(points))


def assert_constraints(polygon, limits):
    vertices = np.asarray(polygon.exterior.coords)
    for direction, limit in zip(DIRECTIONS, limits):
        assert np.max(vertices @ direction.vector) <= limit + 1e-7


def test_compass_unit_vectors_and_normalized_diagonal_spans():
    assert [direction.name for direction in DIRECTIONS] == ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    assert np.allclose(np.linalg.norm([item.vector for item in DIRECTIONS], axis=1), 1)
    points = np.array([[0, 0], [200, 100], [100, -100]], dtype=float)
    maxima, spans = route_projections(points)
    assert spans[1] == pytest.approx(300 / np.sqrt(2))
    assert spans[3] == pytest.approx(200 / np.sqrt(2))
    polygon = support_polygon(maxima + spans, points[0])
    assert polygon.covers(LineString(points))
    assert_constraints(polygon, maxima + spans)


def test_redundant_constraints_and_diagonal_corner_cuts():
    limits = np.array([10, 100, 10, 100, 10, 100, 10, 100], dtype=float)
    square = support_polygon(limits, np.zeros(2))
    assert len(square.exterior.coords) == 5
    limits[1::2] = 11
    octagon = support_polygon(limits, np.zeros(2))
    assert len(octagon.exterior.coords) == 9
    assert not octagon.covers(Point(10, 10))
    assert_constraints(octagon, limits)


@pytest.mark.parametrize("offset", [(0, 0), (500000, 4000000)])
def test_detected_zero_padding_retains_route_vertices_on_half_plane_boundaries(offset):
    route = activity(np.array((
        (1641.7588983404275, -796.7592679761233),
        (-984.9450668044964, -1416.559823429488),
        (-1236.434639106705, -1173.6968607737626),
        (-703.9362255075705, -949.3200512224948),
        (-656.1756024478686, 476.7711171230053),
    )) + offset)
    maxima, _ = route_projections(route.points)
    limits = maxima + np.array([0, 500, 1000, 1500, 1500, 0, 0, 0])
    evidence = tuple(
        boundary._DirectionalEvidence(
            float(limit) - 45 * sum(abs(value) for value in direction.vector) - 90,
            np.empty(0), True,
        )
        for direction, limit in zip(DIRECTIONS, limits)
    )
    result = boundary._finish(
        route, FlatDem(()), 1, 8000, evidence, [True] * 8,
        ["stable boundary"] * 8, lambda _: None,
    )
    assert result.envelope.covers(LineString(route.points))
    assert all(item.resolved for item in result.directions)
    np.testing.assert_allclose(
        [item.support_limit for item in result.directions], limits, rtol=0, atol=1e-7
    )
    assert_constraints(result.envelope, limits)


@pytest.mark.parametrize("index", range(8), ids=[item.name for item in DIRECTIONS])
def test_single_direction_fallback_preserves_all_other_constraints(index):
    route = activity()
    maxima, spans = route_projections(route.points)
    evidence = tuple(boundary._DirectionalEvidence(float(limit), np.empty(0), True)
                     for limit in maxima + 4000)
    resolved = [True] * 8
    resolved[index] = False
    logs = []
    result = boundary._finish(route, FlatDem(()), 1, 4000, evidence, resolved,
                              ["test spatial evidence"] * 8, logs.append)
    assert [item.resolved for item in result.directions] == resolved
    fallback = result.directions[index]
    assert fallback.support_limit == pytest.approx(maxima[index] + spans[index])
    for other, item in enumerate(result.directions):
        if other != index:
            cell_extent = 45 * sum(abs(value) for value in DIRECTIONS[other].vector)
            assert item.support_limit == pytest.approx(evidence[other].support + cell_extent + 90)
            assert item.padding_m > spans[other]
    assert_constraints(result.envelope, [item.support_limit for item in result.directions])
    assert result.envelope.covers(LineString(route.points))
    # Adjacent fallback may make a requested detected constraint redundant.
    supports = np.asarray(result.envelope.exterior.coords) @ np.asarray([item.vector for item in DIRECTIONS]).T
    assert np.any(supports.max(axis=0) < np.array([item.support_limit for item in result.directions]) - 1)
    assert any("may crop" in message and "complete mountain coverage" in message for message in logs)
    assert sum("using 100% fallback" in message for message in logs) == 1


@pytest.mark.parametrize("index", range(8), ids=[item.name for item in DIRECTIONS])
def test_directional_discovery_independently_rejects_changed_support(monkeypatch, index):
    route = activity()
    windows = iter((2000, 4000))
    route_bounds = np.concatenate((route.points.min(axis=0), route.points.max(axis=0)))
    calls = []

    def segment(elevation, corridor, criteria):
        distance = next(windows)
        grid = boundary._grid(route_bounds + [-distance, -distance, distance, distance])
        x, y = np.meshgrid(grid.x * 90, grid.y * 90)
        selected = np.hypot(x, y) <= 2000
        if calls:
            tip = np.rint(np.asarray(DIRECTIONS[index].vector) * 2500 / 90).astype(int)
            selected[tip[1] - grid.y[0], tip[0] - grid.x[0]] = True
        calls.append(distance)
        # An unrelated summit count change must not invalidate other directions.
        return selected, len(calls), None

    monkeypatch.setattr(boundary, "_segment", segment)
    result = boundary.discover_auto_boundary(route, 4, lambda _: FlatDem(()), progress=lambda _: None)
    assert [item.resolved for item in result.directions] == [other != index for other in range(8)]
    assert result.directions[index].padding_m == pytest.approx(route_projections(route.points)[1][index])


def test_zero_span_substitution_and_fully_degenerate_error():
    route = activity(((-100, 0), (100, 0)))
    logs = []
    result = boundary.discover_auto_boundary(route, 2, lambda _: FlatDem(()), progress=logs.append)
    assert result.directions[0].span_m == result.directions[4].span_m == 200
    assert result.directions[0].used_zero_span_basis
    assert result.directions[4].used_zero_span_basis
    assert sum("zero projected route span" in message for message in logs) == 2
    maxima, spans = route_projections(route.points)
    basis = np.where(spans == 0, spans.max(), spans)
    assert_constraints(result.envelope, maxima + basis)
    with pytest.raises(Gpx2StlError, match="all eight projected route spans are zero"):
        boundary.discover_auto_boundary(activity(((0, 0), (0, 0))), 2,
                                        lambda _: FlatDem(()), progress=lambda _: None)


def test_terminal_no_regions_invalidates_earlier_evidence(monkeypatch):
    route = activity()
    calls = []

    def segment(elevation, corridor, criteria):
        calls.append(1)
        if len(calls) == 1:
            return corridor, 1, None
        return np.ones(elevation.shape, dtype=bool), 0, "no observable valley floor"

    monkeypatch.setattr(boundary, "_segment", segment)
    result = boundary.discover_auto_boundary(route, 4, lambda _: FlatDem(()), progress=lambda _: None)
    assert result.region_count == 0
    assert all(not item.resolved and item.reason == "no observable valley floor" for item in result.directions)


def test_spatial_reassignment_and_edge_contact_invalidate_stability():
    grid = boundary._grid(np.array([-2000, -2000, 2000, 2000], dtype=float))
    x, y = np.meshgrid(grid.x, grid.y)
    mask = (np.abs(x) <= 5) & (np.abs(y) <= 5)
    initial = boundary._directional_evidence(mask, grid)
    shifted = np.roll(mask, 8, axis=1)
    changed = boundary._directional_evidence(shifted, grid)
    assert initial[0].support == changed[0].support
    assert not boundary._direction_stable(initial[0], changed[0], DIRECTIONS[0].vector)
    mask[-1, len(grid.x) // 2] = True
    edge = boundary._directional_evidence(mask, grid)
    assert not edge[0].interior
    assert not boundary._direction_stable(initial[0], edge[0], DIRECTIONS[0].vector)


def test_found_direction_is_not_frozen_when_later_evidence_changes(monkeypatch):
    route = activity()
    windows = iter((2000, 4000, 8000))
    route_bounds = np.concatenate((route.points.min(axis=0), route.points.max(axis=0)))

    def segment(elevation, corridor, criteria):
        distance = next(windows)
        grid = boundary._grid(route_bounds + [-distance, -distance, distance, distance])
        x, y = np.meshgrid(grid.x * 90, grid.y * 90)
        selected = np.hypot(x, y) <= 1000
        # East remains unresolved, forcing a third window after N was found.
        selected[np.abs(y) < 90] = x[np.abs(y) < 90] >= 0
        if distance == 8000:
            selected[(np.abs(x) < 90) & (y >= 0) & (y < 2000)] = True
        return selected, 1, None

    monkeypatch.setattr(boundary, "_segment", segment)
    logs = []
    result = boundary.discover_auto_boundary(route, 8, lambda _: FlatDem(()), progress=logs.append)
    assert "Search 4 km N: valley boundary found and stable" in logs
    assert any(message.startswith("Search 8 km N: candidate") for message in logs)
    assert not result.directions[0].resolved
    assert any("N: valley boundary not found; using 100% fallback" in message for message in logs)
