from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
import requests
from pyproj import Transformer
from shapely.geometry import LineString, MultiLineString, box
from shapely.ops import unary_union

from gpx2stl.city import (
    CITY_REQUEST_INTERVAL_SECONDS,
    OVERPASS_TILE_DEGREES,
    GeographicBridge,
    GeographicCityData,
    GeographicRoad,
    GeographicBuilding,
    GeographicWater,
    geographic_tiles,
    load_city_data,
    load_raw_tiles,
    parse_city_data,
    parse_height,
    project_city_data,
)
from gpx2stl.dem import GeographicBounds
from gpx2stl.errors import Gpx2StlError
from gpx2stl.models import ProjectedRoute


class _Response:
    def __init__(self, body: bytes, status: int = 200) -> None:
        self.content = body
        self.status = status
        self.status_code = status
        self.headers: dict[str, str] = {}

    def raise_for_status(self) -> None:
        if self.status >= 400:
            raise requests.HTTPError(f"{self.status} response")


class _Session:
    def __init__(self, responses: list[_Response]) -> None:
        self.responses = responses
        self.calls: list[dict[str, object]] = []

    def get(self, url: str, **kwargs: object) -> _Response:
        self.calls.append({"url": url, **kwargs})
        return self.responses.pop(0)

    def close(self) -> None:
        pass


def _route() -> ProjectedRoute:
    forward = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
    inverse = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)
    return ProjectedRoute(
        (np.array([[0.0, 0.0], [1.0, 1.0]]),),
        (np.zeros(2),),
        forward,
        inverse,
    )


def _document() -> dict[str, object]:
    return {
        "elements": [
            {
                "type": "way",
                "id": 10,
                "tags": {"building": "yes", "height": "20 ft"},
                "geometry": [
                    {"lon": 0.0, "lat": 0.0},
                    {"lon": 0.002, "lat": 0.0},
                    {"lon": 0.002, "lat": 0.002},
                    {"lon": 0.0, "lat": 0.002},
                    {"lon": 0.0, "lat": 0.0},
                ],
            },
            {
                "type": "way",
                "id": 20,
                "tags": {"highway": "residential"},
                "geometry": [
                    {"lon": -0.001, "lat": 0.001},
                    {"lon": 0.003, "lat": 0.001},
                ],
            },
        ]
    }


def test_geographic_tiles_are_fixed_deterministic_and_antimeridian_safe() -> None:
    size = OVERPASS_TILE_DEGREES
    bounds = GeographicBounds(1.01, 1.01 + size, 179.98, -179.98)
    first = geographic_tiles(bounds)
    second = geographic_tiles(bounds)
    assert first == second
    assert {tile.west for tile in first} == {
        179.98,
        179.99,
        -180.0,
        -179.99,
    }
    assert all(tile.east <= 180.0 for tile in first)
    assert len({tile.cache_key for tile in first}) == len(first)
    larger = geographic_tiles(bounds, tile_degrees=size * 2)
    assert {tile.cache_key for tile in first}.isdisjoint(
        tile.cache_key for tile in larger
    )


def test_city_tiles_remain_fixed_for_broad_marathon_footprint() -> None:
    bounds = GeographicBounds(
        49.20562,
        49.33461,
        -123.29883,
        -123.07126,
    )

    tiles = geographic_tiles(bounds)

    assert len(tiles) == 322
    assert all(
        tile.north - tile.south == pytest.approx(OVERPASS_TILE_DEGREES)
        for tile in tiles
    )


def test_nonempty_cached_tile_is_reused_without_network(tmp_path: Path) -> None:
    bounds = GeographicBounds(0.001, 0.002, 0.001, 0.002)
    tile = geographic_tiles(bounds)[0]
    cached = json.dumps(_document()).encode()
    (tmp_path / f"{tile.cache_key}.json").write_bytes(cached)
    session = _Session([])

    assert load_raw_tiles(bounds, tmp_path, session=session) == (_document(),)
    assert session.calls == []


