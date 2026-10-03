from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import numpy as np
from numpy.typing import NDArray
from pyproj import Transformer

Shape = Literal["square", "circle", "hex"]
TopoSource = Literal["auto", "online", "local"]


@dataclass(frozen=True)
class Config:
    gpx_file: Path
    output: Path
    route_width: float = 1.0
    route_height: float = 2.0
    topo: bool = True
    boundary_percent: float = 10.0
    shape: Shape = "square"
    text: str | None = None
    text_height: float = 1.0
    inner_size_percent: float = 70.0
    font_file: Path | None = None
    base_stl: Path | None = None
    use_3mf: bool = True
    max_size: float = 200.0
    terrain_height: float | None = None
    base_height: float = 2.0
    topo_source: TopoSource = "auto"
    topo_file: Path | None = None
    topo_dir: Path = Path("asset")
    dem_type: str | None = None
    api_key: str | None = None
    force: bool = False


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
