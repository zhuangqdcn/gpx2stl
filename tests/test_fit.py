from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from gpx2stl.activity import read_activity
from gpx2stl.errors import Gpx2StlError
from gpx2stl.fit import read_fit
from gpx2stl.gpx import project_paths


def test_reads_fit_records_as_separate_gps_paths(simple_fit: Path) -> None:
    paths = read_fit(simple_fit)

    assert len(paths) == 2
    assert paths[0].longitude.tolist() == pytest.approx([-122.0, -121.999])
    assert paths[0].latitude.tolist() == pytest.approx([37.0, 37.001])
    assert paths[0].elevation.tolist() == pytest.approx([100.0, 110.0])
    assert paths[1].elevation.tolist() == pytest.approx([120.0, 130.0])
    projected = project_paths(paths)
    assert all(np.all(np.isfinite(path)) for path in projected.paths)


def test_activity_reader_dispatches_fit_files(simple_fit: Path) -> None:
    assert len(read_activity(simple_fit)) == 2


def test_timer_stop_event_separates_fit_paths(timer_paused_fit: Path) -> None:
    paths = read_fit(timer_paused_fit)
    assert len(paths) == 2
    assert paths[0].latitude.tolist() == pytest.approx([37.0, 37.001])
    assert paths[1].latitude.tolist() == pytest.approx([38.0, 38.001])


def test_fit_reader_rejects_corrupt_checksum(simple_fit: Path) -> None:
    contents = bytearray(simple_fit.read_bytes())
    contents[-1] ^= 0xFF
    simple_fit.write_bytes(contents)

    with pytest.raises(Gpx2StlError, match="Unable to read FIT"):
        read_fit(simple_fit)


def test_activity_reader_rejects_unsupported_extension(tmp_path: Path) -> None:
    path = tmp_path / "route.tcx"
    path.touch()
    with pytest.raises(Gpx2StlError, match=r"expected \.gpx or \.fit"):
        read_activity(path)
