from __future__ import annotations

import math
from pathlib import Path

import fitdecode
import numpy as np

from gpx2stl.errors import Gpx2StlError
from gpx2stl.models import GeoPath

SEMICIRCLES_TO_DEGREES = 180.0 / 2**31


def _path_from_rows(rows: list[tuple[float, float, float]]) -> GeoPath | None:
    if len(rows) < 2:
        return None
    values = np.asarray(rows, dtype=np.float64)
    return GeoPath(values[:, 0], values[:, 1], values[:, 2])


def read_fit(path: Path) -> tuple[GeoPath, ...]:
    paths: list[GeoPath] = []
    rows: list[tuple[float, float, float]] = []
    previous: tuple[float, float] | None = None

    def finish_path() -> None:
        nonlocal rows, previous
        parsed = _path_from_rows(rows)
        if parsed is not None:
            paths.append(parsed)
        rows = []
        previous = None

    try:
        with fitdecode.FitReader(
            path,
            check_crc=fitdecode.CrcCheck.RAISE,
            error_handling=fitdecode.ErrorHandling.RAISE,
        ) as reader:
            for frame in reader:
                if not isinstance(frame, fitdecode.FitDataMessage):
                    continue
                if frame.name in {"file_id", "session"}:
                    finish_path()
                    continue
                if frame.name == "event":
                    event = frame.get_value("event", fallback=None)
                    event_type = frame.get_value("event_type", fallback=None)
                    if event == "timer" and str(event_type).startswith("stop"):
                        finish_path()
                    continue
                if frame.name != "record":
                    continue
                latitude_value = frame.get_value("position_lat", fallback=None)
                longitude_value = frame.get_value("position_long", fallback=None)
                if latitude_value is None or longitude_value is None:
                    finish_path()
                    continue
                latitude = float(latitude_value) * SEMICIRCLES_TO_DEGREES
                longitude = float(longitude_value) * SEMICIRCLES_TO_DEGREES
                if (
                    not math.isfinite(latitude)
                    or not math.isfinite(longitude)
                    or not -90.0 <= latitude <= 90.0
                    or not -180.0 <= longitude <= 180.0
                ):
                    finish_path()
                    continue
                key = (longitude, latitude)
                if key == previous:
                    continue
                elevation_value = frame.get_value(
                    "enhanced_altitude",
                    fallback=None,
                )
                if elevation_value is None:
                    elevation_value = frame.get_value("altitude", fallback=None)
                elevation = (
                    float(elevation_value)
                    if elevation_value is not None
                    else np.nan
                )
                rows.append((longitude, latitude, elevation))
                previous = key
    except (OSError, fitdecode.FitError) as exc:
        raise Gpx2StlError(f"Unable to read FIT file '{path}': {exc}") from exc

    finish_path()
    if not paths:
        raise Gpx2StlError(
            "The FIT file contains no path with at least two distinct GPS points."
        )
    return tuple(paths)
