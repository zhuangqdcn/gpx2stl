from __future__ import annotations

import json
import math
import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import Any, Literal, Protocol, TypeAlias

import requests
import numpy as np
from shapely.geometry import (
    GeometryCollection,
    LineString,
    MultiLineString,
    MultiPolygon,
    Polygon,
)
from shapely.geometry.base import BaseGeometry
from shapely.ops import polygonize, split, transform, unary_union

from gpx2stl.dem import GeographicBounds
from gpx2stl.errors import Gpx2StlError
from gpx2stl.models import ProjectedRoute
from gpx2stl.progress import ProgressCallback

OVERPASS_URLS = (
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
)
OVERPASS_TILE_DEGREES = 0.01
OVERPASS_TIMEOUT_SECONDS = 120.0
MAX_OVERPASS_TILES = 200
OSM_CACHE_VERSION = 3

Polygonal: TypeAlias = Polygon | MultiPolygon
Linear: TypeAlias = LineString | MultiLineString
OsmElementType = Literal["way", "relation"]


class HttpResponse(Protocol):
    content: bytes
    headers: Mapping[str, str]
    status_code: int

    def raise_for_status(self) -> None:
        ...


class HttpSession(Protocol):
    def get(
        self,
        url: str,
        *,
        params: Mapping[str, str],
        headers: Mapping[str, str],
        timeout: float,
    ) -> HttpResponse:
        ...


@dataclass(frozen=True, order=True)
class GeographicTile:
    """A fixed-grid geographic tile used as an immutable cache unit."""

    latitude_index: int
    longitude_index: int
    south: float
    north: float
    west: float
    east: float

    @property
    def cache_key(self) -> str:
        size_microdegrees = round((self.north - self.south) * 1_000_000)
        return (
            f"osm_v{OSM_CACHE_VERSION}_{size_microdegrees}_"
            f"{self.latitude_index:05d}_{self.longitude_index:05d}"
        )


@dataclass(frozen=True)
class GeographicBuilding:
    osm_type: OsmElementType
    osm_id: int
    polygon: Polygonal
    height_m: float


@dataclass(frozen=True)
class CityBuilding:
    polygon: Polygon
    height_m: float


@dataclass(frozen=True)
class GeographicRoad:
    osm_id: int
    line: Linear
    highway: str


@dataclass(frozen=True)
class GeographicBridge:
    osm_id: int
    geometry: Linear | Polygonal
    width_m: float
    height_m: float


@dataclass(frozen=True)
class GeographicWater:
    osm_id: int
    geometry: Linear | Polygonal
    width_m: float | None = None
    coastline: bool = False
    boundary: bool = False


@dataclass(frozen=True)
class GeographicCityData:
    buildings: tuple[GeographicBuilding, ...]
    roads: tuple[GeographicRoad, ...]
    bridges: tuple[GeographicBridge, ...] = ()
    water: tuple[GeographicWater, ...] = ()


@dataclass(frozen=True)
class CityData:
    buildings: tuple[CityBuilding, ...]
    roads: tuple[LineString, ...]
    bridges: tuple[CityBuilding, ...] = ()
    water: tuple[Polygon, ...] = ()


def geographic_tiles(
    bounds: GeographicBounds | Iterable[GeographicBounds],
    tile_degrees: float = OVERPASS_TILE_DEGREES,
) -> tuple[GeographicTile, ...]:
    """Return fixed-grid tiles covering bounds, including bounds crossing ±180°."""

    if not math.isfinite(tile_degrees) or tile_degrees <= 0.0:
        raise ValueError("tile_degrees must be a positive finite number")
    supplied = (bounds,) if isinstance(bounds, GeographicBounds) else tuple(bounds)
    tiles: dict[tuple[int, int], GeographicTile] = {}
    for item in supplied:
        _validate_bounds(item)
        for west, east in _longitude_intervals(item.west, item.east):
            latitude_indices = _covering_indices(item.south, item.north, -90.0, tile_degrees)
            longitude_indices = _covering_indices(west, east, -180.0, tile_degrees)
            for latitude_index in latitude_indices:
                south = round(-90.0 + latitude_index * tile_degrees, 12)
                north = min(90.0, round(south + tile_degrees, 12))
                for longitude_index in longitude_indices:
                    tile_west = round(-180.0 + longitude_index * tile_degrees, 12)
                    tile_east = min(180.0, round(tile_west + tile_degrees, 12))
                    tiles[(latitude_index, longitude_index)] = GeographicTile(
                        latitude_index,
                        longitude_index,
                        south,
                        north,
                        tile_west,
                        tile_east,
                    )
    return tuple(tiles[key] for key in sorted(tiles))


