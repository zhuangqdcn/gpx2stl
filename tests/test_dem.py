from __future__ import annotations

import numpy as np
import responses
from rasterio.io import MemoryFile
from rasterio.transform import from_origin

from gpx2stl.dem import (
    GLOBAL_DEM_URL,
    DemTile,
    GeographicBounds,
    OpenTopographyClient,
    choose_dem_type,
)


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


def test_dem_selection() -> None:
    normal = (GeographicBounds(-10, 10, 1, 2),)
    polar = (GeographicBounds(70, 71, 1, 2),)
    assert choose_dem_type(normal, None) == "SRTMGL1"
    assert choose_dem_type(polar, None) == "COP30"
    assert choose_dem_type(normal, "COP90") == "COP90"


def test_dem_tile_bilinear_sampling() -> None:
    tile = DemTile.from_bytes(_geotiff())
    sampled = tile.sample(np.array([-0.75]), np.array([0.75]))
    assert sampled.tolist() == [0.0]


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
