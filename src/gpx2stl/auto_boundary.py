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
from scipy.spatial import cKDTree
from shapely.geometry import LineString, Point, Polygon

from gpx2stl.dem import DemSource, GeographicBounds, request_projected_bounds
from gpx2stl.directions import DIRECTIONS, route_projections, support_polygon
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
class ValleyCriteria:
    max_relief_m: float = VALLEY_RELIEF_M
    max_slope_percent: float = 100 * VALLEY_SLOPE
    max_height_m: float = 20.0
    max_height_percent: float = 3.0

    def __post_init__(self) -> None:
        for name in (
            "max_relief_m", "max_slope_percent", "max_height_m", "max_height_percent"
        ):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value < 0
            ):
                raise Gpx2StlError(
                    f"Automatic valley {name} must be a finite nonnegative number."
                )
        if self.max_height_percent > 100:
            raise Gpx2StlError(
                "Automatic valley max_height_percent must not exceed 100."
            )


@dataclass(frozen=True)
class DirectionalBoundary:
    direction: str
    resolved: bool
    reason: str
    support_limit: float
    padding_m: float
    span_m: float
    used_zero_span_basis: bool = False


@dataclass(frozen=True)
class AutoBoundaryResult:
    envelope: Polygon
    dem: DemSource
    region_count: int
    search_distance_m: float
    directions: tuple[DirectionalBoundary, ...] = ()


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


def _valley_background(
    height: NDArray[np.float64],
    floor: float,
    relief: float,
    criteria: ValleyCriteria,
    local_size: int,
) -> NDArray[np.bool_]:
    local_range = (
        ndimage.maximum_filter(height, size=local_size)
        - ndimage.minimum_filter(height, size=local_size)
    )
    dy, dx = np.gradient(height, GRID_SPACING_M)
    # Low, flat cells are valley-floor markers, not arbitrarily clipped search edges.
    return (
        (height <= floor + min(
            criteria.max_height_m, criteria.max_height_percent / 100 * relief
        ))
        & (local_range <= criteria.max_relief_m)
        & (np.hypot(dx, dy) <= criteria.max_slope_percent / 100)
    )


