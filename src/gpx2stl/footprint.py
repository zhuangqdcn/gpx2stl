from __future__ import annotations

import math
import random

import numpy as np
from numpy.typing import NDArray
from scipy.optimize import linprog

from gpx2stl.models import Footprint, ModelTransform, Shape

_HEX_APOTHEM_RATIO = math.sqrt(3.0) / 2.0


def _circle_from_two(a: NDArray[np.float64], b: NDArray[np.float64]) -> tuple[np.ndarray, float]:
    center = (a + b) / 2.0
    return center, float(np.linalg.norm(a - center))


def _circle_from_three(
    a: NDArray[np.float64], b: NDArray[np.float64], c: NDArray[np.float64]
) -> tuple[np.ndarray, float] | None:
    ab = b - a
    ac = c - a
    cross = ab[0] * ac[1] - ab[1] * ac[0]
    if abs(float(cross)) < 1e-12:
        return None
    denominator = 2.0 * float(cross)
    aa = float(np.dot(a, a))
    bb = float(np.dot(b, b))
    cc = float(np.dot(c, c))
    center = np.array(
        [
            (aa * (b[1] - c[1]) + bb * (c[1] - a[1]) + cc * (a[1] - b[1]))
            / denominator,
            (aa * (c[0] - b[0]) + bb * (a[0] - c[0]) + cc * (b[0] - a[0]))
            / denominator,
        ],
        dtype=np.float64,
    )
    return center, float(np.linalg.norm(a - center))


def _contains(center: NDArray[np.float64], radius: float, point: NDArray[np.float64]) -> bool:
    return float(np.linalg.norm(point - center)) <= radius + max(1e-7, radius * 1e-10)


def minimum_enclosing_circle(points: NDArray[np.float64]) -> tuple[np.ndarray, float]:
    shuffled = [point.copy() for point in points]
    random.Random(0).shuffle(shuffled)
    center = shuffled[0]
    radius = 0.0
    for i, point in enumerate(shuffled):
        if _contains(center, radius, point):
            continue
        center = point
        radius = 0.0
        for j in range(i):
            other = shuffled[j]
            if _contains(center, radius, other):
                continue
            center, radius = _circle_from_two(point, other)
            for k in range(j):
                third = shuffled[k]
                if _contains(center, radius, third):
                    continue
                circle = _circle_from_three(point, other, third)
                if circle is not None:
                    center, radius = circle
    return center, radius


def minimum_enclosing_hexagon(
    points: NDArray[np.float64],
) -> tuple[NDArray[np.float64], float]:
    angles = np.deg2rad(np.arange(30.0, 360.0, 60.0))
    normals = np.column_stack((np.cos(angles), np.sin(angles)))
    constraints = []
    limits = []
    for point in points:
        for normal in normals:
            constraints.append((-normal[0], -normal[1], -_HEX_APOTHEM_RATIO))
            limits.append(-float(np.dot(normal, point)))
    result = linprog(
        c=np.array([0.0, 0.0, 1.0]),
        A_ub=np.asarray(constraints),
        b_ub=np.asarray(limits),
        bounds=((None, None), (None, None), (0.0, None)),
        method="highs",
    )
    if not result.success:
        raise ValueError(f"Unable to construct a hexagonal footprint: {result.message}")
    return np.asarray(result.x[:2], dtype=np.float64), float(result.x[2])


def footprint_vertices(footprint: Footprint) -> NDArray[np.float64]:
    if footprint.shape == "square":
        minimum = footprint.min_xy
        maximum = footprint.max_xy
        return np.array(
            [
                [minimum[0], minimum[1]],
                [maximum[0], minimum[1]],
                [maximum[0], maximum[1]],
                [minimum[0], maximum[1]],
            ],
            dtype=np.float64,
        )
    if footprint.shape == "hex":
        angles = np.deg2rad(np.arange(0.0, 360.0, 60.0))
        return footprint.center + footprint.radius * np.column_stack(
            (np.cos(angles), np.sin(angles))
        )
    raise ValueError("Circular footprints do not have polygon vertices.")


def create_footprint(
    points: NDArray[np.float64],
    shape: Shape,
    boundary_percent: float,
    minimum_diameter: float,
) -> Footprint:
    padding = boundary_percent / 100.0
    if shape == "circle":
        center, radius = minimum_enclosing_circle(points)
        radius *= 1.0 + padding
    elif shape == "hex":
        center, radius = minimum_enclosing_hexagon(points)
        radius *= 1.0 + padding
    else:
        minimum = points.min(axis=0)
        maximum = points.max(axis=0)
        center = (minimum + maximum) / 2.0
        span = maximum - minimum
        side = float(max(span)) * (1.0 + 2.0 * padding)
        radius = side / 2.0
    radius = max(radius, minimum_diameter / 2.0)
    return Footprint(shape, np.asarray(center, dtype=np.float64), radius)


def create_model_transform(footprint: Footprint, max_size: float) -> ModelTransform:
    if not math.isfinite(footprint.diameter) or footprint.diameter <= 0:
        raise ValueError("Footprint diameter must be finite and positive.")
    return ModelTransform(footprint, max_size / footprint.diameter)


def add_route_clearance(
    footprint: Footprint, route_width: float, max_size: float
) -> Footprint:
    clearance_diameter = (
        max_size * _HEX_APOTHEM_RATIO if footprint.shape == "hex" else max_size
    )
    if route_width >= clearance_diameter:
        raise ValueError("Route width must be smaller than the footprint width.")
    factor = clearance_diameter / (clearance_diameter - route_width)
    return Footprint(footprint.shape, footprint.center, footprint.radius * factor)
