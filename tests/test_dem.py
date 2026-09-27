from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import responses
from rasterio.io import MemoryFile
from rasterio.transform import from_bounds, from_origin

from gpx2stl.dem import (
    GLOBAL_DEM_URL,
    DemSource,
    DemTile,
    GeographicBounds,
    OpenTopographyClient,
    _copernicus_geographic_bounds,
    choose_dem_type,
    load_local_dem,
)
from gpx2stl.errors import Gpx2StlError


def _geotiff() -> bytes:
    values = np.arange(16, dtype=np.float32).reshape(4, 4)
    with MemoryFile() as memory:
        with memory.open(
            driver="GTiff",
            width=4,
            height=4,
            count=1,
            dtype="float32",
            crs="EPSG:4326",
            transform=from_origin(-1.0, 1.0, 0.5, 0.5),
        ) as dataset:
            dataset.write(values, 1)
        return memory.read()


def _write_geotiff(
    path: Path,
    value: float,
    bounds: tuple[float, float, float, float],
    crs: str | None = "EPSG:4326",
) -> None:
    with MemoryFile() as memory:
        with memory.open(
            driver="GTiff",
            width=4,
            height=4,
            count=1,
            dtype="float32",
            crs=crs,
            transform=from_bounds(*bounds, 4, 4),
        ) as dataset:
            dataset.write(np.full((4, 4), value, dtype=np.float32), 1)
        path.write_bytes(memory.read())


def test_dem_selection() -> None:
    normal = (GeographicBounds(-10, 10, 1, 2),)
    polar = (GeographicBounds(70, 71, 1, 2),)
    assert choose_dem_type(normal, None) == "SRTMGL1"
    assert choose_dem_type(polar, None) == "COP30"
    assert choose_dem_type(normal, "COP90") == "COP90"


def test_copernicus_filename_provides_tile_bounds() -> None:
    path = Path("Copernicus_DSM_COG_10_S07_00_W123_00_DEM.tif")
    assert _copernicus_geographic_bounds(path) == GeographicBounds(
        -7, -6, -123, -122
    )


def test_dem_tile_bilinear_sampling() -> None:
    tile = DemTile.from_bytes(_geotiff())
    sampled = tile.sample(np.array([-0.75]), np.array([0.75]))
    assert sampled.tolist() == [0.0]


def test_local_projected_geotiff_sampling(tmp_path: Path) -> None:
    path = tmp_path / "projected.tif"
    _write_geotiff(path, 42.0, (-120_000, -120_000, 120_000, 120_000), "EPSG:3857")
    tile = DemTile.from_path(path)
    sampled = tile.sample(np.array([0.0]), np.array([0.0]))
    assert sampled.tolist() == [42.0]


def test_local_geotiff_requires_crs(tmp_path: Path) -> None:
    path = tmp_path / "missing-crs.tif"
    _write_geotiff(path, 42.0, (-1.0, -1.0, 1.0, 1.0), crs=None)
    with pytest.raises(Gpx2StlError, match="no coordinate reference system"):
        DemTile.from_path(path)


def test_local_source_requires_complete_coverage(tmp_path: Path) -> None:
    path = tmp_path / "partial.tif"
    _write_geotiff(path, 10.0, (-1.0, -1.0, 1.0, 1.0))
    source = DemSource(
        (DemTile.from_path(path),),
        require_complete_coverage=True,
        description=str(path),
    )
    with pytest.raises(Gpx2StlError, match="incomplete"):
        source.sample_lonlat(np.array([0.0, 2.0]), np.array([0.0, 0.0]))


def test_local_directory_combines_intersecting_tiles(tmp_path: Path) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    _write_geotiff(tmp_path / "west.tif", 10.0, (-1.0, -1.0, 0.0, 1.0))
    _write_geotiff(nested / "east.tiff", 20.0, (0.0, -1.0, 1.0, 1.0))
    _write_geotiff(tmp_path / "far.tif", 30.0, (20.0, 20.0, 21.0, 21.0))
    bounds = (GeographicBounds(-0.5, 0.5, -0.5, 0.5),)
    source = load_local_dem(None, tmp_path, bounds)
    assert source is not None
    assert len(source.tiles) == 2
    sampled = source.sample_lonlat(
        np.array([-0.25, 0.25]),
        np.array([0.0, 0.0]),
    )
    assert sampled.tolist() == [10.0, 20.0]


def test_explicit_local_file_ignores_directory(tmp_path: Path) -> None:
    explicit = tmp_path / "explicit.tif"
    directory = tmp_path / "tiles"
    directory.mkdir()
    _write_geotiff(explicit, 10.0, (-1.0, -1.0, 1.0, 1.0))
    _write_geotiff(directory / "tile.tif", 20.0, (-1.0, -1.0, 1.0, 1.0))
    source = load_local_dem(
        explicit,
        directory,
        (GeographicBounds(-0.5, 0.5, -0.5, 0.5),),
    )
    assert source is not None
    assert len(source.tiles) == 1
    assert source.sample_lonlat(np.array([0.0]), np.array([0.0])).tolist() == [10.0]


def test_local_tiles_support_antimeridian_bounds(tmp_path: Path) -> None:
    _write_geotiff(tmp_path / "east.tif", 10.0, (179.0, -1.0, 180.0, 1.0))
    _write_geotiff(tmp_path / "west.tif", 20.0, (-180.0, -1.0, -179.0, 1.0))
    source = load_local_dem(
        None,
        tmp_path,
        (
            GeographicBounds(-0.5, 0.5, 179.5, 180.0),
            GeographicBounds(-0.5, 0.5, -180.0, -179.5),
        ),
    )
    assert source is not None
    sampled = source.sample_lonlat(
        np.array([179.5, -179.5]),
        np.array([0.0, 0.0]),
    )
    assert sampled.tolist() == [10.0, 20.0]


@responses.activate
def test_client_requests_each_antimeridian_part() -> None:
    responses.add(responses.GET, GLOBAL_DEM_URL, body=_geotiff(), status=200)
    responses.add(responses.GET, GLOBAL_DEM_URL, body=_geotiff(), status=200)
    bounds = (
        GeographicBounds(0, 1, 179, 180),
        GeographicBounds(0, 1, -180, -179),
    )
    source = OpenTopographyClient("secret").fetch(bounds, "COP30")
    assert len(source.tiles) == 2
    assert len(responses.calls) == 2
    assert "API_Key=secret" in responses.calls[0].request.url
