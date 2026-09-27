from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def simple_gpx(tmp_path: Path) -> Path:
    path = tmp_path / "route.gpx"
    path.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" creator="tests" xmlns="http://www.topografix.com/GPX/1/1">
  <trk><name>Test</name><trkseg>
    <trkpt lat="37.0000" lon="-122.0000"><ele>100</ele></trkpt>
    <trkpt lat="37.0010" lon="-121.9990"><ele>110</ele></trkpt>
    <trkpt lat="37.0020" lon="-121.9980"><ele>120</ele></trkpt>
  </trkseg></trk>
</gpx>
""",
        encoding="utf-8",
    )
    return path
