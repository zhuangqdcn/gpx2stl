from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from gpx2stl.errors import Gpx2StlError
from gpx2stl.gpx import interpolate_elevations, project_paths, read_gpx
from gpx2stl.models import GeoPath


def test_reads_and_projects_all_paths(simple_gpx: Path) -> None:
    paths = read_gpx(simple_gpx)
    projected = project_paths(paths)
    assert len(paths) == 1
    assert projected.paths[0].shape == (3, 2)
    assert np.all(np.isfinite(projected.paths[0]))


def test_interpolates_only_internal_elevation_gaps() -> None:
    path = GeoPath(
        np.array([0.0, 1.0, 2.0]),
        np.array([0.0, 1.0, 2.0]),
        np.array([100.0, np.nan, 120.0]),
    )
    result = interpolate_elevations(path)
    assert result.elevation.tolist() == [100.0, 110.0, 120.0]


def test_rejects_missing_endpoint_elevation() -> None:
    path = GeoPath(
        np.array([0.0, 1.0]),
        np.array([0.0, 1.0]),
        np.array([np.nan, 100.0]),
    )
    with pytest.raises(Gpx2StlError, match="endpoint"):
        interpolate_elevations(path)


def test_antimeridian_path_stays_local() -> None:
    path = GeoPath(
        np.array([179.9, -179.9]),
        np.array([10.0, 10.0]),
        np.array([1.0, 2.0]),
    )
    projected = project_paths((path,))
    distance = np.linalg.norm(projected.paths[0][1] - projected.paths[0][0])
    assert 20_000 < distance < 25_000