def _segment(
    elevation: NDArray[np.float64],
    corridor: NDArray[np.bool_],
    criteria: ValleyCriteria = ValleyCriteria(),
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
    background = _valley_background(height, floor, relief, criteria, local_size)
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


@dataclass(frozen=True)
class _DirectionalEvidence:
    support: float
    keys: NDArray
    interior: bool
    grid_bounds: tuple[int, int, int, int] | None = None


def _directional_evidence(
    selected: NDArray[np.bool_], grid: _Grid
) -> tuple[_DirectionalEvidence, ...]:
    rows, cols = np.nonzero(selected)
    if not len(rows):
        raise _failure("selected mountain regions contain no analysis cells")
    centers = np.column_stack((grid.x[cols], grid.y[rows])) * GRID_SPACING_M
    keys = _keys(selected, grid)
    evidence = []
    for direction in DIRECTIONS:
        ux, uy = direction.vector
        projected = centers @ np.asarray(direction.vector)
        support = float(projected.max())
        strip = projected >= support - 2 * EDGE_GUARD_CELLS * GRID_SPACING_M
        outward_edge = (
            ((rows >= len(grid.y) - EDGE_GUARD_CELLS) & (uy > 0))
            | ((rows < EDGE_GUARD_CELLS) & (uy < 0))
            | ((cols >= len(grid.x) - EDGE_GUARD_CELLS) & (ux > 0))
            | ((cols < EDGE_GUARD_CELLS) & (ux < 0))
        )
        grid_bounds = (int(grid.x[0]), int(grid.y[0]), int(grid.x[-1]), int(grid.y[-1]))
        evidence.append(_DirectionalEvidence(support, keys, not np.any(outward_edge & strip), grid_bounds))
    return tuple(evidence)


def _direction_stable(previous: _DirectionalEvidence, current: _DirectionalEvidence, vector) -> bool:
    tolerance = GRID_SPACING_M * sum(abs(value) for value in vector)
    if not current.interior or abs(previous.support - current.support) > tolerance:
        return False
    # Compare the same spatial strip in both windows, not watershed label IDs.
    threshold = min(previous.support, current.support) - 2 * EDGE_GUARD_CELLS * GRID_SPACING_M
    ux, uy = vector

    def strip(keys):
        projections = (keys["x"] * ux + keys["y"] * uy) * GRID_SPACING_M
        inside = projections >= threshold
        # Tangential growth into newly sampled terrain must not invalidate an
        # unchanged directional boundary. Reassignment in shared coverage does.
        for bounds in (previous.grid_bounds, current.grid_bounds):
            if bounds is not None:
                inside &= (
                    (keys["x"] >= bounds[0]) & (keys["y"] >= bounds[1])
                    & (keys["x"] <= bounds[2]) & (keys["y"] <= bounds[3])
                )
        return keys[inside]

    first, second = strip(previous.keys), strip(current.keys)
    if not len(first) or not len(second):
        return False
    # The support tolerance permits one-cell contour jitter, not disappearance
    # or reassignment of a spatial patch farther from the previous boundary.
    def coordinates(keys):
        return np.column_stack((keys["x"], keys["y"]))

    a, b = coordinates(first), coordinates(second)
    changed = (
        np.count_nonzero(cKDTree(a).query(b, p=np.inf)[0] > 1)
        + np.count_nonzero(cKDTree(b).query(a, p=np.inf)[0] > 1)
    )
    return changed <= STABILITY_FRACTION * max(len(first), len(second))


def _finish(
    route: ProjectedRoute, dem: DemSource, region_count: int, distance: float,
    evidence: tuple[_DirectionalEvidence, ...] | None, resolved: list[bool],
    reasons: list[str], progress: ProgressCallback,
) -> AutoBoundaryResult:
    maxima, spans = route_projections(route.points)
    largest_span = float(spans.max())
    results = []
    for index, direction in enumerate(DIRECTIONS):
        substituted = False
        basis = float(spans[index])
        if resolved[index]:
            assert evidence is not None
            cell_extent = GRID_SPACING_M / 2 * sum(abs(value) for value in direction.vector)
            limit = max(float(maxima[index]), evidence[index].support + cell_extent + SAFETY_BUFFER_M)
            padding = limit - float(maxima[index])
            percentage = f"{100 * padding / basis:.1f}%" if basis else "percentage undefined: zero route span"
            progress(f"{direction.name}: valley boundary found; padding {padding:.1f} m ({percentage})")
        else:
            if not basis:
                if not largest_span:
                    raise _failure("all eight projected route spans are zero; fallback requires a nondegenerate route")
                basis = largest_span
                substituted = True
                progress(f"Warning: {direction.name} has zero projected route span; "
                         f"using largest nonzero eight-direction span {basis:.1f} m")
            padding = basis
            limit = float(maxima[index]) + padding
            progress(f"Warning: {direction.name}: valley boundary not found; using 100% fallback "
                     f"{padding:.1f} m; reason: {reasons[index]}; "
                     f"distance basis: {'largest nonzero eight-direction' if substituted else 'route projected'} "
                     f"span {basis:.1f} m")
        results.append(DirectionalBoundary(direction.name, resolved[index], reasons[index],
                                           limit, padding, basis, substituted))
    envelope = support_polygon(np.asarray([item.support_limit for item in results]), route.points[0])
    for path in route.paths:
        geometry = LineString(path) if len(path) > 1 else Point(path[0])
        if not envelope.covers(geometry):
            raise _failure("directional polygon does not contain the complete route")
    if not all(resolved):
        progress("Warning: unresolved directional 100% fallback limits take priority and may crop "
                 "terrain in adjacent detected directions; complete mountain coverage is not guaranteed. "
                 "Selection padding is not final printable-footprint padding.")
    return AutoBoundaryResult(envelope, dem, region_count, distance, tuple(results))


def discover_auto_boundary(
    route: ProjectedRoute,
    max_distance_km: float,
    load_dem: Callable[[tuple[GeographicBounds, ...]], DemSource],
    progress: ProgressCallback = console_progress,
    *,
    valley_criteria: ValleyCriteria = ValleyCriteria(),
) -> AutoBoundaryResult:
    """Estimate eight independent terrain supports, falling back only at the cap.

    Reliable directional strips must be interior and spatially stable between
    nested windows. Detected supports include whole cells and a 90 m margin.
    Unresolved supports use 100% of the route's directional span. Missing DEM,
    inadequate resolution, and resource/geometry failures remain hard errors.
    """
    if not math.isfinite(max_distance_km) or max_distance_km <= 0:
        raise _failure("maximum search distance must be finite and positive")
    points = route.points
    if points.ndim != 2 or points.shape[1] != 2 or not len(points) or not np.all(np.isfinite(points)):
        raise _failure("route has no finite projected coordinates")
    route_bounds = np.concatenate((points.min(axis=0), points.max(axis=0)))
    progress(
        f"Automatic valley criteria: local variation <= {valley_criteria.max_relief_m:g} m "
        f"over 900 m; slope <= {valley_criteria.max_slope_percent:g}%; "
        f"height above window's 10th-percentile floor <= min("
        f"{valley_criteria.max_height_m:g} m, "
        f"{valley_criteria.max_height_percent:g}% of observed relief)"
    )
    cap = max_distance_km * 1000.0
    # Leave a second nested window even for caps below the normal 2 km start.
    # Three quarters retains enough smoothing/flat-floor context in small caps.
    distance = min(INITIAL_SEARCH_DISTANCE_M, cap * 0.75)
    previous = None
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
        selected, region_count, unresolved = _segment(
            elevation, _route_cells(route, grid), valley_criteria
        )
        # A placeholder all-ones mask has no detected boundary evidence.
        evidence = _directional_evidence(selected, grid) if region_count else None
        resolved = []
        reasons = []
        for index, direction in enumerate(DIRECTIONS):
            found = False
            if evidence is None:
                reason = unresolved or "no usable route-associated mountain regions"
                status = f"unresolved: {reason}"
            elif not evidence[index].interior:
                reason = "directional boundary touches the search-edge guard"
                status = f"unresolved: {reason}"
            elif previous is not None and _direction_stable(previous[index], evidence[index], direction.vector):
                found = True
                reason = "interior directional boundary and selected geometry stable between nested windows"
                status = "valley boundary found and stable"
            else:
                reason = "directional boundary awaiting spatial stability between nested windows"
                status = f"candidate: {reason}"
            resolved.append(found)
            reasons.append(reason)
            progress(f"Search {distance / 1000:g} km {direction.name}: {status}")
        if all(resolved) or distance >= cap:
            return _finish(route, dem, region_count, distance, evidence, resolved, reasons, progress)
        previous = evidence
        distance = min(distance * 2, cap)