def _validate_bounds(bounds: GeographicBounds) -> None:
    values = (bounds.south, bounds.north, bounds.west, bounds.east)
    if not all(math.isfinite(value) for value in values):
        raise ValueError("geographic bounds must be finite")
    if not -90.0 <= bounds.south < bounds.north <= 90.0:
        raise ValueError("geographic latitude bounds are invalid")
    if not -180.0 <= bounds.west <= 180.0 or not -180.0 <= bounds.east <= 180.0:
        raise ValueError("geographic longitude bounds are invalid")
    if bounds.west == bounds.east:
        raise ValueError("geographic longitude bounds have no width")


def _longitude_intervals(west: float, east: float) -> tuple[tuple[float, float], ...]:
    if west < east:
        return ((west, east),)
    return ((west, 180.0), (-180.0, east))


def _covering_indices(
    lower: float, upper: float, origin: float, tile_degrees: float
) -> range:
    start = math.floor((lower - origin) / tile_degrees + 1e-10)
    stop = math.ceil((upper - origin) / tile_degrees - 1e-10)
    return range(start, stop)


def overpass_query(tile: GeographicTile) -> str:
    bbox = f"{tile.south:.10g},{tile.west:.10g},{tile.north:.10g},{tile.east:.10g}"
    return (
        "[out:json][timeout:120];"
        "("
        f'way["building"]({bbox});'
        f'relation["building"]["type"="multipolygon"]({bbox});'
        f'way["highway"]({bbox});'
        f'way["bridge"]({bbox});'
        f'way["man_made"="bridge"]({bbox});'
        f'way["natural"~"water|coastline"]({bbox});'
        f'relation["natural"="water"]["type"="multipolygon"]({bbox});'
        f'way["waterway"~"river|stream|canal|riverbank"]({bbox});'
        f'relation["waterway"="riverbank"]["type"="multipolygon"]({bbox});'
        f'way["landuse"~"reservoir|basin"]({bbox});'
        f'relation["landuse"~"reservoir|basin"]["type"="multipolygon"]({bbox});'
        ");"
        "out body;"
        ">;"
        "out skel qt;"
    )


def load_raw_tiles(
    bounds: GeographicBounds | Iterable[GeographicBounds],
    cache_dir: Path,
    *,
    progress: ProgressCallback | None = None,
    session: HttpSession | None = None,
) -> tuple[Mapping[str, Any], ...]:
    """Load cached Overpass documents, fetching only absent or empty tile files."""

    tiles = geographic_tiles(bounds)
    if len(tiles) > MAX_OVERPASS_TILES:
        raise Gpx2StlError(
            f"City footprint requires {len(tiles)} OpenStreetMap tiles, exceeding "
            f"the safe public Overpass limit of {MAX_OVERPASS_TILES}; use a smaller "
            "numeric --route-boundary-percent or a shorter activity."
        )
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise Gpx2StlError(
            f"Unable to create city-data cache directory '{cache_dir}': {exc}"
        ) from exc
    client = session if session is not None else requests.Session()
    owns_client = session is None
    documents: list[Mapping[str, Any]] = []
    last_request_at: float | None = None
    map_fallback_active = False
    try:
        for index, tile in enumerate(tiles, start=1):
            path = cache_dir / f"{tile.cache_key}.json"
            if path.is_file() and path.stat().st_size > 0:
                if progress is not None:
                    progress(f"Loading cached city data tile {index}/{len(tiles)}")
                try:
                    content = path.read_bytes()
                except OSError as exc:
                    raise Gpx2StlError(
                        f"Unable to read cached city data '{path}': {exc}"
                    ) from exc
                documents.append(_decode_document(content, str(path)))
                continue
            if progress is not None:
                progress(f"Downloading city data tile {index}/{len(tiles)}")
            if owns_client and last_request_at is not None:
                delay = 1.0 - (time.monotonic() - last_request_at)
                if delay > 0.0:
                    time.sleep(delay)
            if map_fallback_active:
                document, content = _fetch_osm_map(client, tile)
            else:
                document, content, used_map_fallback = _fetch_tile(
                    client, tile, split_on_failure=owns_client
                )
                map_fallback_active = used_map_fallback
            last_request_at = time.monotonic()
            _atomic_write(path, content)
            documents.append(document)
    finally:
        if owns_client:
            client.close()
    return tuple(documents)


