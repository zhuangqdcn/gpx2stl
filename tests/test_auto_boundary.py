"""Controlled geometric evidence, not assertions about named mountains."""

from dataclasses import FrozenInstanceError

import numpy as np
import pytest
from pyproj import Transformer
from rasterio import Affine
from rasterio.coords import BoundingBox
from shapely.geometry import LineString, Point, box

from gpx2stl import auto_boundary as boundary
from gpx2stl.dem import DemSource, DemTile
from gpx2stl.errors import Gpx2StlError
from gpx2stl.models import ProjectedRoute


class AnalyticDem(DemSource):
    def __init__(self, function):
        super().__init__(tiles=())
        object.__setattr__(self, "function", function)
        object.__setattr__(self, "samples", [])

    def sample_projected(self, points, route):
        self.samples.append(points.copy())
        return self.function(points[:, 0], points[:, 1])


def route(*paths):
    forward = Transformer.from_crs(4326, 32610, always_xy=True)
    inverse = Transformer.from_crs(32610, 4326, always_xy=True)
    arrays = tuple(np.asarray(path, dtype=np.float64) + [500000, 4000000] for path in paths)
    return ProjectedRoute(arrays, tuple(np.zeros(len(p)) for p in arrays), forward, inverse)


def cone(x, y, cx=0, cy=0, rx=1200, ry=1200, height=500):
    """Exact elliptical foot contour, with genuinely flat valley outside it."""
    distance = np.hypot((x - 500000 - cx) / rx, (y - 4000000 - cy) / ry)
    return height * np.maximum(0, 1 - distance)


def discover(activity, function, cap=20, *, valley_criteria=boundary.ValleyCriteria()):
    dem = AnalyticDem(function)
    requests = []

    def load(bounds):
        requests.append(bounds)
        return dem

    result = boundary.discover_auto_boundary(
        activity, cap, load, progress=lambda _: None, valley_criteria=valley_criteria
    )
    return result, dem, requests


def assert_contains_foot(result, cx=0, cy=0, rx=1200, ry=1200):
    angles = np.linspace(0, 2 * np.pi, 64, endpoint=False)
    for angle in angles:
        assert result.envelope.covers(
            Point(500000 + cx + rx * np.cos(angle), 4000000 + cy + ry * np.sin(angle))
        )
    assert all(item.resolved for item in result.directions)


def test_whole_asymmetric_mountain_and_unvisited_summit():
    activity = route([[-900, -150], [-850, 150]])
    result, _, requests = discover(
        activity, lambda x, y: cone(x, y, cx=900, rx=2600, ry=1400)
    )
    assert_contains_foot(result, cx=900, rx=2600, ry=1400)
    assert result.envelope.covers(Point(500900, 4000000))
    assert result.envelope.covers(LineString(activity.paths[0]))
    assert result.region_count == 1
    assert result.search_distance_m >= 8000
    assert len(requests) >= 3
    with pytest.raises(FrozenInstanceError):
        result.region_count = 2


def test_complete_sparse_segment_selects_multiple_mountains_not_distant_peak():
    activity = route([[-2300, 0], [2300, 0]])
    result, _, _ = discover(
        activity,
        lambda x, y: (
            cone(x, y, cx=-1200, rx=850, ry=1000)
            + cone(x, y, cx=1200, rx=850, ry=1000)
            + cone(x, y, cx=0, cy=3200, rx=700, ry=700, height=800)
        ),
    )
    assert result.region_count == 2
    assert_contains_foot(result, cx=-1200, rx=850, ry=1000)
    assert_contains_foot(result, cx=1200, rx=850, ry=1000)
    assert result.envelope.covers(LineString(activity.paths[0]))
    assert not result.envelope.covers(Point(500000, 4003200))


def test_disconnected_activity_paths_do_not_select_intervening_peak():
    activity = route([[-2400, -100], [-2200, 100]], [[2200, -100], [2400, 100]])
    result, _, _ = discover(
        activity,
        lambda x, y: (
            cone(x, y, cx=-2200, rx=800, ry=800)
            + cone(x, y, cx=2200, rx=800, ry=800)
            + cone(x, y, cx=0, rx=500, ry=500)
        ),
    )
    assert result.region_count == 2


def test_shallow_saddle_massif_is_merged_but_deep_saddle_is_not():
    activity = route([[-750, -100], [750, 100]])
    shallow, _, _ = discover(
        activity,
        lambda x, y: np.maximum(
            cone(x, y, cx=-450, rx=3000, ry=1500, height=600),
            cone(x, y, cx=450, rx=3000, ry=1500, height=600),
        ),
    )
    deep, _, _ = discover(
        activity,
        lambda x, y: np.maximum(
            cone(x, y, cx=-750, rx=1700, ry=1300, height=600),
            cone(x, y, cx=750, rx=1700, ry=1300, height=600),
        ),
    )
    assert shallow.region_count == 1
    assert deep.region_count == 2