def test_only_missing_or_empty_tiles_are_fetched_and_atomically_cached(
    tmp_path: Path,
) -> None:
    bounds = GeographicBounds(
        0.001, 0.002, 0.001, OVERPASS_TILE_DEGREES + 0.002
    )
    tiles = geographic_tiles(bounds)
    assert len(tiles) == 2
    first_content = json.dumps({"elements": []}).encode()
    (tmp_path / f"{tiles[0].cache_key}.json").write_bytes(first_content)
    (tmp_path / f"{tiles[1].cache_key}.json").write_bytes(b"")
    second_content = json.dumps(_document()).encode()
    session = _Session([_Response(second_content)])

    documents = load_raw_tiles(bounds, tmp_path, session=session)

    assert documents == ({"elements": []}, _document())
    assert len(session.calls) == 1
    assert session.calls[0]["timeout"] == 120.0
    assert "gpx2stl/0.1" in str(session.calls[0]["headers"])
    assert 'way["building"]' in str(session.calls[0]["params"])
    assert 'way["bridge"]' in str(session.calls[0]["params"])
    assert 'way["man_made"="bridge"]' in str(session.calls[0]["params"])
    assert 'way["natural"~"water|coastline"]' in str(
        session.calls[0]["params"]
    )
    assert "out skel qt" in str(session.calls[0]["params"])
    assert (tmp_path / f"{tiles[1].cache_key}.json").read_bytes() == second_content
    assert not tuple(tmp_path.glob("*.partial"))


def test_uncached_tile_requests_are_paced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    bounds = GeographicBounds(
        0.001, 0.002, 0.001, OVERPASS_TILE_DEGREES + 0.002
    )
    body = json.dumps(_document()).encode()
    session = _Session([_Response(body), _Response(body)])
    sleeps: list[float] = []
    monkeypatch.setattr("gpx2stl.city.requests.Session", lambda: session)
    monkeypatch.setattr("gpx2stl.city.time.monotonic", lambda: 0.0)
    monkeypatch.setattr("gpx2stl.city.time.sleep", sleeps.append)

    load_raw_tiles(bounds, tmp_path)

    assert sleeps == [CITY_REQUEST_INTERVAL_SECONDS]


def test_fetch_failure_and_invalid_json_are_explicit_and_not_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    bounds = GeographicBounds(0.001, 0.002, 0.001, 0.002)
    monkeypatch.setattr("gpx2stl.city.time.sleep", lambda _: None)
    session = _Session([_Response(b"busy", 503) for _ in range(3)])
    with pytest.raises(Gpx2StlError, match="Unable to download"):
        load_raw_tiles(bounds, tmp_path, session=session)
    assert not tuple(tmp_path.glob("*.json"))

    session = _Session([_Response(b"<html>not json</html>")])
    with pytest.raises(Gpx2StlError, match="valid Overpass JSON"):
        load_raw_tiles(bounds, tmp_path, session=session)
    assert not tuple(tmp_path.glob("*.json"))


def test_overpass_failure_activates_map_fallback_for_remaining_tiles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    bounds = GeographicBounds(
        0.001, 0.002, 0.001, OVERPASS_TILE_DEGREES + 0.002
    )
    osm_xml = b"""\
<osm version="0.6">
  <node id="1" lat="0.001" lon="0.001"/>
  <node id="2" lat="0.001" lon="0.002"/>
  <way id="10">
    <nd ref="1"/><nd ref="2"/>
    <tag k="highway" v="residential"/>
  </way>
</osm>
"""
    session = _Session(
        [_Response(b"busy", 503) for _ in range(3)]
        + [_Response(osm_xml), _Response(osm_xml)]
    )
    monkeypatch.setattr("gpx2stl.city.requests.Session", lambda: session)
    monkeypatch.setattr("gpx2stl.city.time.sleep", lambda _: None)

    documents = load_raw_tiles(bounds, tmp_path)

    assert len(documents) == 2
    assert documents[0]["elements"][-1]["tags"] == {"highway": "residential"}
    assert [call["url"] for call in session.calls[-2:]] == [
        "https://api.openstreetmap.org/api/0.6/map",
        "https://api.openstreetmap.org/api/0.6/map",
    ]


@pytest.mark.parametrize(
    ("tags", "expected"),
    [
        ({"height": "12 m"}, 12.0),
        ({"height": "20 ft"}, 6.096),
        ({"height": "6' 2\""}, 1.8796),
        ({"building:levels": "2.5"}, 7.5),
        ({"height": "unknown", "building:levels": "3"}, 9.0),
        ({"height": "unknown", "building:levels": "unknown"}, 8.0),
    ],
)
def test_parse_height_metric_imperial_levels_and_fallback(
    tags: dict[str, str], expected: float
) -> None:
    assert parse_height(tags, 8.0) == pytest.approx(expected)


