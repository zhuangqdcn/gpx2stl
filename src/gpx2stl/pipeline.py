from __future__ import annotations

import numpy as np
import shapely
from numpy.typing import NDArray
from shapely.geometry import LineString, Polygon

from gpx2stl.activity import read_activity
from gpx2stl.auto_boundary import discover_auto_boundary
from gpx2stl.custom_base import CustomBase, prepare_custom_base, sample_exterior
from gpx2stl.dem import (
    DemSource,
    GeographicBounds,
    OpenTopographyClient,
    cache_copernicus_tiles,
    choose_dem_type,
    dem_covers_bounds,
    load_local_dem,
    request_bounds,
    request_projected_bounds,
)
from gpx2stl.errors import Gpx2StlError
from gpx2stl.export import export_geometry
from gpx2stl.footprint import (
    add_route_clearance,
    create_footprint,
    create_model_transform,
    footprint_vertices,
)
from gpx2stl.gpx import interpolate_elevations, project_paths
from gpx2stl.mesh import build_geometry
from gpx2stl.models import Config, Footprint, ModelTransform, ProjectedRoute
from gpx2stl.progress import ProgressCallback, console_progress


class _FiniteDemSource(DemSource):
    def sample_lonlat(
        self, longitude: NDArray[np.float64], latitude: NDArray[np.float64]
    ) -> NDArray[np.float64]:
        values = super().sample_lonlat(longitude, latitude)
        if not np.all(np.isfinite(values)):
            raise Gpx2StlError(
                "Automatic terrain requires complete finite DEM coverage for the "
                "final printable footprint; use better terrain data or a numeric boundary."
            )
        return values


def _generated_perimeter(footprint: Footprint) -> NDArray[np.float64]:
    if footprint.shape == "circle":
        angles = np.linspace(0.0, 2.0 * np.pi, 720, endpoint=False)
        return footprint.center + footprint.radius * np.column_stack(
            (np.cos(angles), np.sin(angles))
        )
    return sample_exterior(Polygon(footprint_vertices(footprint)))


def _validate_auto_fit(
    envelope: Polygon,
    route: ProjectedRoute,
    footprint: Footprint,
    transform: ModelTransform,
    custom_base: CustomBase | None,
    route_width: float,
) -> None:
    if custom_base is not None:
        model_envelope = shapely.transform(envelope, transform.to_model)
        model_route = shapely.union_all(
            [LineString(transform.to_model(path)) for path in route.paths]
        )
        fits = custom_base.terrain_polygon.covers(model_envelope) and (
            custom_base.terrain_polygon.buffer(
                -route_width / 2.0, quad_segs=32, join_style="round"
            ).covers(model_route)
        )
    elif footprint.shape == "circle":
        # A circle is convex: these extrema also enclose all segments and polygon interiors.
        envelope_radius = np.linalg.norm(
            np.asarray(envelope.exterior.coords) - footprint.center, axis=1
        ).max()
        route_radius = np.linalg.norm(route.points - footprint.center, axis=1).max()
        tolerance = max(1e-7, footprint.radius * 1e-10)
        fits = envelope_radius <= footprint.radius + tolerance and (
            route_radius + route_width / (2.0 * transform.scale)
            <= footprint.radius + tolerance
        )
    else:
        terrain = Polygon(transform.to_model(footprint_vertices(footprint)))
        model_envelope = shapely.transform(envelope, transform.to_model)
        model_route = shapely.union_all(
            [LineString(transform.to_model(path)) for path in route.paths]
        )
        fits = terrain.buffer(1e-7).covers(model_envelope) and (
            terrain.buffer(
                -route_width / 2.0 + 1e-7, quad_segs=32, join_style="round"
            ).covers(model_route)
        )
    if not fits:
        raise Gpx2StlError(
            "The entire detected terrain envelope and route ribbon do not fit "
            "inside the printable terrain footprint."
        )


