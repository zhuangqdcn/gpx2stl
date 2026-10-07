from __future__ import annotations

from pathlib import Path

from gpx2stl.errors import Gpx2StlError
from gpx2stl.fit import read_fit
from gpx2stl.gpx import read_gpx
from gpx2stl.models import GeoPath

ACTIVITY_EXTENSIONS = {".fit", ".gpx"}


def read_activity(path: Path) -> tuple[GeoPath, ...]:
    suffix = path.suffix.lower()
    if suffix == ".gpx":
        return read_gpx(path)
    if suffix == ".fit":
        return read_fit(path)
    raise Gpx2StlError(
        f"Unsupported activity file extension '{path.suffix}'; expected .gpx or .fit."
    )
