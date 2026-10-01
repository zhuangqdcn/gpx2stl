from __future__ import annotations

import numpy as np

from gpx2stl.footprint import (
    add_route_clearance,
    create_footprint,
    footprint_vertices,
    minimum_enclosing_circle,
    minimum_enclosing_hexagon,
)


def test_square_padding_is_added_on_every_side() -> None:
    points = np.array([[0.0, 0.0], [100.0, 50.0]])
    footprint = create_footprint(points, "square", 10.0, 1.0)
    assert footprint.center.tolist() == [50.0, 25.0]
    assert footprint.diameter == 120.0


def test_minimum_enclosing_circle() -> None:
    angles = np.deg2rad([0.0, 120.0, 240.0])
    points = np.column_stack((np.cos(angles), np.sin(angles)))
    center, radius = minimum_enclosing_circle(points)
    assert np.allclose(center, [0.0, 0.0])
    assert np.isclose(radius, 1.0)


def test_circle_padding_increases_radius() -> None:
    points = np.array([[-10.0, 0.0], [10.0, 0.0]])
    footprint = create_footprint(points, "circle", 10.0, 1.0)
    assert np.isclose(footprint.radius, 11.0)


def test_minimum_flat_top_hexagon_contains_points() -> None:
    center = np.array([3.0, -4.0])
    radius = 10.0
    angles = np.deg2rad(np.arange(0.0, 360.0, 60.0))
    points = center + radius * np.column_stack((np.cos(angles), np.sin(angles)))
    result_center, result_radius = minimum_enclosing_hexagon(points)
    assert np.allclose(result_center, center)
    assert np.isclose(result_radius, radius)


def test_hex_padding_and_orientation() -> None:
    points = np.array([[-10.0, 0.0], [10.0, 0.0]])
    footprint = create_footprint(points, "hex", 10.0, 1.0)
    vertices = footprint_vertices(footprint)
    assert np.isclose(footprint.radius, 11.0)
    assert np.isclose(vertices[1, 1], vertices[2, 1])
    assert np.isclose(vertices[4, 1], vertices[5, 1])


def test_route_clearance_preserves_requested_model_size() -> None:
    points = np.array([[-10.0, 0.0], [10.0, 0.0]])
    footprint = create_footprint(points, "square", 0.0, 1.0)
    cleared = add_route_clearance(footprint, route_width=2.0, max_size=20.0)
    scale = 20.0 / cleared.diameter
    assert np.isclose((cleared.max_xy[0] - 10.0) * scale, 1.0)


def test_hex_route_clearance_uses_apothem() -> None:
    points = np.array([[-10.0, 0.0], [10.0, 0.0]])
    footprint = create_footprint(points, "hex", 0.0, 1.0)
    cleared = add_route_clearance(footprint, route_width=2.0, max_size=20.0)
    scale = 20.0 / cleared.diameter
    original_apothem = footprint.radius * np.sqrt(3.0) / 2.0
    outer_apothem = 20.0 * np.sqrt(3.0) / 4.0
    assert np.isclose(outer_apothem - original_apothem * scale, 1.0)
