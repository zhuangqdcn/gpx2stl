from __future__ import annotations

from gpx2stl.dem import OpenTopographyClient, choose_dem_type, request_bounds
from gpx2stl.export import export_geometry
from gpx2stl.footprint import (
    add_route_clearance,
    create_footprint,
    create_model_transform,
)
from gpx2stl.gpx import interpolate_elevations, project_paths, read_gpx
from gpx2stl.mesh import build_geometry
from gpx2stl.models import Config


def convert(config: Config) -> None:
    paths = read_gpx(config.gpx_file)
    if not config.topo:
        paths = tuple(interpolate_elevations(path) for path in paths)
    route = project_paths(paths)
    minimum_footprint = config.route_width / config.max_size
    footprint = create_footprint(
        route.points,
        config.shape,
        config.boundary_percent,
        minimum_footprint,
    )
    footprint = add_route_clearance(footprint, config.route_width, config.max_size)
    transform = create_model_transform(footprint, config.max_size)

    dem = None
    if config.topo:
        bounds = request_bounds(footprint, route)
        dem_type = choose_dem_type(bounds, config.dem_type)
        dem = OpenTopographyClient(config.api_key or "").fetch(bounds, dem_type)
    geometry = build_geometry(route, footprint, transform, config, dem)
    export_geometry(geometry, config)