def test_metric_smoothing_suppresses_noise():
    activity = route([[-200, -200], [200, 200]])
    clean, _, _ = discover(activity, lambda x, y: cone(x, y))
    noisy, _, _ = discover(
        activity,
        lambda x, y: cone(x, y) + 4 * np.sin(x / 35) * np.cos(y / 31),
    )
    assert noisy.region_count == clean.region_count == 1
    assert np.allclose(noisy.envelope.bounds, clean.envelope.bounds, atol=180)


def test_grid_is_anchored_and_sampling_is_batched(monkeypatch):
    monkeypatch.setattr(boundary, "SAMPLE_BATCH_SIZE", 200)
    _, dem, _ = discover(route([[-200, 0], [200, 0]]), lambda x, y: cone(x, y))
    assert all(len(points) <= 200 for points in dem.samples)
    points = np.concatenate(dem.samples)
    assert np.allclose(points / 90, np.rint(points / 90))
    assert len(np.unique(points, axis=0)) < len(points)


@pytest.mark.parametrize(
    "function, message",
    [
        (lambda x, y: np.full_like(x, np.nan), "nodata"),
        (lambda x, y: np.full_like(x, np.inf), "nodata"),
        (lambda x, y: np.where(x > 500100, np.nan, cone(x, y)), "nodata"),
    ],
)
def test_missing_data_are_explicit_errors(function, message):
    with pytest.raises(Gpx2StlError, match=message) as error:
        discover(route([[-200, 0], [200, 0]]), function)
    assert "--route-boundary-percent" in str(error.value)


def test_relaxed_valley_criteria_do_not_bypass_missing_dem_errors():
    with pytest.raises(Gpx2StlError, match="nodata"):
        discover(
            route([[-200, 0], [200, 0]]),
            lambda x, y: np.full_like(x, np.nan),
            valley_criteria=boundary.ValleyCriteria(100, 10, 500, 50),
        )


def test_discovery_passes_valley_criteria_and_reports_active_thresholds(monkeypatch):
    criteria = boundary.ValleyCriteria(20, 2, 100, 20)
    seen = []

    def segment(elevation, corridor, actual_criteria):
        seen.append(actual_criteria)
        return np.ones(elevation.shape, dtype=np.bool_), 0, "no observable valley floor"

    monkeypatch.setattr(boundary, "_segment", segment)
    messages = []
    dem = AnalyticDem(lambda x, y: np.zeros_like(x))
    result = boundary.discover_auto_boundary(
        route([[-200, 0], [200, 0]]), 2, lambda _: dem, messages.append,
        valley_criteria=criteria,
    )
    assert seen == [criteria, criteria]
    assert all(not item.resolved for item in result.directions)
    assert any(
        "local variation <= 20 m" in message
        and "slope <= 2%" in message
        and "min(100 m, 20%" in message
        for message in messages
    )


def test_unresolved_edge_falls_back_at_cap_without_claiming_detection():
    activity = route([[-200, 0], [200, 0]])
    result, _, _ = discover(
        activity, lambda x, y: cone(x, y, rx=12000, ry=12000, height=3000), cap=2,
    )
    assert result.search_distance_m == 2000
    assert not any(item.resolved for item in result.directions)
    assert result.envelope.covers(LineString(activity.paths[0]))


def test_analytic_elongated_mountain_keeps_independent_north_south_detection():
    activity = route([[-200, -100], [200, 100]])
    result, _, _ = discover(
        activity, lambda x, y: cone(x, y, rx=12000, ry=1200, height=1000), cap=8,
    )
    directional = {item.direction: item for item in result.directions}
    assert directional["N"].resolved and directional["S"].resolved
    assert not directional["E"].resolved and not directional["W"].resolved
    assert directional["N"].padding_m > directional["N"].span_m
    assert result.envelope.covers(LineString(activity.paths[0]))


def test_route_on_flat_valley_does_not_pick_nearest_mountain():
    result, _, _ = discover(
        route([[-200, 0], [200, 0]]),
        lambda x, y: cone(x, y, cy=1600, rx=500, ry=500),
    )
    assert result.region_count == 0
    assert all(not item.resolved and "does not touch" in item.reason for item in result.directions)


def test_grid_budget_is_checked_before_loading():
    activity = route([[0, 0], [200000, 200000]])
    with pytest.raises(Gpx2StlError, match="resource budget"):
        boundary.discover_auto_boundary(
            activity, 20, lambda _: pytest.fail("must not load an oversized grid")
        )


