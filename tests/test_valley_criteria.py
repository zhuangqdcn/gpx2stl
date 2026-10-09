from dataclasses import replace

import numpy as np
import pytest
from scipy import ndimage

from gpx2stl import auto_boundary as boundary
from gpx2stl.errors import Gpx2StlError


@pytest.mark.parametrize(
    "field",
    ["max_relief_m", "max_slope_percent", "max_height_m", "max_height_percent"],
)
@pytest.mark.parametrize("value", [-1, float("nan"), float("inf"), True, "10", None])
def test_invalid_valley_criteria(field, value):
    with pytest.raises(Gpx2StlError, match="finite nonnegative"):
        boundary.ValleyCriteria(**{field: value})


def test_height_percentage_cannot_exceed_observed_relief():
    with pytest.raises(Gpx2StlError, match="not exceed 100"):
        boundary.ValleyCriteria(max_height_percent=100.1)


def test_strict_valley_mask_matches_original_conditions():
    y, x = np.mgrid[-40:41, -40:41]
    height = ndimage.gaussian_filter(
        500 * np.maximum(0, 1 - np.hypot(x, y) / 20), 1.5
    )
    floor = float(np.percentile(height, 10))
    relief = float(height.max() - floor)
    dy, dx = np.gradient(height, 90)
    expected = (
        (height <= floor + min(20, 0.03 * relief))
        & (
            ndimage.maximum_filter(height, size=11)
            - ndimage.minimum_filter(height, size=11) <= 1
        )
        & (np.hypot(dx, dy) <= 0.002)
    )
    actual = boundary._valley_background(
        height, floor, relief, boundary.ValleyCriteria(1, 0.2, 20, 3), 11
    )
    assert np.any(expected)
    np.testing.assert_array_equal(actual, expected)


def test_default_valley_markers_include_sloping_high_cells():
    height = np.tile(np.arange(21) * 45.0, (21, 1))
    defaults = boundary.ValleyCriteria()
    assert defaults == boundary.ValleyCriteria(1000, 100, 1000, 100)
    permissive = boundary._valley_background(height, 0, 900, defaults, 11)
    strict = boundary._valley_background(
        height, 0, 900, boundary.ValleyCriteria(1, 0.2, 20, 3), 11
    )
    assert np.all(permissive)
    assert not np.any(strict)
    assert not np.any(boundary._valley_background(height, -1001, 2000, defaults, 11))


def test_default_segment_treats_permissive_summit_cells_as_background():
    y, x = np.mgrid[-40:41, -40:41]
    elevation = 500 * np.maximum(0, 1 - np.hypot(x, y) / 20) + x * 0.9
    corridor = np.zeros(elevation.shape, dtype=np.bool_)
    corridor[40, 45:51] = True
    selected, count, reason = boundary._segment(elevation, corridor)
    assert count == 0
    assert reason == "no resolved summit markers"
    assert np.all(selected)
    explicit = boundary._segment(
        elevation, corridor, boundary.ValleyCriteria(1000, 100, 1000, 100)
    )
    np.testing.assert_array_equal(selected, explicit[0])
    assert (count, reason) == explicit[1:]


@pytest.mark.parametrize(
    "field",
    ["max_relief_m", "max_slope_percent", "max_height_m", "max_height_percent"],
)
def test_each_threshold_is_inclusive_and_independently_controls_valley_cells(field):
    height = np.tile(100 + np.arange(21) * 0.9, (21, 1))
    floor, relief = 100.0, 100.0
    center = (10, 10)
    dy, dx = np.gradient(height, boundary.GRID_SPACING_M)
    measurements = {
        "max_relief_m": (
            ndimage.maximum_filter(height, size=11)
            - ndimage.minimum_filter(height, size=11)
        )[center],
        "max_slope_percent": 100 * np.hypot(dx, dy)[center],
        "max_height_m": height[center] - floor,
        "max_height_percent": 100 * (height[center] - floor) / relief,
    }
    generous = boundary.ValleyCriteria(30, 5, 100, 100)
    at_limit = replace(generous, **{field: float(measurements[field])})
    below_limit = replace(
        generous, **{field: float(measurements[field]) - 1e-6}
    )
    assert boundary._valley_background(height, floor, relief, at_limit, 11)[center]
    assert not boundary._valley_background(height, floor, relief, below_limit, 11)[center]


def test_zero_thresholds_accept_only_exact_flat_floor():
    height = np.full((21, 21), 100.0)
    criteria = boundary.ValleyCriteria(0, 0, 0, 0)
    assert np.all(boundary._valley_background(height, 100, 100, criteria, 11))
    assert not np.any(boundary._valley_background(height, 99, 100, criteria, 11))


def test_relaxing_thresholds_expands_marker_set_without_bypassing_other_checks():
    y, x = np.mgrid[-40:41, -40:41]
    height = ndimage.gaussian_filter(
        500 * np.maximum(0, 1 - np.hypot(x, y) / 20) + x * 0.9, 1.5
    )
    floor = float(np.percentile(height, 10))
    relief = float(height.max() - floor)
    strict = boundary._valley_background(
        height, floor, relief, boundary.ValleyCriteria(1, 0.2, 20, 3), 11
    )
    relaxed = boundary._valley_background(
        height, floor, relief, boundary.ValleyCriteria(20, 2, 100, 20), 11
    )
    assert not np.any(strict)
    assert np.count_nonzero(relaxed) > 0
    assert np.all(~strict | relaxed)
    assert not relaxed[40, 40]


def test_relaxed_valley_markers_enable_segmentation_of_a_sloping_floor():
    y, x = np.mgrid[-40:41, -40:41]
    elevation = 500 * np.maximum(0, 1 - np.hypot(x, y) / 20) + x * 0.9
    corridor = np.zeros(elevation.shape, dtype=np.bool_)
    corridor[40, 45:51] = True
    _, strict_count, strict_reason = boundary._segment(
        elevation, corridor, boundary.ValleyCriteria(1, 0.2, 20, 3)
    )
    selected, relaxed_count, relaxed_reason = boundary._segment(
        elevation, corridor, boundary.ValleyCriteria(20, 2, 100, 20)
    )
    assert strict_count == 0
    assert strict_reason == "no observable valley floor"
    assert relaxed_count == 1
    assert relaxed_reason is None
    assert np.all(selected[corridor])
