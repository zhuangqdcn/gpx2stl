from __future__ import annotations

import numpy as np

from gpx2stl.footprint import (
    add_route_clearance,
    create_footprint,
    minimum_enclosing_circle,
)


def test_square_padding_is_added_on_every_side() -> None:
    points = np.array([[0.0, 0.0], [100.0, 50.0]])
    footprint = create_footprint(points, "square", 10.0, 1.0)
    assert footprint.center.tolist() == [50.0, 25.0]
    assert footprint.diameter == 120.0


def test_minimum_enclosing_circle() -> None:
    points = np.array([[-1.0, 0.0], [1.0, 0.0], [0.0, 1.0]])
    center, radius = minimum_enclosing_circle(points)
    assert np.allclose(center, [0.0, 0.0])
    assert np.isclose(radius, 1.0)


def test_circle_padding_increases_radius() -> None:
    points = np.array([[-10.0, 0.0], [10.0, 0.0]])
    footprint = create_footprint(points, "circle", 10.0, 1.0)
    assert np.isclose(footprint.radius, 11.0)


def test_route_clearance_preserves_requested_model_size() -> None:
    points = np.array([[-10.0, 0.0], [10.0, 0.0]])
    footprint = create_footprint(points, "square", 0.0, 1.0)
    cleared = add_route_clearance(footprint, route_width=2.0, max_size=20.0)
    scale = 20.0 / cleared.diameter
    assert np.isclose((cleared.max_xy[0] - 10.0) * scale, 1.0)