@pytest.mark.parametrize("cap", [0, -1, float("nan"), float("inf")])
def test_invalid_search_cap(cap):
    with pytest.raises(Gpx2StlError, match="finite and positive"):
        discover(route([[0, 0], [100, 0]]), lambda x, y: cone(x, y), cap=cap)


def test_small_cap_uses_two_nested_searches():
    result, _, requests = discover(
        route([[-100, 0], [100, 0]]),
        lambda x, y: cone(x, y, rx=300, ry=300),
        cap=2,
    )
    assert result.search_distance_m == 2000
    assert len(requests) == 2


def test_summit_plateau_is_one_marker():
    activity = route([[-400, -100], [400, 100]])
    result, _, _ = discover(
        activity, lambda x, y: np.minimum(350, cone(x, y, rx=1600, ry=1300))
    )
    assert result.region_count == 1
    assert_contains_foot(result, rx=1600, ry=1300)


def test_finite_coverage_error_is_actionable():
    class MissingCoverage(AnalyticDem):
        def sample_projected(self, points, route):
            raise Gpx2StlError("outside local tiles")

    dem = MissingCoverage(lambda x, y: cone(x, y))
    with pytest.raises(Gpx2StlError, match="incomplete DEM coverage"):
        boundary.discover_auto_boundary(
            route([[0, 0], [100, 0]]), 20, lambda _: dem, progress=lambda _: None
        )


def test_coarse_dem_is_rejected_before_sampling():
    activity = route([[0, 0], [100, 0]])
    tile = DemTile(
        data=np.ones((100, 100)),
        transform=Affine(1000, 0, 450000, 0, -1000, 4050000),
        bounds=BoundingBox(450000, 3950000, 550000, 4050000),
        crs=activity.forward.target_crs,
        source="coarse fixture",
    )
    with pytest.raises(Gpx2StlError, match="resolution is too coarse"):
        boundary.discover_auto_boundary(
            activity, 20, lambda _: DemSource((tile,)), progress=lambda _: None
        )


def projected_tile(activity, pixel_m, west=494000, south=3994000, size_m=12000):
    size = int(np.ceil(size_m / pixel_m))
    return DemTile(
        data=np.ones((size, size)),
        transform=Affine(pixel_m, 0, west, 0, -pixel_m, south + size_m),
        bounds=BoundingBox(west, south, west + size_m, south + size_m),
        crs=activity.forward.target_crs,
        source=f"{pixel_m} m resolution fixture",
    )


def test_unrelated_fine_tile_cannot_conceal_coarse_contributing_coverage():
    activity = route([[0, 0], [100, 0]])
    fine = projected_tile(activity, 90, west=600000)
    coarse = projected_tile(activity, 1000)
    with pytest.raises(Gpx2StlError, match="contributing DEM resolution is too coarse"):
        boundary.discover_auto_boundary(
            activity, 20, lambda _: DemSource((fine, coarse)), progress=lambda _: None
        )


def test_partially_covering_fine_tile_cannot_conceal_coarse_remainder():
    activity = route([[0, 0], [100, 0]])
    fine = projected_tile(activity, 90, west=494000, size_m=6300)
    coarse = projected_tile(activity, 1000)
    with pytest.raises(Gpx2StlError, match="contributing DEM resolution is too coarse"):
        boundary.discover_auto_boundary(
            activity, 20, lambda _: DemSource((fine, coarse)), progress=lambda _: None
        )


def test_complete_fine_coverage_can_supersede_unused_coarse_tiles():
    activity = route([[0, 0], [100, 0]])
    fine = projected_tile(activity, 90)
    coarse = projected_tile(activity, 1000)
    grid = boundary._grid(np.array([498000, 3998000, 502000, 4002000]))
    boundary._check_resolution(DemSource((fine, coarse)), activity, grid)
    with pytest.raises(Gpx2StlError, match="contributing DEM resolution"):
        boundary._check_resolution(DemSource((coarse, fine)), activity, grid)