def _report_auto_clearances(
    perimeter: NDArray[np.float64], route: ProjectedRoute, progress: ProgressCallback
) -> None:
    minimum = perimeter.min(axis=0)
    maximum = perimeter.max(axis=0)
    route_minimum = route.points.min(axis=0)
    route_maximum = route.points.max(axis=0)
    spans = route_maximum - route_minimum
    sides = (
        ("west", route_minimum[0] - minimum[0], spans[0]),
        ("east", maximum[0] - route_maximum[0], spans[0]),
        ("south", route_minimum[1] - minimum[1], spans[1]),
        ("north", maximum[1] - route_maximum[1], spans[1]),
    )
    progress(
        "Effective automatic geographic clearances: "
        + "; ".join(
            f"{name} {distance:.1f} m "
            + (f"({distance / span * 100:.1f}%)" if span > 0.0 else
               "(percentage undefined: zero route span)")
            for name, distance, span in sides
        )
    )


def _discovery_covers_final(
    dem: DemSource,
    bounds: tuple[GeographicBounds, ...],
    perimeter: NDArray[np.float64],
    route: ProjectedRoute,
) -> bool:
    if not dem_covers_bounds(dem, bounds):
        return False
    try:
        return bool(np.all(np.isfinite(dem.sample_projected(perimeter, route))))
    except Gpx2StlError:
        return False


def resolve_dem(
    config: Config,
    bounds: tuple[GeographicBounds, ...],
    progress: ProgressCallback = console_progress,
) -> DemSource:
    if config.topo_source != "online":
        progress(f"Checking local terrain data in {config.topo_file or config.topo_dir}")
        local = load_local_dem(config.topo_file, config.topo_dir, bounds)
        if local is not None and (
            config.topo_source == "local" or dem_covers_bounds(local, bounds)
        ):
            progress(
                f"Using {len(local.tiles)} local terrain "
                f"{'tile' if len(local.tiles) == 1 else 'tiles'}"
            )
            return local
        if (
            config.topo_source == "auto"
            and config.topo_file is None
        ):
            progress("Downloading missing Copernicus GLO-30 terrain tiles")
            cached_available = cache_copernicus_tiles(bounds, config.topo_dir)
        else:
            cached_available = False
        if cached_available:
            cached = load_local_dem(None, config.topo_dir, bounds)
            if cached is not None:
                progress(
                    f"Using {len(cached.tiles)} cached Copernicus "
                    f"{'tile' if len(cached.tiles) == 1 else 'tiles'}"
                )
                return cached
        if config.topo_source == "local":
            raise Gpx2StlError(
                f"No local GeoTIFF intersects the requested footprint in '{config.topo_dir}'."
            )

    dem_type = choose_dem_type(bounds, config.dem_type)
    progress(f"Requesting {dem_type} terrain from OpenTopography")
    return OpenTopographyClient(config.api_key or "").fetch(bounds, dem_type)