def _fetch_tile(
    session: HttpSession,
    tile: GeographicTile,
    *,
    split_on_failure: bool = False,
) -> tuple[Mapping[str, Any], bytes, bool]:
    response: HttpResponse | None = None
    for attempt in range(3):
        try:
            response = session.get(
                OVERPASS_URLS[attempt],
                params={"data": overpass_query(tile)},
                headers={
                    "User-Agent": "gpx2stl/0.1 "
                    "(+https://github.com/zhuangqdcn/gpx2stl)"
                },
                timeout=OVERPASS_TIMEOUT_SECONDS,
            )
            if response.status_code in {429, 502, 504} and attempt < 2:
                retry_after = response.headers.get("Retry-After")
                delay = (
                    float(retry_after)
                    if retry_after is not None and retry_after.isdigit()
                    else 5.0 * (attempt + 1)
                )
                time.sleep(delay)
                continue
            response.raise_for_status()
            break
        except requests.RequestException as exc:
            if attempt < 2:
                time.sleep(5.0 * (attempt + 1))
                continue
            if split_on_failure:
                document, content = _fetch_osm_map(session, tile)
                return document, content, True
            raise Gpx2StlError(
                f"Unable to download OpenStreetMap city data for tile "
                f"({tile.south:g}, {tile.west:g}, {tile.north:g}, "
                f"{tile.east:g}): {exc}"
            ) from exc
    assert response is not None
    content = bytes(response.content)
    if not content:
        raise Gpx2StlError("Overpass returned an empty city-data response.")
    return _decode_document(content, "Overpass response"), content, False