def test_fine_nodata_does_not_hide_coarse_fallback():
    activity = route([[0, 0], [100, 0]])
    fine = projected_tile(activity, 90)
    fine.data[:, fine.data.shape[1] // 2 :] = np.nan
    coarse = projected_tile(activity, 1000)
    grid = boundary._grid(np.array([498000, 3998000, 502000, 4002000]))
    with pytest.raises(Gpx2StlError, match="contributing DEM resolution"):
        boundary._check_resolution(DemSource((fine, coarse)), activity, grid)


def test_low_initial_relief_expands_to_whole_gentle_mountain_foot():
    activity = route([[-11000, -100], [-10900, 100]])
    result, _, requests = discover(
        activity, lambda x, y: cone(x, y, rx=12000, ry=12000, height=150), cap=40
    )
    assert len(requests) >= 5
    assert result.search_distance_m == 40000
    assert result.region_count == 1
    assert result.envelope.covers(Point(500000, 4000000))
    assert_contains_foot(result, rx=12000, ry=12000)


def test_gentle_mountain_foot_beyond_cap_reports_partial_fallback():
    activity = route([[-11000, -100], [-10900, 100]])
    result, _, _ = discover(
        activity, lambda x, y: cone(x, y, rx=12000, ry=12000, height=150), cap=20
    )
    assert not all(item.resolved for item in result.directions)
    assert result.envelope.covers(LineString(activity.paths[0]))


def test_broad_low_cone_cannot_use_its_gentle_outer_slope_as_background():
    activity = route([[-100, 0], [100, 0]])
    result, _, _ = discover(
        activity, lambda x, y: cone(x, y, rx=32000, ry=32000, height=63.5), cap=44
    )
    assert result.region_count == 1
    assert_contains_foot(result, rx=32000, ry=32000)


def test_genuinely_flat_terrain_retries_through_cap_before_fallback():
    activity = route([[0, 0], [100, 0]])
    messages = []
    dem = AnalyticDem(lambda x, y: np.zeros_like(x))
    result = boundary.discover_auto_boundary(activity, 8, lambda _: dem, progress=messages.append)
    windows = [message for message in messages if message.startswith("Discovering")]
    assert [message.split(": ")[1].split(" km")[0] for message in windows] == ["2", "4", "8"]
    assert result.region_count == 0
    assert len(result.directions) == 8
    assert all(not item.resolved and "flat or ambiguous" in item.reason for item in result.directions)
    assert not any("valley boundary found" in message for message in messages)
    assert sum("using 100% fallback" in message for message in messages) == 8


def test_cap_is_not_exceeded_and_grid_has_no_silent_coarsening(monkeypatch):
    activity = route([[0, 0], [100, 0]])
    distances = []
    monkeypatch.setattr(
        boundary, "request_projected_bounds", lambda perimeter, _: perimeter
    )
    dem = AnalyticDem(lambda x, y: cone(x, y, rx=30000, ry=30000, height=6000))

    def load(perimeter):
        distances.append(float(activity.points[:, 1].min() - perimeter[0, 1]))
        return dem

    result = boundary.discover_auto_boundary(activity, 20, load, progress=lambda _: None)
    assert not any(item.resolved for item in result.directions)
    assert distances == [2000, 4000, 8000, 16000, 20000]


def test_missing_values_are_never_filled(monkeypatch):
    monkeypatch.setattr(
        "gpx2stl.dem.fill_missing", lambda _: pytest.fail("must not fill discovery nodata")
    )
    with pytest.raises(Gpx2StlError, match="nodata"):
        discover(route([[0, 0], [100, 0]]), lambda x, y: np.full_like(x, np.nan))


def test_actual_projected_dem_sampler_and_result_source_are_reused():
    activity = route([[-200, 0], [200, 0]])
    transform = Affine(90, 0, 490000, 0, -90, 4010000)
    rows, cols = np.indices((223, 223))
    x = transform.c + (cols + 0.5) * 90
    y = transform.f - (rows + 0.5) * 90
    tile = DemTile(
        data=cone(x, y),
        transform=transform,
        bounds=BoundingBox(490000, 3989930, 510070, 4010000),
        crs=activity.forward.target_crs,
        source="90 m projected cone fixture",
    )
    dem = DemSource((tile,), require_complete_coverage=True)
    result = boundary.discover_auto_boundary(
        activity, 20, lambda _: dem, progress=lambda _: None
    )
    assert result.dem is dem
    assert result.region_count == 1
    assert_contains_foot(result)


def test_request_uses_existing_antimeridian_split():
    forward = Transformer.from_crs(
        4326, "+proj=aeqd +lat_0=0 +lon_0=180 +datum=WGS84 +units=m", always_xy=True
    )
    inverse = Transformer.from_crs(forward.target_crs, 4326, always_xy=True)
    points = np.array([[-100, 0], [100, 0]], dtype=np.float64)
    activity = ProjectedRoute((points,), (np.zeros(2),), forward, inverse)
    requests = []
    dem = AnalyticDem(
        lambda x, y: 500 * np.maximum(0, 1 - np.hypot(x, y) / 1200)
    )

    def load(bounds):
        requests.append(bounds)
        return dem

    result = boundary.discover_auto_boundary(
        activity, 20, load, progress=lambda _: None
    )
    assert result.region_count == 1
    assert all(len(bounds) == 2 for bounds in requests)
    assert all(bounds[0].east == 180 and bounds[1].west == -180 for bounds in requests)
