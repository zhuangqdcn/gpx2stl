"""Shared metric compass directions and convex support geometry."""

from dataclasses import dataclass
from itertools import combinations
import math

import numpy as np
from numpy.typing import NDArray
from shapely.geometry import Polygon

from gpx2stl.errors import Gpx2StlError


@dataclass(frozen=True)
class Direction:
    name: str
    vector: tuple[float, float]


_DIAGONAL = 1 / math.sqrt(2)
DIRECTIONS = (
    Direction("N", (0, 1)),
    Direction("NE", (_DIAGONAL, _DIAGONAL)),
    Direction("E", (1, 0)),
    Direction("SE", (_DIAGONAL, -_DIAGONAL)),
    Direction("S", (0, -1)),
    Direction("SW", (-_DIAGONAL, -_DIAGONAL)),
    Direction("W", (-1, 0)),
    Direction("NW", (-_DIAGONAL, _DIAGONAL)),
)
SPAN_TOLERANCE_M = 1e-8


def route_projections(
    points: NDArray[np.float64],
) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    vectors = np.asarray([direction.vector for direction in DIRECTIONS])
    projected = points @ vectors.T
    # Subtract a local origin before measuring spans, to avoid cancellation.
    local = (points - points[0]) @ vectors.T
    spans = np.ptp(local, axis=0)
    spans[spans <= SPAN_TOLERANCE_M] = 0
    return projected.max(axis=0), spans


def support_polygon(
    limits: NDArray[np.float64], origin: NDArray[np.float64]
) -> Polygon:
    """Intersect eight half-planes without an arbitrary enclosing world box."""
    vectors = np.asarray([direction.vector for direction in DIRECTIONS])
    limits = np.asarray(limits, dtype=np.float64)
    origin = np.asarray(origin, dtype=np.float64)
    if limits.shape != (8,) or not np.all(np.isfinite(limits)):
        raise Gpx2StlError("Automatic terrain boundary: invalid directional support limits")
    local_limits = limits - vectors @ origin
    vertices = []
    for first, second in combinations(range(8), 2):
        matrix = vectors[[first, second]]
        if abs(float(np.linalg.det(matrix))) < 1e-12:
            continue
        point = np.linalg.solve(matrix, local_limits[[first, second]])
        if np.all(vectors @ point <= local_limits + 1e-8):
            if not any(np.linalg.norm(point - existing) <= 1e-8 for existing in vertices):
                vertices.append(point)
    if len(vertices) < 3:
        raise Gpx2StlError("Automatic terrain boundary: empty directional intersection")
    coordinates = np.asarray(vertices)
    center = coordinates.mean(axis=0)
    offsets = coordinates - center
    order = np.argsort(np.arctan2(offsets[:, 1], offsets[:, 0]))
    polygon = Polygon(coordinates[order] + origin)
    if not polygon.is_valid or not math.isfinite(polygon.area) or polygon.area <= 0:
        raise Gpx2StlError("Automatic terrain boundary: invalid directional polygon")
    return polygon