def _fetch_osm_map(
    session: HttpSession, tile: GeographicTile
) -> tuple[Mapping[str, Any], bytes]:
    try:
        response = session.get(
            "https://api.openstreetmap.org/api/0.6/map",
            params={
                "bbox": (
                    f"{tile.west:.10g},{tile.south:.10g},"
                    f"{tile.east:.10g},{tile.north:.10g}"
                )
            },
            headers={
                "User-Agent": "gpx2stl/0.1 "
                "(+https://github.com/zhuangqdcn/gpx2stl)"
            },
            timeout=OVERPASS_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise Gpx2StlError(
            "Unable to download fallback OpenStreetMap data for tile "
            f"({tile.south:g}, {tile.west:g}, {tile.north:g}, {tile.east:g}): "
            f"{exc}"
        ) from exc
    try:
        root = ET.fromstring(response.content)
    except ET.ParseError as exc:
        raise Gpx2StlError(
            "OpenStreetMap map fallback returned invalid XML."
        ) from exc
    elements: list[dict[str, Any]] = []
    for source in root:
        if source.tag not in {"node", "way", "relation"}:
            continue
        try:
            element: dict[str, Any] = {
                "type": source.tag,
                "id": int(source.attrib["id"]),
            }
        except (KeyError, ValueError):
            continue
        if source.tag == "node":
            try:
                element["lat"] = float(source.attrib["lat"])
                element["lon"] = float(source.attrib["lon"])
            except (KeyError, ValueError):
                continue
        elif source.tag == "way":
            element["nodes"] = [
                int(child.attrib["ref"])
                for child in source
                if child.tag == "nd" and child.attrib.get("ref", "").isdigit()
            ]
        else:
            element["members"] = [
                {
                    "type": child.attrib["type"],
                    "ref": int(child.attrib["ref"]),
                    "role": child.attrib.get("role", ""),
                }
                for child in source
                if child.tag == "member"
                and child.attrib.get("type") in {"node", "way", "relation"}
                and child.attrib.get("ref", "").isdigit()
            ]
        tags = {
            child.attrib["k"]: child.attrib["v"]
            for child in source
            if child.tag == "tag" and "k" in child.attrib and "v" in child.attrib
        }
        if tags:
            element["tags"] = tags
        elements.append(element)
    combined: Mapping[str, Any] = {"elements": elements}
    return combined, json.dumps(combined, separators=(",", ":")).encode()


def _decode_document(content: bytes, source: str) -> Mapping[str, Any]:
    try:
        document = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise Gpx2StlError(f"{source} does not contain valid Overpass JSON.") from exc
    if not isinstance(document, dict) or not isinstance(document.get("elements"), list):
        raise Gpx2StlError(f"{source} is not a valid Overpass response.")
    return document


def _atomic_write(path: Path, content: bytes) -> None:
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.partial"
    )
    try:
        with temporary.open("wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        raise Gpx2StlError(f"Unable to cache OpenStreetMap city data at '{path}': {exc}") from exc
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def parse_height(tags: Mapping[str, Any], default_height: float) -> float:
    """Parse OSM height values into source meters, then levels, then fallback."""

    if not math.isfinite(default_height) or default_height <= 0.0:
        raise ValueError("default_height must be a positive finite number")
    explicit = _parse_length(tags.get("height"))
    if explicit is not None:
        return explicit
    levels = _positive_number(tags.get("building:levels"))
    if levels is not None:
        return levels * 3.0
    return float(default_height)


_NUMBER = r"(?:\d+(?:\.\d*)?|\.\d+)"
_FEET_INCHES = re.compile(
    rf"^\s*(?P<feet>{_NUMBER})\s*(?:ft|feet|foot|')"
    rf"(?:\s*(?P<inches>{_NUMBER})\s*(?:in|inch(?:es)?|\"))?\s*$",
    re.IGNORECASE,
)
_METRIC = re.compile(rf"^\s*(?P<value>{_NUMBER})\s*(?:m|meter(?:s)?|metre(?:s)?)?\s*$", re.I)


def _parse_length(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return _positive_number(value)
    if not isinstance(value, str):
        return None
    feet_match = _FEET_INCHES.fullmatch(value)
    if feet_match is not None:
        feet = float(feet_match.group("feet"))
        inches = float(feet_match.group("inches") or 0.0)
        result = feet * 0.3048 + inches * 0.0254
        return result if result > 0.0 and math.isfinite(result) else None
    metric_match = _METRIC.fullmatch(value)
    if metric_match is None:
        return None
    return _positive_number(metric_match.group("value"))


def _positive_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0.0 else None


def parse_city_data(
    documents: Mapping[str, Any] | Iterable[Mapping[str, Any]],
    default_height: float,
) -> GeographicCityData:
    """Deduplicate and parse raw Overpass documents into geographic features."""

    parse_height({}, default_height)
    supplied = (documents,) if isinstance(documents, Mapping) else tuple(documents)
    elements: dict[tuple[str, int], Mapping[str, Any]] = {}
    nodes: dict[int, tuple[float, float]] = {}
    for document in supplied:
        raw_elements = document.get("elements")
        if not isinstance(raw_elements, list):
            raise Gpx2StlError("Overpass document has no elements array.")
        for element in raw_elements:
            if not isinstance(element, Mapping):
                continue
            element_type = element.get("type")
            osm_id = element.get("id")
            if not isinstance(element_type, str) or not isinstance(osm_id, int):
                continue
            if element_type == "node":
                coordinate = _node_coordinate(element)
                if coordinate is not None:
                    nodes[osm_id] = coordinate
            elif element_type in {"way", "relation"}:
                elements.setdefault((element_type, osm_id), element)

    buildings: list[GeographicBuilding] = []
    roads: list[GeographicRoad] = []
    bridges: list[GeographicBridge] = []
    water: list[GeographicWater] = []
    for (element_type, osm_id), element in sorted(elements.items()):
        tags = element.get("tags")
        tags = tags if isinstance(tags, Mapping) else {}
        if element_type == "way":
            if _is_building(tags):
                polygon = _way_polygon(element, nodes)
                if polygon is not None:
                    buildings.append(
                        GeographicBuilding(
                            "way", osm_id, polygon, parse_height(tags, default_height)
                        )
                    )
            highway = tags.get("highway")
            area = str(tags.get("area", "")).lower()
            excluded_highways = {
                "bus_guideway",
                "construction",
                "corridor",
                "elevator",
                "platform",
                "proposed",
                "raceway",
            }
            if (
                isinstance(highway, str)
                and highway
                and highway not in excluded_highways
                and area not in {"1", "true", "yes"}
            ):
                line = _way_line(element, nodes)
                if line is not None:
                    roads.append(GeographicRoad(osm_id, line, highway))
            if _is_bridge(tags):
                bridge_geometry: Linear | Polygonal | None = None
                if area in {"1", "true", "yes"} or tags.get("man_made") == "bridge":
                    bridge_geometry = _way_polygon(element, nodes)
                if bridge_geometry is None:
                    bridge_geometry = _way_line(element, nodes)
                if bridge_geometry is not None:
                    bridges.append(
                        GeographicBridge(
                            osm_id,
                            bridge_geometry,
                            _bridge_width(tags),
                            _bridge_height(tags),
                        )
                    )
            if _is_water_area(tags):
                polygon = _way_polygon(element, nodes)
                if polygon is not None:
                    water.append(GeographicWater(osm_id, polygon))
            elif tags.get("natural") == "coastline":
                line = _way_line(element, nodes)
                if line is not None:
                    water.append(
                        GeographicWater(osm_id, line, coastline=True)
                    )
            elif tags.get("waterway") in {"river", "stream", "canal"}:
                line = _way_line(element, nodes)
                if line is not None:
                    water.append(
                        GeographicWater(
                            osm_id,
                            line,
                            width_m=_waterway_width(tags),
                        )
                    )
        elif tags.get("type") == "multipolygon":
            polygon = _relation_polygon(element, nodes, elements)
            if polygon is not None and _is_building(tags):
                buildings.append(
                    GeographicBuilding(
                        "relation", osm_id, polygon, parse_height(tags, default_height)
                    )
                )
            if polygon is not None and _is_water_area(tags):
                water.append(GeographicWater(osm_id, polygon))
            elif polygon is None and _is_water_area(tags):
                boundary = _relation_boundary(element, nodes, elements)
                if boundary is not None:
                    water.append(
                        GeographicWater(osm_id, boundary, boundary=True)
                    )
    return GeographicCityData(
        tuple(buildings), tuple(roads), tuple(bridges), tuple(water)
    )


def _is_building(tags: Mapping[str, Any]) -> bool:
    value = tags.get("building")
    return value is not None and str(value).lower() not in {"", "no", "false", "0"}


def _is_bridge(tags: Mapping[str, Any]) -> bool:
    bridge = str(tags.get("bridge", "")).lower()
    return bridge not in {"", "no", "false", "0"} or tags.get("man_made") == "bridge"


def _is_water_area(tags: Mapping[str, Any]) -> bool:
    return (
        tags.get("natural") == "water"
        or tags.get("waterway") == "riverbank"
        or tags.get("landuse") in {"reservoir", "basin"}
    )


def _waterway_width(tags: Mapping[str, Any]) -> float:
    explicit = _parse_length(tags.get("width"))
    if explicit is not None:
        return explicit
    return {
        "river": 12.0,
        "canal": 6.0,
        "stream": 2.0,
    }.get(str(tags.get("waterway")), 3.0)


def _bridge_width(tags: Mapping[str, Any]) -> float:
    explicit = _parse_length(tags.get("width"))
    if explicit is not None:
        return explicit
    lanes = _positive_number(tags.get("lanes"))
    if lanes is not None:
        return max(3.0, lanes * 3.2)
    highway = tags.get("highway")
    if highway in {"motorway", "trunk"}:
        return 10.0
    if highway in {"primary", "secondary", "tertiary"}:
        return 7.0
    if highway in {"footway", "path", "cycleway", "steps"}:
        return 2.5
    if tags.get("railway") is not None:
        return 5.0
    return 5.0


def _bridge_height(tags: Mapping[str, Any]) -> float:
    explicit = _parse_length(tags.get("height"))
    if explicit is not None:
        return explicit
    try:
        layer = float(tags.get("layer", 1.0))
    except (TypeError, ValueError):
        layer = 1.0
    return max(5.0, abs(layer) * 5.0) if math.isfinite(layer) else 5.0


def _node_coordinate(element: Mapping[str, Any]) -> tuple[float, float] | None:
    try:
        longitude = float(element["lon"])
        latitude = float(element["lat"])
    except (KeyError, TypeError, ValueError):
        return None
    if not math.isfinite(longitude) or not math.isfinite(latitude):
        return None
    return longitude, latitude


def _coordinates(
    element: Mapping[str, Any], nodes: Mapping[int, tuple[float, float]]
) -> list[tuple[float, float]]:
    geometry = element.get("geometry")
    coordinates: list[tuple[float, float]] = []
    if isinstance(geometry, Sequence) and not isinstance(geometry, (str, bytes)):
        for point in geometry:
            if not isinstance(point, Mapping):
                continue
            coordinate = _node_coordinate(point)
            if coordinate is not None:
                coordinates.append(coordinate)
        if coordinates:
            return coordinates
    node_ids = element.get("nodes")
    if isinstance(node_ids, Sequence) and not isinstance(node_ids, (str, bytes)):
        for node_id in node_ids:
            if isinstance(node_id, int) and node_id in nodes:
                coordinates.append(nodes[node_id])
    return coordinates


def _way_line(
    element: Mapping[str, Any], nodes: Mapping[int, tuple[float, float]]
) -> LineString | None:
    coordinates = _coordinates(element, nodes)
    if len(coordinates) < 2:
        return None
    line = LineString(coordinates)
    return line if not line.is_empty and line.length > 0.0 else None


def _way_polygon(
    element: Mapping[str, Any], nodes: Mapping[int, tuple[float, float]]
) -> Polygonal | None:
    coordinates = _coordinates(element, nodes)
    if len(coordinates) < 3:
        return None
    if coordinates[0] != coordinates[-1]:
        coordinates.append(coordinates[0])
    polygon = Polygon(coordinates)
    return _polygonal(polygon if polygon.is_valid else polygon.buffer(0))


def _relation_polygon(
    element: Mapping[str, Any],
    nodes: Mapping[int, tuple[float, float]],
    elements: Mapping[tuple[str, int], Mapping[str, Any]],
) -> Polygonal | None:
    members = element.get("members")
    if not isinstance(members, Sequence) or isinstance(members, (str, bytes)):
        return None
    outer_lines: list[LineString] = []
    inner_lines: list[LineString] = []
    for member in members:
        if not isinstance(member, Mapping) or member.get("type") != "way":
            continue
        reference = member.get("ref")
        source = (
            elements.get(("way", reference), member)
            if isinstance(reference, int)
            else member
        )
        line = _way_line(source, nodes)
        if line is None:
            continue
        if member.get("role") == "inner":
            inner_lines.append(line)
        else:
            outer_lines.append(line)
    outer = _polygonized(outer_lines)
    if outer is None:
        return None
    inner = _polygonized(inner_lines)
    result = outer if inner is None else outer.difference(inner)
    return _polygonal(result)


def _relation_boundary(
    element: Mapping[str, Any],
    nodes: Mapping[int, tuple[float, float]],
    elements: Mapping[tuple[str, int], Mapping[str, Any]],
) -> Linear | None:
    members = element.get("members")
    if not isinstance(members, Sequence) or isinstance(members, (str, bytes)):
        return None
    lines: list[LineString] = []
    for member in members:
        if (
            not isinstance(member, Mapping)
            or member.get("type") != "way"
            or member.get("role") == "inner"
        ):
            continue
        reference = member.get("ref")
        source = (
            elements.get(("way", reference), member)
            if isinstance(reference, int)
            else member
        )
        line = _way_line(source, nodes)
        if line is not None:
            lines.append(line)
    return _linear(unary_union(lines)) if lines else None


def _polygonized(lines: Sequence[LineString]) -> Polygonal | None:
    if not lines:
        return None
    polygons = list(polygonize(unary_union(lines)))
    if not polygons:
        return None
    return _polygonal(unary_union(polygons))


def _polygonal(geometry: BaseGeometry) -> Polygonal | None:
    if geometry.is_empty:
        return None
    if isinstance(geometry, Polygon):
        return geometry
    if isinstance(geometry, MultiPolygon):
        return geometry
    if isinstance(geometry, GeometryCollection):
        polygons = [
            item
            for item in geometry.geoms
            if isinstance(item, (Polygon, MultiPolygon))
        ]
        if polygons:
            combined = unary_union(polygons)
            if isinstance(combined, (Polygon, MultiPolygon)):
                return combined
    return None


def _linear(geometry: BaseGeometry) -> Linear | None:
    if geometry.is_empty:
        return None
    if isinstance(geometry, (LineString, MultiLineString)):
        return geometry
    if isinstance(geometry, GeometryCollection):
        lines = [
            item
            for item in geometry.geoms
            if isinstance(item, (LineString, MultiLineString))
        ]
        if lines:
            combined = unary_union(lines)
            if isinstance(combined, (LineString, MultiLineString)):
                return combined
    return None


def _individual_polygons(geometry: Polygonal) -> tuple[Polygon, ...]:
    if isinstance(geometry, Polygon):
        return (geometry,)
    return tuple(geometry.geoms)


def _individual_lines(geometry: Linear) -> tuple[LineString, ...]:
    if isinstance(geometry, LineString):
        return (geometry,)
    return tuple(geometry.geoms)


def project_city_data(
    data: GeographicCityData,
    route: ProjectedRoute,
    projected_clip: Polygonal,
) -> CityData:
    """Project WGS84 features and clip them to the printable projected polygon."""

    if projected_clip.is_empty or not projected_clip.is_valid:
        raise ValueError("projected_clip must be a valid, nonempty polygon")
    buildings: list[CityBuilding] = []
    for building in data.buildings:
        projected = transform(route.forward.transform, building.polygon)
        clipped = _polygonal(projected.intersection(projected_clip))
        if clipped is not None:
            buildings.extend(
                CityBuilding(polygon, building.height_m)
                for polygon in _individual_polygons(clipped)
            )
    roads: list[LineString] = []
    for road in data.roads:
        projected = transform(route.forward.transform, road.line)
        clipped = _linear(projected.intersection(projected_clip))
        if clipped is not None:
            roads.extend(_individual_lines(clipped))
    bridges: list[CityBuilding] = []
    for bridge in data.bridges:
        projected = transform(route.forward.transform, bridge.geometry)
        footprint = (
            projected
            if isinstance(projected, (Polygon, MultiPolygon))
            else projected.buffer(
                bridge.width_m / 2.0,
                cap_style="flat",
                join_style="round",
            )
        )
        clipped = _polygonal(footprint.intersection(projected_clip))
        if clipped is not None:
            bridges.extend(
                CityBuilding(polygon, bridge.height_m)
                for polygon in _individual_polygons(clipped)
            )
    water_polygons: list[Polygon] = []
    coastlines: list[LineString] = []
    water_boundaries: list[LineString] = []
    linear_water: list[Polygon] = []
    for feature in data.water:
        projected = transform(route.forward.transform, feature.geometry)
        if feature.coastline:
            linear = _linear(projected)
            if linear is not None:
                coastlines.extend(_individual_lines(linear))
            continue
        if feature.boundary:
            linear = _linear(projected)
            if linear is not None:
                water_boundaries.extend(_individual_lines(linear))
            continue
        footprint = (
            projected
            if isinstance(projected, (Polygon, MultiPolygon))
            else projected.buffer(
                (feature.width_m or 3.0) / 2.0,
                cap_style="round",
                join_style="round",
            )
        )
        clipped = _polygonal(footprint.intersection(projected_clip))
        if clipped is not None:
            polygons = _individual_polygons(clipped)
            water_polygons.extend(polygons)
            if feature.width_m is not None:
                linear_water.extend(polygons)
    water_polygons.extend(
        _bounded_water(projected_clip, water_boundaries, linear_water)
    )
    water_polygons.extend(_coastline_water(projected_clip, coastlines))
    merged_water = (
        _polygonal(unary_union(water_polygons))
        if water_polygons
        else None
    )
    return CityData(
        tuple(buildings),
        tuple(roads),
        tuple(bridges),
        _individual_polygons(merged_water) if merged_water is not None else (),
    )


def _bounded_water(
    projected_clip: Polygonal,
    boundaries: Sequence[LineString],
    linear_water: Sequence[Polygon],
) -> tuple[Polygon, ...]:
    if not boundaries or not linear_water:
        return ()
    seeds = unary_union(linear_water)
    water: list[Polygon] = []
    cutter = unary_union(boundaries)
    for clip_polygon in _individual_polygons(projected_clip):
        pieces = split(clip_polygon, cutter)
        for piece in pieces.geoms:
            polygon = _polygonal(piece)
            if polygon is None:
                continue
            for candidate in _individual_polygons(polygon):
                overlap_area = float(candidate.intersection(seeds).area)
                if (
                    overlap_area > 0.0
                    and candidate.area <= overlap_area * 50.0
                ):
                    water.append(candidate)
    return tuple(water)


def _coastline_water(
    projected_clip: Polygonal,
    coastlines: Sequence[LineString],
) -> tuple[Polygon, ...]:
    if not coastlines:
        return ()
    water: list[Polygon] = []
    cutter = unary_union(coastlines)
    for clip_polygon in _individual_polygons(projected_clip):
        pieces = split(clip_polygon, cutter)
        for piece in pieces.geoms:
            polygon = _polygonal(piece)
            if polygon is None:
                continue
            for candidate in _individual_polygons(polygon):
                point = candidate.representative_point()
                if _right_of_nearest_coastline(point.x, point.y, coastlines):
                    water.append(candidate)
    return tuple(water)


def _right_of_nearest_coastline(
    x: float,
    y: float,
    coastlines: Sequence[LineString],
) -> bool:
    nearest_distance = math.inf
    nearest_cross = 0.0
    for coastline in coastlines:
        coordinates = np.asarray(coastline.coords, dtype=np.float64)
        for start, end in pairwise(coordinates):
            delta = end - start
            length_squared = float(np.dot(delta, delta))
            if length_squared <= 0.0:
                continue
            fraction = float(
                np.clip(np.dot(np.array([x, y]) - start, delta) / length_squared, 0.0, 1.0)
            )
            closest = start + fraction * delta
            distance = float(np.dot(np.array([x, y]) - closest, np.array([x, y]) - closest))
            if distance < nearest_distance:
                nearest_distance = distance
                nearest_cross = float(
                    delta[0] * (y - start[1]) - delta[1] * (x - start[0])
                )
    return nearest_cross < 0.0


def load_city_data(
    bounds: GeographicBounds | Iterable[GeographicBounds],
    cache_dir: Path,
    route: ProjectedRoute,
    projected_clip: Polygonal,
    default_height: float,
    *,
    progress: ProgressCallback | None = None,
    session: HttpSession | None = None,
) -> CityData:
    """Fetch/cache, parse, project, and clip OpenStreetMap city features."""

    documents = load_raw_tiles(
        bounds, Path(cache_dir), progress=progress, session=session
    )
    geographic = parse_city_data(documents, default_height)
    return project_city_data(geographic, route, projected_clip)
