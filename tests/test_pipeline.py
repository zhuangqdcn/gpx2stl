from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import rasterio
import trimesh
from rasterio.transform import from_bounds
from shapely.geometry import Polygon

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
    messages: list[str] = []
    convert(
        Config(
            gpx_file=simple_gpx,
            output=output,
            topo_source="local",
            topo_file=topo_file,
            topo_dir=tmp_path / "asset",
            max_size=20.0,
        ),
        progress=messages.append,
    )
    assert output.is_file()
    assert any(message.startswith("Generated topography mesh") for message in messages)


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
    assert any(message.startswith("Reading activity paths") for message in messages)
    assert any(message.startswith("Loaded 1 path") for message in messages)
    assert any(message.startswith("Created square footprint") for message in messages)
    assert "Topography disabled; generating a flat base" in messages
    assert "Generating watertight base, topography, and route meshes" in messages
    assert any(message.startswith("Generated base mesh") for message in messages)
    assert not any(message.startswith("Generated topography mesh") for message in messages)
    assert "Writing 2-object, 4-material 3MF package" in messages
    assert "Validated 3MF mesh and material resources" in messages
    assert messages[-1] == f"Finished writing {output}"


def test_fit_conversion_generates_an_independent_model(
    simple_fit: Path, tmp_path: Path
) -> None:
    output = tmp_path / "fit-route.3mf"
    messages: list[str] = []
    convert(
        Config(
            gpx_file=simple_fit,
            output=output,
            topo=False,
            max_size=20.0,
        ),
        progress=messages.append,
    )
    assert output.is_file()
    assert any(message.startswith("Loaded 2 paths") for message in messages)


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
    assert "Writing 3-object, 4-material 3MF package" in messages


def test_conversion_preserves_custom_stl_dimensions(
    simple_gpx: Path, tmp_path: Path
) -> None:
    base = tmp_path / "base.stl"
    trimesh.creation.extrude_polygon(
        Polygon([(-30, -20), (30, -20), (30, 20), (-30, 20)]),
        5.0,
    ).export(base, file_type="stl")
    output = tmp_path / "custom.3mf"
    messages: list[str] = []
    convert(
        Config(
            gpx_file=simple_gpx,
            output=output,
            topo=False,
            base_stl=base,
            max_size=999.0,
        ),
        progress=messages.append,
    )
    assert output.is_file()
    assert "Using custom 60.0 x 40.0 mm base with top Z 5.0 mm" in messages
    assert any(message.startswith("Fitted route at ") for message in messages)


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
