from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import rasterio
import requests
from numpy.typing import NDArray
from pyproj import CRS, Transformer
from rasterio.io import MemoryFile
from rasterio.warp import transform_bounds
from scipy.ndimage import distance_transform_edt

from gpx2stl.errors import Gpx2StlError
from gpx2stl.models import Footprint, ProjectedRoute

GLOBAL_DEM_URL = "https://portal.opentopography.org/API/globaldem"
SUPPORTED_DEM_TYPES = {
    "SRTMGL3",
    "SRTMGL1",
    "SRTMGL1_E",
    "AW3D30",
    "AW3D30_E",
    "SRTM15Plus",
    "NASADEM",
    "COP30",
    "COP90",
    "EU_DTM",
    "GEDI_L3",
    "GEBCOIceTopo",
    "GEBCOSubIceTopo",
    "CA_MRDEM_DTM",
    "CA_MRDEM_DSM",
    "ANADEM",
    "GEDTM30",
}


@dataclass(frozen=True)
class GeographicBounds:
    south: float
    north: float
    west: float
    east: float


@dataclass(frozen=True)
class DemTile:
    data: NDArray[np.float64]
    transform: rasterio.Affine
    bounds: rasterio.coords.BoundingBox
    crs: CRS
    source: str

    @classmethod
    def from_bytes(cls, content: bytes) -> DemTile:
        try:
            with MemoryFile(content) as memory, memory.open() as dataset:
                if dataset.crs is None or CRS.from_user_input(dataset.crs).to_epsg() != 4326:
                    raise Gpx2StlError("OpenTopography returned a DEM that is not WGS84.")
                return cls._from_dataset(dataset, "OpenTopography response")
        except (rasterio.errors.RasterioError, ValueError) as exc:
            if isinstance(exc, Gpx2StlError):
                raise
            raise Gpx2StlError("OpenTopography returned an invalid GeoTIFF.") from exc

    @classmethod
    def from_path(cls, path: Path) -> DemTile:
        try:
            with rasterio.open(path) as dataset:
                return cls._from_dataset(dataset, str(path))
        except Gpx2StlError:
            raise
        except (OSError, rasterio.errors.RasterioError, ValueError) as exc:
            raise Gpx2StlError(f"Unable to read local DEM '{path}': {exc}") from exc

    @classmethod
    def _from_dataset(cls, dataset, source: str) -> DemTile:
        if dataset.count < 1:
            raise Gpx2StlError(f"DEM '{source}' has no elevation band.")
        if dataset.crs is None:
            raise Gpx2StlError(f"DEM '{source}' has no coordinate reference system.")
        if dataset.transform.is_identity:
            raise Gpx2StlError(f"DEM '{source}' has no usable geotransform.")
        band = dataset.read(1, masked=True).astype(np.float64)
        data = np.asarray(band.filled(np.nan), dtype=np.float64)
        if not np.any(np.isfinite(data)):
            raise Gpx2StlError(f"DEM '{source}' contains no finite elevation values.")
        return cls(
            data=data,
            transform=dataset.transform,
            bounds=dataset.bounds,
            crs=CRS.from_user_input(dataset.crs),
            source=source,
        )

    def _pixel_coordinates(
        self, longitude: NDArray[np.float64], latitude: NDArray[np.float64]
    ) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
        transformer = Transformer.from_crs("EPSG:4326", self.crs, always_xy=True)
        x, y = transformer.transform(longitude, latitude)
        inverse = ~self.transform
        column, row = inverse @ (x, y)
        return (
            np.asarray(column, dtype=np.float64),
            np.asarray(row, dtype=np.float64),
        )

    def covers(
        self, longitude: NDArray[np.float64], latitude: NDArray[np.float64]
    ) -> NDArray[np.bool_]:
        column, row = self._pixel_coordinates(longitude, latitude)
        tolerance = 1e-7
        return (
            np.isfinite(column)
            & np.isfinite(row)
            & (column >= -tolerance)
            & (row >= -tolerance)
            & (column <= self.data.shape[1] + tolerance)
            & (row <= self.data.shape[0] + tolerance)
        )

    def sample(
        self, longitude: NDArray[np.float64], latitude: NDArray[np.float64]
    ) -> NDArray[np.float64]:
        column, row = self._pixel_coordinates(longitude, latitude)
        covered = self.covers(longitude, latitude)
        result = np.full(longitude.shape, np.nan, dtype=np.float64)
        if not np.any(covered):
            return result
        sample_column = np.clip(column[covered] - 0.5, 0.0, self.data.shape[1] - 1.0)
        sample_row = np.clip(row[covered] - 0.5, 0.0, self.data.shape[0] - 1.0)
        c0 = np.floor(sample_column).astype(int)
        r0 = np.floor(sample_row).astype(int)
        c1 = np.minimum(c0 + 1, self.data.shape[1] - 1)
        r1 = np.minimum(r0 + 1, self.data.shape[0] - 1)
        dx = sample_column - c0
        dy = sample_row - r0
        values = np.column_stack(
            (
                self.data[r0, c0],
                self.data[r0, c1],
                self.data[r1, c0],
                self.data[r1, c1],
            )
        )
        weights = np.column_stack(
            ((1 - dx) * (1 - dy), dx * (1 - dy), (1 - dx) * dy, dx * dy)
        )
        complete = np.all(np.isfinite(values), axis=1)
        sampled = np.full(len(c0), np.nan, dtype=np.float64)
        sampled[complete] = np.sum(values[complete] * weights[complete], axis=1)
        result[covered] = sampled
        return result


