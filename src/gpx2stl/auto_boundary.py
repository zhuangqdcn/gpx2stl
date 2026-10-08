"""Bounded, reproducible DEM-based *estimates* of mountain slope envelopes.

This is not named-mountain identification. Low, nearly level valley floors act
as background markers in an inverted-elevation watershed; summit plateaus act
as foreground markers. A massif without an observable valley floor is
deliberately unresolved, rather than silently cropped at the search limit.
"""

from __future__ import annotations

import heapq
import math
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray
from pyproj import Transformer
from scipy import ndimage
from shapely.geometry import Polygon, box

from gpx2stl.dem import DemSource, GeographicBounds, request_projected_bounds
from gpx2stl.errors import Gpx2StlError
from gpx2stl.models import ProjectedRoute
from gpx2stl.progress import ProgressCallback, console_progress

# Fixed metric sampling is anchored at projected (0, 0), never at a window edge.
GRID_SPACING_M = 90.0
MAX_GRID_CELLS = 1_000_000
SAMPLE_BATCH_SIZE = 65_536
MAX_SUMMIT_MARKERS = 4096
INITIAL_SEARCH_DISTANCE_M = 2000.0
SMOOTHING_SIGMA_M = 135.0
SUMMIT_SEPARATION_M = 450.0
MIN_RELIEF_M = 60.0
# Merge noise summits / shallow saddles, not distinct peaks across deep valleys.
MIN_PROMINENCE_M = 35.0
SHALLOW_SADDLE_FRACTION = 0.12
# Require level valley floors, rather than misclassifying gentle foothills.
VALLEY_RELIEF_M = 1.0
VALLEY_SLOPE = 0.002
SAFETY_BUFFER_M = GRID_SPACING_M
EDGE_GUARD_CELLS = 4
STABILITY_FRACTION = 0.005
MAX_DEM_PIXEL_M = 3.0 * GRID_SPACING_M


@dataclass(frozen=True)
class AutoBoundaryResult:
    envelope: Polygon
    dem: DemSource
    region_count: int
    search_distance_m: float


@dataclass(frozen=True)
class _Grid:
    x: NDArray[np.int64]
    y: NDArray[np.int64]

    @property
    def shape(self) -> tuple[int, int]:
        return len(self.y), len(self.x)


def _failure(reason: str) -> Gpx2StlError:
    return Gpx2StlError(
        f"Automatic terrain boundary: {reason}. Use a numeric "
        "--route-boundary-percent (for example 10), better local DEM data, "
        "or a larger --auto-boundary-max-distance-km."
    )


def _grid(bounds: NDArray[np.float64]) -> _Grid:
    first = np.ceil(bounds[:2] / GRID_SPACING_M).astype(np.int64)
    last = np.floor(bounds[2:] / GRID_SPACING_M).astype(np.int64)
    size = last - first + 1
    if np.any(size < 2 * EDGE_GUARD_CELLS + 1):
        raise _failure("search window is too small for the 90 m analysis grid")
    if int(size[0]) * int(size[1]) > MAX_GRID_CELLS:
        raise _failure(
            f"90 m analysis grid exceeds the {MAX_GRID_CELLS:,}-cell resource budget"
        )
    return _Grid(
        np.arange(first[0], last[0] + 1, dtype=np.int64),
        np.arange(first[1], last[1] + 1, dtype=np.int64),
    )