def test_parse_ways_relations_holes_and_deduplicates_adjacent_results() -> None:
    relation = {
        "type": "relation",
        "id": 30,
        "tags": {"type": "multipolygon", "building": "yes", "building:levels": "2"},
        "members": [
            {
                "type": "way",
                "ref": 31,
                "role": "outer",
                "geometry": [
                    {"lon": 1.0, "lat": 1.0},
                    {"lon": 1.004, "lat": 1.0},
                    {"lon": 1.004, "lat": 1.004},
                    {"lon": 1.0, "lat": 1.004},
                    {"lon": 1.0, "lat": 1.0},
                ],
            },
            {
                "type": "way",
                "ref": 32,
                "role": "inner",
                "geometry": [
                    {"lon": 1.001, "lat": 1.001},
                    {"lon": 1.002, "lat": 1.001},
                    {"lon": 1.002, "lat": 1.002},
                    {"lon": 1.001, "lat": 1.002},
                    {"lon": 1.001, "lat": 1.001},
                ],
            },
        ],
    }
    first = _document()
    first["elements"].extend(  # type: ignore[union-attr]
        [
            {
                "type": "way",
                "id": 21,
                "tags": {"highway": "construction"},
                "geometry": [
                    {"lon": 0.0, "lat": 0.0},
                    {"lon": 0.001, "lat": 0.001},
                ],
            },
            {
                "type": "way",
                "id": 22,
                "tags": {"highway": "pedestrian", "area": "yes"},
                "geometry": [
                    {"lon": 0.0, "lat": 0.0},
                    {"lon": 0.001, "lat": 0.001},
                ],
            },
            {
                "type": "way",
                "id": 23,
                "tags": {
                    "bridge": "yes",
                    "railway": "rail",
                    "width": "4 m",
                    "layer": "2",
                },
                "geometry": [
                    {"lon": 0.0, "lat": 0.001},
                    {"lon": 0.003, "lat": 0.001},
                ],
            },
            {
                "type": "way",
                "id": 24,
                "tags": {"natural": "water", "water": "pond"},
                "geometry": [
                    {"lon": 0.001, "lat": 0.001},
                    {"lon": 0.002, "lat": 0.001},
                    {"lon": 0.002, "lat": 0.002},
                    {"lon": 0.001, "lat": 0.002},
                    {"lon": 0.001, "lat": 0.001},
                ],
            },
            {
                "type": "way",
                "id": 25,
                "tags": {"waterway": "stream"},
                "geometry": [
                    {"lon": 0.0, "lat": 0.002},
                    {"lon": 0.003, "lat": 0.002},
                ],
            },
        ]
    )
    first["elements"].append(relation)  # type: ignore[union-attr]
    second = {"elements": [_document()["elements"][0], relation]}

    parsed = parse_city_data((first, second), default_height=9.0)

    assert [(item.osm_type, item.osm_id) for item in parsed.buildings] == [
        ("relation", 30),
        ("way", 10),
    ]
    assert parsed.buildings[0].height_m == 6.0
    assert len(parsed.buildings[0].polygon.interiors) == 1
    assert [road.osm_id for road in parsed.roads] == [20]
    assert len(parsed.bridges) == 1
    assert parsed.bridges[0].osm_id == 23
    assert parsed.bridges[0].width_m == 4.0
    assert parsed.bridges[0].height_m == 10.0
    assert len(parsed.water) == 2
    assert parsed.water[0].osm_id == 24
    assert parsed.water[0].width_m is None
    assert parsed.water[1].osm_id == 25
    assert parsed.water[1].width_m == 2.0


def test_projection_clips_buildings_and_roads_and_drops_outside() -> None:
    geographic = GeographicCityData(
        buildings=(
            GeographicBuilding("way", 1, box(-0.001, 0.0, 0.002, 0.002), 10.0),
            GeographicBuilding("way", 2, box(1.0, 1.0, 1.1, 1.1), 12.0),
        ),
        roads=(
            GeographicRoad(
                3,
                LineString([(-0.001, 0.001), (0.003, 0.001)]),
                "residential",
                6.0,
            ),
        ),
        bridges=(
            GeographicBridge(
                4,
                LineString([(-0.001, 0.0005), (0.003, 0.0005)]),
                4.0,
                5.0,
            ),
        ),
    )
    clip = box(0.0, 0.0, 200.0, 200.0)

    projected = project_city_data(geographic, _route(), clip)

    assert len(projected.buildings) == 1
    assert projected.buildings[0].polygon.bounds == pytest.approx((0.0, 0.0, 200.0, 200.0))
    assert projected.buildings[0].height_m == 10.0
    assert len(projected.roads) == 1
    assert projected.roads[0].line.bounds == pytest.approx(
        (0.0, 111.31949, 200.0, 111.31949)
    )
    assert projected.roads[0].width_m == 6.0
    assert len(projected.bridges) == 1
    assert projected.bridges[0].polygon.bounds == pytest.approx(
        (0.0, 53.659745, 200.0, 57.659745)
    )


