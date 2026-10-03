from __future__ import annotations

import math
import re
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
from gpx2stl.footprint import footprint_vertices
from gpx2stl.models import Footprint, ProjectedRoute

GLOBAL_DEM_URL = "https://portal.opentopography.org/API/globaldem"
COPERNICUS_GLO30_URL = "https://copernicus-dem-30m.s3.amazonaws.com"
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
COPERNICUS_TILE_PATTERN = re.compile(
    r"Copernicus_DSM_COG_(?:10|30)_([NS])(\d{2})_00_([EW])(\d{3})_00_DEM"
    r"\.(?:tif|tiff)",
    re.IGNORECASE,
)


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

    def covers_lonlat(
        self, longitude: NDArray[np.float64], latitude: NDArray[np.float64]
    ) -> NDArray[np.bool_]:
        covered = np.zeros(longitude.shape, dtype=np.bool_)
        adjusted = ((longitude + 180.0) % 360.0) - 180.0
        for tile in self.tiles:
            covered |= tile.covers(adjusted, latitude)
        return covered

    def sample_lonlat(
        self, longitude: NDArray[np.float64], latitude: NDArray[np.float64]
    ) -> NDArray[np.float64]:
        result = np.full(longitude.shape, np.nan, dtype=np.float64)
        covered = self.covers_lonlat(longitude, latitude)
        for tile in self.tiles:
            adjusted = ((longitude + 180.0) % 360.0) - 180.0
            tile_coverage = tile.covers(adjusted, latitude)
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
    return footprint_vertices(footprint)


def request_bounds(footprint: Footprint, route: ProjectedRoute) -> tuple[GeographicBounds, ...]:
    perimeter = _footprint_perimeter(footprint)
    return request_projected_bounds(perimeter, route)


def request_projected_bounds(
    perimeter: NDArray[np.float64],
    route: ProjectedRoute,
) -> tuple[GeographicBounds, ...]:
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


def _copernicus_geographic_bounds(path: Path) -> GeographicBounds | None:
    match = COPERNICUS_TILE_PATTERN.fullmatch(path.name)
    if match is None:
        return None
    latitude = int(match.group(2)) * (-1 if match.group(1).upper() == "S" else 1)
    longitude = int(match.group(4)) * (-1 if match.group(3).upper() == "W" else 1)
    return GeographicBounds(latitude, latitude + 1, longitude, longitude + 1)


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
        raster_bounds = _copernicus_geographic_bounds(path) or _raster_geographic_bounds(path)
        if any(_bounds_intersect(raster_bounds, item) for item in bounds):
            selected.append(path)
    if not selected:
        return None
    return DemSource(
        tuple(DemTile.from_path(path) for path in selected),
        require_complete_coverage=True,
        description=str(topo_dir),
    )


def _copernicus_tile_name(latitude: int, longitude: int) -> str:
    northing = f"{'N' if latitude >= 0 else 'S'}{abs(latitude):02d}_00"
    easting = f"{'E' if longitude >= 0 else 'W'}{abs(longitude):03d}_00"
    return f"Copernicus_DSM_COG_10_{northing}_{easting}_DEM"


def required_copernicus_tiles(
    bounds: tuple[GeographicBounds, ...],
) -> tuple[str, ...]:
    names: set[str] = set()
    for item in bounds:
        south = max(-90, math.floor(item.south))
        north = min(90, math.ceil(item.north))
        west = max(-180, math.floor(item.west))
        east = min(180, math.ceil(item.east))
        for latitude in range(south, north):
            for longitude in range(west, east):
                names.add(_copernicus_tile_name(latitude, longitude))
    return tuple(sorted(names))


def cache_copernicus_tiles(
    bounds: tuple[GeographicBounds, ...],
    cache_dir: Path,
    session: requests.Session | None = None,
    timeout: tuple[float, float] = (10.0, 180.0),
) -> bool:
    cache_dir.mkdir(parents=True, exist_ok=True)
    http = session or requests.Session()
    for tile_name in required_copernicus_tiles(bounds):
        destination = cache_dir / f"{tile_name}.tif"
        if destination.is_file() and destination.stat().st_size > 0:
            continue
        partial = destination.with_suffix(".tif.part")
        offset = partial.stat().st_size if partial.is_file() else 0
        headers = {"Range": f"bytes={offset}-"} if offset else {}
        url = f"{COPERNICUS_GLO30_URL}/{tile_name}/{tile_name}.tif"
        try:
            response = http.get(
                url,
                headers=headers,
                stream=True,
                timeout=timeout,
            )
        except requests.RequestException as exc:
            raise Gpx2StlError(f"Copernicus GLO-30 download failed: {exc}") from exc
        if response.status_code == 404:
            return False
        if offset and response.status_code != 206:
            offset = 0
        if not response.ok:
            raise Gpx2StlError(
                f"Copernicus GLO-30 returned HTTP {response.status_code} for "
                f"{tile_name}."
            )
        mode = "ab" if offset and response.status_code == 206 else "wb"
        try:
            with partial.open(mode) as stream:
                for chunk in response.iter_content(1024 * 1024):
                    if chunk:
                        stream.write(chunk)
            partial.replace(destination)
        except OSError as exc:
            raise Gpx2StlError(
                f"Unable to cache Copernicus tile '{destination}': {exc}"
            ) from exc
    return True


def dem_covers_bounds(
    source: DemSource,
    bounds: tuple[GeographicBounds, ...],
    samples_per_axis: int = 9,
) -> bool:
    for item in bounds:
        longitude = np.linspace(item.west, item.east, samples_per_axis)
        latitude = np.linspace(item.south, item.north, samples_per_axis)
        xx, yy = np.meshgrid(longitude, latitude)
        if not np.all(source.covers_lonlat(xx.ravel(), yy.ravel())):
            return False
    return True


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