@dataclass(frozen=True)
class DemSource:
    tiles: tuple[DemTile, ...]
    require_complete_coverage: bool = False
    description: str = "DEM"

    def sample_lonlat(
        self, longitude: NDArray[np.float64], latitude: NDArray[np.float64]
    ) -> NDArray[np.float64]:
        result = np.full(longitude.shape, np.nan, dtype=np.float64)
        covered = np.zeros(longitude.shape, dtype=np.bool_)
        for tile in self.tiles:
            adjusted = ((longitude + 180.0) % 360.0) - 180.0
            tile_coverage = tile.covers(adjusted, latitude)
            covered |= tile_coverage
            inside = np.isnan(result) & tile_coverage
            if np.any(inside):
                result[inside] = tile.sample(adjusted[inside], latitude[inside])
        if self.require_complete_coverage and not np.all(covered):
            missing = int(np.count_nonzero(~covered))
            raise Gpx2StlError(
                f"Local DEM coverage is incomplete for the printable footprint "
                f"({missing} sampled locations are outside {self.description})."
            )
        return result

    def sample_projected(
        self, points: NDArray[np.float64], route: ProjectedRoute
    ) -> NDArray[np.float64]:
        longitude, latitude = route.inverse.transform(points[:, 0], points[:, 1])
        return self.sample_lonlat(
            np.asarray(longitude, dtype=np.float64),
            np.asarray(latitude, dtype=np.float64),
        )


def _footprint_perimeter(footprint: Footprint, samples: int = 180) -> NDArray[np.float64]:
    if footprint.shape == "circle":
        angle = np.linspace(0.0, 2.0 * math.pi, samples, endpoint=False)
        return footprint.center + footprint.radius * np.column_stack((np.cos(angle), np.sin(angle)))
    minimum = footprint.min_xy
    maximum = footprint.max_xy
    return np.array(
        [
            [minimum[0], minimum[1]],
            [minimum[0], maximum[1]],
            [maximum[0], minimum[1]],
            [maximum[0], maximum[1]],
        ],
        dtype=np.float64,
    )


def request_bounds(footprint: Footprint, route: ProjectedRoute) -> tuple[GeographicBounds, ...]:
    perimeter = _footprint_perimeter(footprint)
    longitude, latitude = route.inverse.transform(perimeter[:, 0], perimeter[:, 1])
    longitude = np.asarray(longitude, dtype=np.float64)
    latitude = np.asarray(latitude, dtype=np.float64)
    center = float(np.rad2deg(np.angle(np.mean(np.exp(1j * np.deg2rad(longitude))))))
    continuous = center + ((longitude - center + 180.0) % 360.0) - 180.0
    west = float(np.min(continuous))
    east = float(np.max(continuous))
    south = max(-90.0, float(np.min(latitude)))
    north = min(90.0, float(np.max(latitude)))
    if east - west >= 360.0:
        return (GeographicBounds(south, north, -180.0, 180.0),)
    if west < -180.0:
        return (
            GeographicBounds(south, north, west + 360.0, 180.0),
            GeographicBounds(south, north, -180.0, east),
        )
    if east > 180.0:
        return (
            GeographicBounds(south, north, west, 180.0),
            GeographicBounds(south, north, -180.0, east - 360.0),
        )
    return (GeographicBounds(south, north, west, east),)