@pytest.mark.parametrize(("tags", "expected"), [
    ({"highway": "residential", "width": "8 m", "lanes": "4"}, 8.0),
    ({"highway": "service", "width": "10'"}, 3.048),
    ({"highway": "secondary", "lanes": "3"}, 9.6),
    ({"highway": "motorway"}, 10.0),
    ({"highway": "primary_link"}, 7.0),
    ({"highway": "residential"}, 6.0),
    ({"highway": "service"}, 3.0),
    ({"highway": "footway"}, 2.5),
    ({"highway": "unclassified"}, 5.0),
    ({"highway": "service", "width": "-1", "lanes": "invalid"}, 3.0),
])
def test_osm_road_width_precedence_and_estimates(tags, expected) -> None:
    document = {"elements": [{
        "type": "way", "id": 1, "tags": tags,
        "geometry": [{"lon": 0.0, "lat": 0.0}, {"lon": 0.001, "lat": 0.0}],
    }]}
    parsed = parse_city_data(document, default_height=10.0)
    assert len(parsed.roads) == 1
    assert parsed.roads[0].width_m == pytest.approx(expected)


def test_clipped_road_parts_preserve_width() -> None:
    geographic = GeographicCityData(
        buildings=(),
        roads=(GeographicRoad(
            1, MultiLineString([
                [(-0.001, 0.0005), (0.003, 0.0005)],
                [(-0.001, 0.001), (0.003, 0.001)],
            ]), "service", 3.5,
        ),),
    )
    projected = project_city_data(geographic, _route(), box(0, 0, 200, 200))
    assert len(projected.roads) == 2
    assert [road.width_m for road in projected.roads] == [3.5, 3.5]


def test_projection_creates_lakes_rivers_and_sea_from_coastline() -> None:
    geographic = GeographicCityData(
        buildings=(),
        roads=(),
        water=(
            GeographicWater(
                1,
                box(0.0001, 0.0001, 0.0004, 0.0004),
            ),
            GeographicWater(
                2,
                LineString([(0.0001, 0.0007), (0.0017, 0.0007)]),
                width_m=4.0,
            ),
            GeographicWater(
                3,
                LineString([(0.0009, -0.001), (0.0009, 0.003)]),
                coastline=True,
            ),
        ),
    )

    projected = project_city_data(
        geographic,
        _route(),
        box(0.0, 0.0, 200.0, 200.0),
    )

    assert len(projected.water) >= 2
    merged = unary_union(projected.water)
    assert merged.covers(box(150.0, 10.0, 190.0, 190.0))
    assert not merged.covers(box(10.0, 100.0, 40.0, 190.0))


def test_projection_reconstructs_partial_river_relation_between_banks() -> None:
    geographic = GeographicCityData(
        buildings=(),
        roads=(),
        water=(
            GeographicWater(
                1,
                LineString([(0.0009, -0.001), (0.0009, 0.003)]),
                width_m=4.0,
            ),
            GeographicWater(
                2,
                MultiLineString(
                    [
                        [(0.0007, -0.001), (0.0007, 0.003)],
                        [(0.0011, -0.001), (0.0011, 0.003)],
                    ]
                ),
                boundary=True,
            ),
        ),
    )

    projected = project_city_data(
        geographic,
        _route(),
        box(0.0, 0.0, 200.0, 200.0),
    )

    merged = unary_union(projected.water)
    assert merged.covers(box(80.0, 10.0, 120.0, 190.0))
    assert not merged.covers(box(10.0, 10.0, 60.0, 190.0))


def test_load_city_data_composes_cache_parse_project_and_progress(tmp_path: Path) -> None:
    bounds = GeographicBounds(0.0001, 0.002, 0.0001, 0.002)
    session = _Session([_Response(json.dumps(_document()).encode())])
    progress: list[str] = []

    result = load_city_data(
        bounds,
        tmp_path,
        _route(),
        box(0.0, 0.0, 300.0, 300.0),
        9.0,
        progress=progress.append,
        session=session,
    )

    assert len(result.buildings) == 1
    assert len(result.roads) == 1
    assert progress == ["Downloading city data tile 1/1"]