def convert(
    config: Config, progress: ProgressCallback = console_progress
) -> None:
    boundary = config.resolved_route_boundary_percent
    auto = boundary == "auto"
    numeric_boundary = 0.0 if boundary == "auto" else boundary
    if auto and not config.topo:
        raise Gpx2StlError(
            "Automatic route boundaries require topography; select a numeric "
            "--route-boundary-percent when using --no-topo."
        )
    progress(f"Reading activity paths from {config.gpx_file}")
    paths = read_activity(config.gpx_file)
    point_count = sum(len(path.latitude) for path in paths)
    progress(
        f"Loaded {len(paths)} path{'s' if len(paths) != 1 else ''} "
        f"with {point_count:,} points"
    )
    if not config.topo:
        progress("Interpolating internal GPX elevation gaps")
        paths = tuple(interpolate_elevations(path) for path in paths)
    progress("Projecting geographic coordinates into a local metric system")
    route = project_paths(paths)
    discovery = None
    if auto:
        progress("Discovering terrain-aware automatic geographic boundaries")
        discovery = discover_auto_boundary(
            route,
            config.auto_boundary_max_distance_km,
            lambda bounds: resolve_dem(config, bounds, progress),
            progress,
        )
        progress(
            f"Detected {discovery.region_count} mountain "
            f"{'region' if discovery.region_count == 1 else 'regions'}; "
            f"search distance {discovery.search_distance_m / 1000:.2f} km "
            f"(configured cap {config.auto_boundary_max_distance_km:g} km)"
        )
    custom_base: CustomBase | None = None
    if config.base_stl is not None:
        progress(f"Loading and validating custom base STL from {config.base_stl}")
        custom_base = prepare_custom_base(
            config.base_stl,
            config.text_boundary_percent if config.text is not None else 0.0,
            route,
            config.route_width,
            numeric_boundary,
            projected_envelope=discovery.envelope if discovery is not None else None,
        )
        footprint = custom_base.transform.footprint
        transform = custom_base.transform
        width = custom_base.mesh.extents[0]
        height = custom_base.mesh.extents[1]
        progress(
            f"Using custom {width:.1f} x {height:.1f} mm base with top Z "
            f"{custom_base.top_z:.1f} mm"
        )
    else:
        terrain_size = config.terrain_size
        footprint_shape = "circle" if config.text is not None else config.shape
        minimum_footprint = config.route_width / terrain_size
        footprint = create_footprint(
            (np.vstack((np.asarray(discovery.envelope.exterior.coords), route.points))
             if discovery is not None else route.points),
            footprint_shape,
            numeric_boundary,
            minimum_footprint,
        )
        footprint = add_route_clearance(footprint, config.route_width, terrain_size)
        transform = create_model_transform(footprint, terrain_size)
    projected_perimeter: NDArray[np.float64] | None = None
    if discovery is not None:
        _validate_auto_fit(
            discovery.envelope, route, footprint, transform, custom_base,
            config.route_width,
        )
        projected_perimeter = (
            _generated_perimeter(footprint) if custom_base is None else
            transform.to_projected(sample_exterior(custom_base.terrain_polygon))
        )
        _report_auto_clearances(projected_perimeter, route, progress)
    if custom_base is not None:
        progress(
            f"Fitted route at {transform.scale:.8f} mm per source meter inside "
            "the inset custom top"
        )
    elif config.text is None:
        progress(
            f"Created {config.shape} footprint: {footprint.diameter / 1000:.2f} km "
            f"source span -> {config.max_size:.1f} mm model"
        )
    else:
        progress(
            f"Created {config.shape} frame with {terrain_size:.1f} mm circular "
            f"terrain inset for {footprint.diameter / 1000:.2f} km source span"
        )

    dem = None
    if config.topo:
        if projected_perimeter is not None:
            bounds = request_projected_bounds(projected_perimeter, route)
        elif custom_base is None:
            bounds = request_bounds(footprint, route)
        else:
            model_perimeter = sample_exterior(custom_base.terrain_polygon)
            projected_perimeter = transform.to_projected(model_perimeter)
            bounds = request_projected_bounds(projected_perimeter, route)
        progress(
            "Terrain bounds: "
            + "; ".join(
                f"{item.south:.5f},{item.west:.5f} to "
                f"{item.north:.5f},{item.east:.5f}"
                for item in bounds
            )
        )
        if discovery is not None:
            assert projected_perimeter is not None
            if _discovery_covers_final(discovery.dem, bounds, projected_perimeter, route):
                dem = discovery.dem
                progress("Reusing discovery DEM for the final printable footprint")
            else:
                dem = resolve_dem(config, bounds, progress)
            dem = _FiniteDemSource(dem.tiles, True, dem.description)
            dem.sample_projected(projected_perimeter, route)
        else:
            dem = resolve_dem(config, bounds, progress)
    else:
        if custom_base is None:
            progress("Topography disabled; generating a flat base")
        else:
            progress("Topography disabled; using the flat custom top")
    progress("Generating watertight base, topography, and route meshes")
    geometry = build_geometry(
        route,
        footprint,
        transform,
        config,
        dem,
        custom_base,
    )
    progress(
        f"Generated base mesh ({len(geometry.base.vertices):,} vertices, "
        f"{len(geometry.base.faces):,} faces)"
    )
    if geometry.topography is not None:
        progress(
            "Generated topography mesh "
            f"({len(geometry.topography.vertices):,} vertices, "
            f"{len(geometry.topography.faces):,} faces)"
        )
    progress(
        f"Generated route mesh ({len(geometry.route.vertices):,} vertices, "
        f"{len(geometry.route.faces):,} faces)"
    )
    if geometry.text is not None:
        progress(
            f"Generated text mesh ({len(geometry.text.vertices):,} vertices, "
            f"{len(geometry.text.faces):,} faces)"
        )
    export_geometry(geometry, config, progress)
    progress(f"Finished writing {config.output}")
