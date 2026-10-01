from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_bounds

import gpx2stl.pipeline
from gpx2stl.dem import DemSource, GeographicBounds
from gpx2stl.errors import Gpx2StlError
from gpx2stl.models import Config, TopoSource
from gpx2stl.pipeline import convert, resolve_dem


def _config(tmp_path: Path, source: TopoSource) -> Config:
    return Config(
        gpx_file=tmp_path / "route.gpx",
        output=tmp_path / "route.3mf",
        topo_source=source,
        topo_dir=tmp_path / "asset",
    )


def test_conversion_uses_local_file_without_network(
    simple_gpx: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    topo_file = tmp_path / "terrain.tif"
    with rasterio.open(
        topo_file,
        "w",
        driver="GTiff",
        width=8,
        height=8,
        count=1,
        dtype="float32",
        crs="EPSG:4326",
        transform=from_bounds(-123.0, 36.0, -121.0, 38.0, 8, 8),
    ) as dataset:
        dataset.write(np.arange(64, dtype=np.float32).reshape(8, 8), 1)

    def fail_online(*args, **kwargs):
        raise AssertionError("OpenTopography must not be called for local terrain")

    monkeypatch.setattr(gpx2stl.pipeline.OpenTopographyClient, "fetch", fail_online)
    output = tmp_path / "local.3mf"
    convert(
        Config(
            gpx_file=simple_gpx,
            output=output,
            topo_source="local",
            topo_file=topo_file,
            topo_dir=tmp_path / "asset",
            max_size=20.0,
        )
    )
    assert output.is_file()


def test_conversion_reports_meaningful_progress(
    simple_gpx: Path, tmp_path: Path
) -> None:
    output = tmp_path / "progress.3mf"
    messages: list[str] = []
    convert(
        Config(
            gpx_file=simple_gpx,
            output=output,
            topo=False,
            max_size=20.0,
        ),
        progress=messages.append,
    )
    assert output.is_file()
    assert any(message.startswith("Reading GPX paths") for message in messages)
    assert any(message.startswith("Loaded 1 path") for message in messages)
    assert any(message.startswith("Created square footprint") for message in messages)
    assert "Topography disabled; generating a flat base" in messages
    assert "Generating watertight terrain/base and route meshes" in messages
    assert "Writing 2-material 3MF package" in messages
    assert "Validated 3MF mesh and material resources" in messages
    assert messages[-1] == f"Finished writing {output}"


def test_text_conversion_uses_circular_inset_and_three_materials(
    simple_gpx: Path, tmp_path: Path
) -> None:
    output = tmp_path / "text.3mf"
    messages: list[str] = []
    convert(
        Config(
            gpx_file=simple_gpx,
            output=output,
            topo=False,
            shape="hex",
            text="TRAIL",
            max_size=20.0,
        ),
        progress=messages.append,
    )
    assert output.is_file()
    assert any(
        message.startswith("Created hex frame with 14.0 mm circular terrain inset")
        for message in messages
    )
    assert any(message.startswith("Generated text mesh") for message in messages)
    assert "Writing 3-material 3MF package" in messages


def test_auto_prefers_local_without_api_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected = DemSource(())
    monkeypatch.setattr(gpx2stl.pipeline, "load_local_dem", lambda *args: expected)
    monkeypatch.setattr(gpx2stl.pipeline, "dem_covers_bounds", lambda *args: True)
    assert resolve_dem(
        _config(tmp_path, "auto"),
        (GeographicBounds(0, 1, 0, 1),),
    ) is expected


def test_local_fails_without_intersecting_raster(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gpx2stl.pipeline, "load_local_dem", lambda *args: None)
    monkeypatch.setattr(
        gpx2stl.pipeline, "cache_copernicus_tiles", lambda *args: False
    )
    with pytest.raises(Gpx2StlError, match="No local GeoTIFF"):
        resolve_dem(
            _config(tmp_path, "local"),
            (GeographicBounds(0, 1, 0, 1),),
        )


def test_auto_without_local_or_key_reports_online_requirement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gpx2stl.pipeline, "load_local_dem", lambda *args: None)
    monkeypatch.setattr(
        gpx2stl.pipeline, "cache_copernicus_tiles", lambda *args: False
    )
    with pytest.raises(Gpx2StlError, match="requires --api-key"):
        resolve_dem(
            _config(tmp_path, "auto"),
            (GeographicBounds(0, 1, 0, 1),),
        )