def _check_resolution(dem: DemSource, route: ProjectedRoute, grid: _Grid) -> None:
    resolutions = []
    for tile in dem.tiles:
        transformer = Transformer.from_crs(
            tile.crs, route.forward.target_crs, always_xy=True
        )
        col = tile.data.shape[1] / 2
        row = tile.data.shape[0] / 2
        pixels = np.array(((col, row), (col + 1, row), (col, row + 1)))
        coordinates = pixels @ np.array(
            [[tile.transform.a, tile.transform.d], [tile.transform.b, tile.transform.e]]
        ) + [tile.transform.c, tile.transform.f]
        x, y = transformer.transform(coordinates[:, 0], coordinates[:, 1])
        projected = np.column_stack((x, y))
        resolutions.append(float(np.max(np.linalg.norm(projected[1:] - projected[0], axis=1))))
    if not any(resolution > MAX_DEM_PIXEL_M for resolution in resolutions):
        return
    # Follow DemSource's first-finite-tile precedence at actual analysis samples.
    # An unrelated fine tile must not conceal coarse coverage, but an unused
    # coarse cache tile underneath complete fine coverage is harmless.
    count = len(grid.x) * len(grid.y)
    for start in range(0, count, SAMPLE_BATCH_SIZE):
        indices = np.arange(start, min(start + SAMPLE_BATCH_SIZE, count))
        longitude, latitude = route.inverse.transform(
            grid.x[indices % len(grid.x)] * GRID_SPACING_M,
            grid.y[indices // len(grid.x)] * GRID_SPACING_M,
        )
        longitude = (np.asarray(longitude) + 180.0) % 360.0 - 180.0
        latitude = np.asarray(latitude)
        remaining = np.ones(len(indices), dtype=np.bool_)
        for tile, resolution in zip(dem.tiles, resolutions):
            inside = remaining & tile.covers(longitude, latitude)
            if not np.any(inside):
                continue
            finite = np.isfinite(tile.sample(longitude[inside], latitude[inside]))
            if resolution > MAX_DEM_PIXEL_M and np.any(finite):
                raise _failure(
                    f"contributing DEM resolution is too coarse (requires pixels "
                    f"no larger than {MAX_DEM_PIXEL_M:g} m)"
                )
            remaining[np.flatnonzero(inside)[finite]] = False
            if not np.any(remaining):
                break


def _sample(dem: DemSource, route: ProjectedRoute, grid: _Grid) -> NDArray[np.float64]:
    count = len(grid.x) * len(grid.y)
    result = np.empty(count, dtype=np.float64)
    for start in range(0, count, SAMPLE_BATCH_SIZE):
        indices = np.arange(start, min(start + SAMPLE_BATCH_SIZE, count))
        points = np.column_stack(
            (grid.x[indices % len(grid.x)], grid.y[indices // len(grid.x)])
        ).astype(np.float64) * GRID_SPACING_M
        try:
            values = np.asarray(dem.sample_projected(points, route), dtype=np.float64)
        except Gpx2StlError as exc:
            raise _failure(f"incomplete DEM coverage ({exc})") from exc
        if values.shape != (len(indices),) or not np.all(np.isfinite(values)):
            raise _failure("incomplete finite DEM coverage / nodata in the discovery window")
        result[start : start + len(indices)] = values
    return result.reshape(grid.shape)


def _route_cells(route: ProjectedRoute, grid: _Grid) -> NDArray[np.bool_]:
    mask = np.zeros(grid.shape, dtype=np.bool_)

    def mark(points: NDArray[np.float64]) -> None:
        cells = np.rint(points / GRID_SPACING_M).astype(np.int64)
        cols = cells[:, 0] - grid.x[0]
        rows = cells[:, 1] - grid.y[0]
        inside = (cols >= 0) & (cols < len(grid.x)) & (rows >= 0) & (rows < len(grid.y))
        mask[rows[inside], cols[inside]] = True

    for path in route.paths:
        mark(path)
        for first, last in zip(path[:-1], path[1:]):
            steps = max(1, math.ceil(float(np.linalg.norm(last - first)) / (GRID_SPACING_M / 2)))
            for start in range(0, steps + 1, SAMPLE_BATCH_SIZE):
                fractions = np.arange(start, min(start + SAMPLE_BATCH_SIZE, steps + 1)) / steps
                mark(first + fractions[:, None] * (last - first))
    return ndimage.binary_dilation(mask, structure=np.ones((3, 3), dtype=np.bool_))


def _merge_saddles(
    labels: NDArray[np.int32], height: NDArray[np.float64], floor: float
) -> tuple[NDArray[np.int32], NDArray[np.float64]]:
    count = int(labels.max())
    peaks = ndimage.maximum(height, labels, np.arange(count + 1))
    parent = np.arange(count + 1)

    def root(value: int) -> int:
        while parent[value] != value:
            parent[value] = parent[parent[value]]
            value = int(parent[value])
        return value

    pairs = []
    levels = []
    for a, b, ah, bh in (
        (labels[:, :-1], labels[:, 1:], height[:, :-1], height[:, 1:]),
        (labels[:-1], labels[1:], height[:-1], height[1:]),
    ):
        adjacent = (a != b) & (a > 1) & (b > 1)
        if np.any(adjacent):
            pairs.append(np.sort(np.column_stack((a[adjacent], b[adjacent])), axis=1))
            levels.append(np.minimum(ah[adjacent], bh[adjacent]))
    if pairs:
        edges = np.concatenate(pairs)
        saddles = np.concatenate(levels)
        unique, inverse = np.unique(edges, axis=0, return_inverse=True)
        highest = np.full(len(unique), -np.inf)
        np.maximum.at(highest, inverse, saddles)
        for index in np.argsort(-highest, kind="stable"):
            a, b = (root(int(v)) for v in unique[index])
            if a == b:
                continue
            low_peak = min(peaks[a], peaks[b])
            prominence = low_peak - highest[index]
            threshold = max(MIN_PROMINENCE_M, SHALLOW_SADDLE_FRACTION * (low_peak - floor))
            if prominence < threshold:
                keep, drop = (a, b) if peaks[a] >= peaks[b] else (b, a)
                parent[drop] = keep
                peaks[keep] = max(peaks[a], peaks[b])
    mapping = np.array([root(i) for i in range(count + 1)], dtype=np.int32)
    return mapping[labels], peaks


def _watershed(
    height: NDArray[np.float64], markers: NDArray[np.int32]
) -> NDArray[np.int32]:
    """Flood inverted elevation, with deterministic FIFO ties on plateaus.

    SciPy's watershed_ift minimizes *edge differences*, not flood elevation.
    On a smooth cone it lets a flat background invade the slopes. Use a
    marker-controlled elevation priority flood with SciPy-derived markers
    instead: a peak claims its complete descending slopes up to a saddle or
    an observed valley-floor marker.
    """
    labels = markers.copy()
    rows, cols = labels.shape
    flat = labels.ravel()
    inverted = (height.max() - height).ravel()
    pending = []
    serial = 0
    # Only marker frontiers enter the queue; large flat valley plateaus need
    # not allocate one Python heap entry for every background cell.
    frontier = (labels > 0) & ndimage.binary_dilation(labels == 0)
    for index in np.flatnonzero(frontier.ravel()):
        pending.append((float(inverted[index]), serial, int(index)))
        serial += 1
    heapq.heapify(pending)
    while pending:
        level, _, index = heapq.heappop(pending)
        row, col = divmod(index, cols)
        for neighbor in (
            index - cols if row else -1,
            index + cols if row + 1 < rows else -1,
            index - 1 if col else -1,
            index + 1 if col + 1 < cols else -1,
        ):
            if neighbor < 0 or flat[neighbor] != 0:
                continue
            flat[neighbor] = flat[index]
            heapq.heappush(
                pending, (max(level, float(inverted[neighbor])), serial, neighbor)
            )
            serial += 1
    return labels


def _segment(
    elevation: NDArray[np.float64], corridor: NDArray[np.bool_]
) -> tuple[NDArray[np.bool_], int, str | None]:
    height = ndimage.gaussian_filter(elevation, SMOOTHING_SIGMA_M / GRID_SPACING_M)
    floor = float(np.percentile(height, 10))
    relief = float(height.max() - floor)
    if relief < MIN_RELIEF_M:
        return (
            np.ones(height.shape, dtype=np.bool_), 0,
            f"flat or ambiguous terrain (less than {MIN_RELIEF_M:g} m observed relief)",
        )
    local_size = 2 * math.ceil(SUMMIT_SEPARATION_M / GRID_SPACING_M) + 1
    local_range = (
        ndimage.maximum_filter(height, size=local_size)
        - ndimage.minimum_filter(height, size=local_size)
    )
    dy, dx = np.gradient(height, GRID_SPACING_M)
    # Low, flat cells are valley-floor markers, not arbitrarily clipped search edges.
    background = (
        (height <= floor + min(20.0, 0.03 * relief))
        & (local_range <= VALLEY_RELIEF_M)
        & (np.hypot(dx, dy) <= VALLEY_SLOPE)
    )
    if not np.any(background):
        # No observable valley floor: caller must enlarge the search window.
        return np.ones(height.shape, dtype=np.bool_), 0, "no observable valley floor"
    maxima = (
        (height == ndimage.maximum_filter(height, size=local_size))
        & (height >= floor + MIN_RELIEF_M)
        & ~background
    )
    markers, count = ndimage.label(maxima, structure=np.ones((3, 3), dtype=np.int8))
    if count == 0:
        return np.ones(height.shape, dtype=np.bool_), 0, "no resolved summit markers"
    if count > MAX_SUMMIT_MARKERS:
        raise _failure("terrain is too fragmented / ambiguous for bounded summit analysis")
    markers = markers.astype(np.int32)
    markers[markers > 0] += 1
    markers[background] = 1
    labels = _watershed(height, markers)
    labels, peaks = _merge_saddles(labels, height, floor)
    touched = np.unique(labels[corridor])
    touched = touched[(touched > 1) & (peaks[touched] - floor >= MIN_RELIEF_M)]
    if len(touched) == 0:
        # A summit may still lie outside the window. Do not decide that a
        # slope is flat merely because its clipped marker is on a search edge.
        if (
            np.any(maxima[:EDGE_GUARD_CELLS])
            or np.any(maxima[-EDGE_GUARD_CELLS:])
            or np.any(maxima[:, :EDGE_GUARD_CELLS])
            or np.any(maxima[:, -EDGE_GUARD_CELLS:])
        ):
            return np.ones(height.shape, dtype=np.bool_), 0, "summit remains on a search edge"
        return (
            np.ones(height.shape, dtype=np.bool_), 0,
            "route does not touch a resolvable mountain slope",
        )
    selected = np.isin(labels, touched)
    return selected, len(touched), None


def _keys(selected: NDArray[np.bool_], grid: _Grid) -> NDArray:
    rows, cols = np.nonzero(selected)
    coordinates = np.column_stack((grid.y[rows], grid.x[cols])).astype(np.int64)
    return np.ascontiguousarray(coordinates).view(
        np.dtype([("y", np.int64), ("x", np.int64)])
    ).ravel()


def discover_auto_boundary(
    route: ProjectedRoute,
    max_distance_km: float,
    load_dem: Callable[[tuple[GeographicBounds, ...]], DemSource],
    progress: ProgressCallback = console_progress,
) -> AutoBoundaryResult:
    """Estimate all route-touched mountain regions, or fail without a fallback.

    Two nested searches must agree to within 0.5% of selected cells, have extrema
    within one cell, and have an interior boundary. The rectangle includes full boundary cells, a
    90 m safety buffer, and every route vertex (therefore every route segment).
    Loader requests include the entire discovery window; the caller may reuse
    tiles/cache through its normal DEM resolver. Inconclusive low-relief windows
    are retried up to the cap: a foothill window need not contain its summit.
    No missing elevations are filled. Reliable flat valley floors are required;
    diffuse mountain-to-plain transitions are not guaranteed exact boundaries.
    """
    if not math.isfinite(max_distance_km) or max_distance_km <= 0:
        raise _failure("maximum search distance must be finite and positive")
    points = route.points
    if points.ndim != 2 or points.shape[1] != 2 or not len(points) or not np.all(np.isfinite(points)):
        raise _failure("route has no finite projected coordinates")
    route_bounds = np.concatenate((points.min(axis=0), points.max(axis=0)))
    cap = max_distance_km * 1000.0
    # Leave a second nested window even for caps below the normal 2 km start.
    # Three quarters retains enough smoothing/flat-floor context in small caps.
    distance = min(INITIAL_SEARCH_DISTANCE_M, cap * 0.75)
    previous = None
    previous_count = None
    while True:
        bounds = route_bounds + np.array([-distance, -distance, distance, distance])
        grid = _grid(bounds)
        progress(f"Discovering estimated mountain boundaries: {distance / 1000:g} km search, "
                 f"{grid.shape[0] * grid.shape[1]:,} analysis cells")
        perimeter = np.array(
            [[bounds[0], bounds[1]], [bounds[2], bounds[1]],
             [bounds[2], bounds[3]], [bounds[0], bounds[3]]]
        )
        dem = load_dem(request_projected_bounds(perimeter, route))
        _check_resolution(dem, route, grid)
        elevation = _sample(dem, route, grid)
        selected, region_count, unresolved = _segment(elevation, _route_cells(route, grid))
        edge = (
            np.any(selected[:EDGE_GUARD_CELLS])
            or np.any(selected[-EDGE_GUARD_CELLS:])
            or np.any(selected[:, :EDGE_GUARD_CELLS])
            or np.any(selected[:, -EDGE_GUARD_CELLS:])
        )
        keys = _keys(selected, grid) if region_count else None
        stable = False
        if previous is not None and keys is not None and previous_count == region_count:
            changed = len(np.setxor1d(previous, keys, assume_unique=True))
            stable = changed <= STABILITY_FRACTION * max(len(previous), len(keys))
            for axis in ("x", "y"):
                stable = stable and (
                    abs(int(keys[axis].min()) - int(previous[axis].min())) <= 1
                    and abs(int(keys[axis].max()) - int(previous[axis].max())) <= 1
                )
        if not edge and stable and region_count > 0:
            rows, cols = np.nonzero(selected)
            margin = GRID_SPACING_M / 2 + SAFETY_BUFFER_M
            minimum = np.minimum(
                [grid.x[cols.min()] * GRID_SPACING_M - margin,
                 grid.y[rows.min()] * GRID_SPACING_M - margin], route_bounds[:2]
            )
            maximum = np.maximum(
                [grid.x[cols.max()] * GRID_SPACING_M + margin,
                 grid.y[rows.max()] * GRID_SPACING_M + margin], route_bounds[2:]
            )
            return AutoBoundaryResult(
                box(*minimum, *maximum), dem, region_count, distance
            )
        if distance >= cap:
            detail = f"{unresolved}; " if unresolved else ""
            raise _failure(
                f"{detail}mountain boundaries remain unresolved at the {max_distance_km:g} km "
                "search limit (edge-touching or unstable terrain)"
            )
        previous, previous_count = keys, region_count
        distance = min(distance * 2, cap)
