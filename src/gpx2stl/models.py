from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import numpy as np
from numpy.typing import NDArray
from pyproj import Transformer

Shape = Literal["square", "circle", "hex"]
Mode = Literal["topo", "city"]
TopoSource = Literal["auto", "online", "local"]
FontWeight = Literal["normal", "bold"]
FontStyle = Literal["normal", "italic"]
TextAlign = Literal["left", "center", "right"]
TextMode = Literal["raised", "embedded"]
DirectionalRouteBoundary = tuple[float, float, float, float]
RouteBoundaryPercent = (
    float | DirectionalRouteBoundary | Literal["auto", "search"]
)
ResolvedRouteBoundary = float | DirectionalRouteBoundary | Literal["search"]


@dataclass(frozen=True)
class Config:
    gpx_file: Path
    output: Path
    mode: Mode = "topo"
    route_width: float = 1.0
    route_height: float = 2.0
    route_depth: float = 0.6
    road_snap_distance: float = 5.0
    topo: bool = True
    route_boundary_percent: RouteBoundaryPercent | None = None
    auto_boundary_max_distance_km: float = 10.0
    auto_valley_max_relief_m: float = 1000.0
    auto_valley_max_slope_percent: float = 100.0
    auto_valley_max_height_m: float = 1000.0
    auto_valley_max_height_percent: float = 100.0
    text_boundary_percent: float = 7.0
    shape: Shape = "square"
    text: str | None = None
    text_height: float = 1.0
    text_margin: float | None = None
    text_end_gap: float = 0.0
    text_align: TextAlign = "center"
    text_mode: TextMode = "raised"
    text_depth: float = 0.6
    font_family: str = "DejaVu Sans"
    font_file: Path | None = None
    font_size: float | None = None
    font_weight: FontWeight = "normal"
    font_style: FontStyle = "normal"
    base_stl: Path | None = None
    use_3mf: bool = True
    max_size: float = 200.0
    terrain_height: float | None = None
    base_height: float = 2.0
    topo_source: TopoSource = "auto"
    topo_file: Path | None = None
    topo_dir: Path = Path("asset")
    city_dir: Path = Path("asset/city")
    building_default_height: float = 10.0
    building_height_scale: float = 5.0
    water_depth: float = 0.4
    dem_type: str | None = None
    api_key: str | None = None
    force: bool = False

    @property
    def resolved_route_boundary_percent(self) -> ResolvedRouteBoundary:
        boundary = self.route_boundary_percent
        if boundary is None:
            if self.mode == "city":
                return (
                    10.0
                    if self.base_stl is not None
                    else (10.0, 10.0, 10.0, 10.0)
                )
            boundary = "auto" if self.topo else 10.0
        if boundary == "auto":
            return 5.0 if self.mode == "city" else "search"
        return boundary

    @property
    def terrain_size(self) -> float:
        if self.text is None:
            return self.max_size
        return self.max_size * (1.0 - 2.0 * self.text_boundary_percent / 100.0)


@dataclass(frozen=True)
class GeoPath:
    longitude: NDArray[np.float64]
    latitude: NDArray[np.float64]
    elevation: NDArray[np.float64]


@dataclass(frozen=True)
class ProjectedRoute:
    paths: tuple[NDArray[np.float64], ...]
    elevations: tuple[NDArray[np.float64], ...]
    forward: Transformer
    inverse: Transformer

    @property
    def points(self) -> NDArray[np.float64]:
        return np.concatenate(self.paths)


@dataclass(frozen=True)
class Footprint:
    shape: Shape
    center: NDArray[np.float64]
    radius: float

    @property
    def min_xy(self) -> NDArray[np.float64]:
        return self.center - self.radius

    @property
    def max_xy(self) -> NDArray[np.float64]:
        return self.center + self.radius

    @property
    def diameter(self) -> float:
        return self.radius * 2.0


@dataclass(frozen=True)
class ModelTransform:
    footprint: Footprint
    scale: float
    model_center: NDArray[np.float64] = field(
        default_factory=lambda: np.zeros(2, dtype=np.float64)
    )

    def to_model(self, points: NDArray[np.float64]) -> NDArray[np.float64]:
        return (points - self.footprint.center) * self.scale + self.model_center

    def to_projected(self, points: NDArray[np.float64]) -> NDArray[np.float64]:
        return (points - self.model_center) / self.scale + self.footprint.center