def iter_local_geotiffs(directory: Path) -> tuple[Path, ...]:
    if not directory.is_dir():
        return ()
    return tuple(
        sorted(
            (
                path
                for path in directory.rglob("*")
                if path.is_file() and path.suffix.lower() in {".tif", ".tiff"}
            ),
            key=lambda path: str(path).casefold(),
        )
    )


def _raster_geographic_bounds(path: Path) -> GeographicBounds:
    try:
        with rasterio.open(path) as dataset:
            if dataset.crs is None:
                raise Gpx2StlError(f"DEM '{path}' has no coordinate reference system.")
            if dataset.transform.is_identity:
                raise Gpx2StlError(f"DEM '{path}' has no usable geotransform.")
            west, south, east, north = transform_bounds(
                dataset.crs,
                "EPSG:4326",
                *dataset.bounds,
                densify_pts=21,
            )
            return GeographicBounds(south, north, west, east)
    except Gpx2StlError:
        raise
    except (OSError, rasterio.errors.RasterioError, ValueError) as exc:
        raise Gpx2StlError(f"Unable to inspect local DEM '{path}': {exc}") from exc


def _bounds_intersect(first: GeographicBounds, second: GeographicBounds) -> bool:
    return not (
        first.east < second.west
        or first.west > second.east
        or first.north < second.south
        or first.south > second.north
    )


def load_local_dem(
    topo_file: Path | None,
    topo_dir: Path,
    bounds: tuple[GeographicBounds, ...],
) -> DemSource | None:
    if topo_file is not None:
        return DemSource(
            (DemTile.from_path(topo_file),),
            require_complete_coverage=True,
            description=str(topo_file),
        )

    selected: list[Path] = []
    for path in iter_local_geotiffs(topo_dir):
        raster_bounds = _raster_geographic_bounds(path)
        if any(_bounds_intersect(raster_bounds, item) for item in bounds):
            selected.append(path)
    if not selected:
        return None
    return DemSource(
        tuple(DemTile.from_path(path) for path in selected),
        require_complete_coverage=True,
        description=str(topo_dir),
    )


def choose_dem_type(bounds: Iterable[GeographicBounds], override: str | None) -> str:
    if override is not None:
        if override not in SUPPORTED_DEM_TYPES:
            choices = ", ".join(sorted(SUPPORTED_DEM_TYPES))
            raise Gpx2StlError(f"Unsupported DEM type '{override}'. Choose one of: {choices}.")
        return override
    all_bounds = tuple(bounds)
    if min(item.south for item in all_bounds) >= -56.0 and max(
        item.north for item in all_bounds
    ) <= 60.0:
        return "SRTMGL1"
    return "COP30"


class OpenTopographyClient:
    def __init__(
        self,
        api_key: str,
        session: requests.Session | None = None,
        timeout: tuple[float, float] = (10.0, 120.0),
    ) -> None:
        if not api_key:
            raise Gpx2StlError(
                "Topo mode requires --api-key or the OPENTOPOGRAPHY_API_KEY environment variable."
            )
        self._api_key = api_key
        self._session = session or requests.Session()
        self._timeout = timeout

    def fetch(self, bounds: tuple[GeographicBounds, ...], dem_type: str) -> DemSource:
        tiles: list[DemTile] = []
        for item in bounds:
            parameters = {
                "demtype": dem_type,
                "south": f"{item.south:.10f}",
                "north": f"{item.north:.10f}",
                "west": f"{item.west:.10f}",
                "east": f"{item.east:.10f}",
                "outputFormat": "GTiff",
                "API_Key": self._api_key,
            }
            try:
                response = self._session.get(
                    GLOBAL_DEM_URL,
                    params=parameters,
                    timeout=self._timeout,
                )
            except requests.RequestException as exc:
                raise Gpx2StlError(f"OpenTopography request failed: {exc}") from exc
            if response.status_code == 204:
                raise Gpx2StlError("OpenTopography returned no DEM data for the route.")
            if not response.ok:
                detail = response.text.strip()[:300]
                suffix = f": {detail}" if detail else ""
                raise Gpx2StlError(
                    f"OpenTopography returned HTTP {response.status_code}{suffix}"
                )
            tiles.append(DemTile.from_bytes(response.content))
        return DemSource(tuple(tiles))


def fill_missing(values: NDArray[np.float64]) -> NDArray[np.float64]:
    missing = ~np.isfinite(values)
    if not np.any(missing):
        return values
    if np.all(missing):
        raise Gpx2StlError("The selected DEM contains no usable elevation samples.")
    indices = distance_transform_edt(missing, return_distances=False, return_indices=True)
    return values[tuple(indices)]
