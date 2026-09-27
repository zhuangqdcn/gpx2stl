from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import gpxpy
import numpy as np
from pyproj import CRS, Transformer

from gpx2stl.errors import Gpx2StlError
from gpx2stl.models import GeoPath, ProjectedRoute


def _path_from_points(points: Iterable[object]) -> GeoPath | None:
    rows: list[tuple[float, float, float]] = []
    previous: tuple[float, float] | None = None
    for point in points:
        longitude = float(point.longitude)  # type: ignore[attr-defined]
        latitude = float(point.latitude)  # type: ignore[attr-defined]
        key = (longitude, latitude)
        if key == previous:
            continue
        elevation_value = point.elevation  # type: ignore[attr-defined]
        elevation = float(elevation_value) if elevation_value is not None else np.nan
        rows.append((longitude, latitude, elevation))
        previous = key
    if len(rows) < 2:
        return None
    values = np.asarray(rows, dtype=np.float64)
    return GeoPath(values[:, 0], values[:, 1], values[:, 2])


def read_gpx(path: Path) -> tuple[GeoPath, ...]:
    try:
        with path.open("r", encoding="utf-8-sig") as stream:
            document = gpxpy.parse(stream)
    except (OSError, gpxpy.gpx.GPXException) as exc:
        raise Gpx2StlError(f"Unable to read GPX file '{path}': {exc}") from exc

    paths: list[GeoPath] = []
    for track in document.tracks:
        for segment in track.segments:
            parsed = _path_from_points(segment.points)
            if parsed is not None:
                paths.append(parsed)
    for route in document.routes:
        parsed = _path_from_points(route.points)
        if parsed is not None:
            paths.append(parsed)
    if not paths:
        raise Gpx2StlError("The GPX file contains no path with at least two distinct points.")
    return tuple(paths)


def interpolate_elevations(path: GeoPath) -> GeoPath:
    elevation = path.elevation.copy()
    valid = np.flatnonzero(np.isfinite(elevation))
    if len(valid) == 0:
        raise Gpx2StlError("A GPX path has no elevation values and cannot be used without topo.")
    if valid[0] != 0 or valid[-1] != len(elevation) - 1:
        raise Gpx2StlError(
            "A GPX path is missing an endpoint elevation; only internal gaps can be interpolated."
        )
    missing = np.flatnonzero(~np.isfinite(elevation))
    if len(missing):
        elevation[missing] = np.interp(missing, valid, elevation[valid])
    return GeoPath(path.longitude, path.latitude, elevation)


def _continuous_longitudes(paths: tuple[GeoPath, ...]) -> tuple[GeoPath, ...]:
    all_longitudes = np.concatenate([path.longitude for path in paths])
    center = float(np.rad2deg(np.angle(np.mean(np.exp(1j * np.deg2rad(all_longitudes))))))
    normalized: list[GeoPath] = []
    for path in paths:
        longitude = center + ((path.longitude - center + 180.0) % 360.0) - 180.0
        normalized.append(GeoPath(longitude, path.latitude, path.elevation))
    return tuple(normalized)


def project_paths(paths: tuple[GeoPath, ...]) -> ProjectedRoute:
    normalized = _continuous_longitudes(paths)
    longitude = np.concatenate([path.longitude for path in normalized])
    latitude = np.concatenate([path.latitude for path in normalized])
    center_lon = float(np.mean(longitude))
    center_lat = float(np.mean(latitude))
    local = CRS.from_proj4(
        f"+proj=aeqd +lat_0={center_lat:.12f} +lon_0={center_lon:.12f} "
        "+datum=WGS84 +units=m +no_defs"
    )
    forward = Transformer.from_crs("EPSG:4326", local, always_xy=True)
    inverse = Transformer.from_crs(local, "EPSG:4326", always_xy=True)
    projected: list[np.ndarray] = []
    elevations: list[np.ndarray] = []
    for path in normalized:
        x, y = forward.transform(path.longitude, path.latitude)
        projected.append(np.column_stack((x, y)).astype(np.float64))
        elevations.append(path.elevation.astype(np.float64))
    return ProjectedRoute(tuple(projected), tuple(elevations), forward, inverse)
