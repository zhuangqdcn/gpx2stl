from __future__ import annotations

import math

import numpy as np
import shapely
from numpy.typing import NDArray
from shapely.geometry import LineString, Point, Polygon

from gpx2stl.activity import read_activity
from gpx2stl.auto_boundary import ValleyCriteria, discover_auto_boundary
from gpx2stl.custom_base import CustomBase, prepare_custom_base, sample_exterior
from gpx2stl.city import CityData, load_city_data
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
from gpx2stl.directions import DIRECTIONS, route_projections
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
from gpx2stl.models import (
    Config,
    DirectionalRouteBoundary,
    Footprint,
    ModelTransform,
    ProjectedRoute,
)
from gpx2stl.progress import ProgressCallback, console_progress
from gpx2stl.road_match import match_route_to_roads


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


def _projected_footprint(footprint: Footprint) -> Polygon:
    if footprint.shape == "circle":
        return Point(footprint.center).buffer(footprint.radius, quad_segs=64)
    return Polygon(footprint_vertices(footprint))


def _route_fits_footprint(
    route: ProjectedRoute,
    footprint: Footprint,
    transform: ModelTransform,
    route_width: float,
    custom_base: CustomBase | None = None,
) -> bool:
    if custom_base is not None:
        model_route = shapely.union_all(
            [LineString(transform.to_model(path)) for path in route.paths]
        )
        return bool(
            custom_base.terrain_polygon.buffer(
                -route_width / 2.0, quad_segs=32, join_style="round"
            ).covers(model_route)
        )
    clearance = route_width / (2.0 * transform.scale)
    printable = _projected_footprint(footprint).buffer(
        -clearance, quad_segs=32, join_style="round"
    )
    return all(printable.covers(LineString(path)) for path in route.paths)


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
    names = {
        "N": "north", "NE": "northeast", "E": "east", "SE": "southeast",
        "S": "south", "SW": "southwest", "W": "west", "NW": "northwest",
    }
    clearances: list[str] = []
    route_maxima, spans = route_projections(route.points)
    for index, direction in enumerate(DIRECTIONS):
        vector = np.asarray(direction.vector)
        span = float(spans[index])
        distance = float(np.max(perimeter @ vector) - route_maxima[index])
        percentage = (
            f"({distance / span * 100:.1f}%)"
            if span > 0
            else "(percentage undefined: zero route span)"
        )
        clearances.append(
            f"{direction.name} ({names[direction.name]}) {distance:.1f} m {percentage}"
        )
    progress(
        "Effective automatic geographic clearances: "
        + "; ".join(clearances)
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
    if config.mode == "city" and not config.topo:
        raise Gpx2StlError("City mode requires topography.")
    if config.nozzle_diameter is not None:
        if config.mode != "city":
            raise Gpx2StlError("Nozzle compensation requires city mode.")
        if (
            isinstance(config.nozzle_diameter, bool)
            or not isinstance(config.nozzle_diameter, (int, float))
            or not math.isfinite(config.nozzle_diameter)
            or config.nozzle_diameter <= 0
        ):
            raise Gpx2StlError(
                "Nozzle diameter must be a finite number greater than zero."
            )
    if boundary == "search":
        if not config.topo:
            raise Gpx2StlError(
                "Terrain-aware route boundary search requires topography; select a "
                "numeric --route-boundary-percent when using --no-topo."
            )
        search = True
        numeric_boundary: float | DirectionalRouteBoundary = 0.0
    else:
        search = False
        numeric_boundary = boundary
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
    if search:
        progress("Discovering terrain-aware automatic geographic boundaries")
        discovery = discover_auto_boundary(
            route,
            config.auto_boundary_max_distance_km,
            lambda bounds: resolve_dem(config, bounds, progress),
            progress,
            valley_criteria=ValleyCriteria(
                max_relief_m=config.auto_valley_max_relief_m,
                max_slope_percent=config.auto_valley_max_slope_percent,
                max_height_m=config.auto_valley_max_height_m,
                max_height_percent=config.auto_valley_max_height_percent,
            ),
        )
        fallback_count = sum(not direction.resolved for direction in discovery.directions)
        if fallback_count == 8:
            detection_summary = "No complete mountain boundary detected; using fallback in all 8 directions"
        elif fallback_count:
            detection_summary = (
                f"Detected {discovery.region_count} mountain "
                f"{'region' if discovery.region_count == 1 else 'regions'} "
                f"with partial boundaries; using fallback in {fallback_count} of 8 directions"
            )
        else:
            detection_summary = (
                f"Detected {discovery.region_count} mountain "
                f"{'region' if discovery.region_count == 1 else 'regions'}"
            )
        progress(
            f"{detection_summary}; "
            f"search distance {discovery.search_distance_m / 1000:.2f} km "
            f"(configured cap {config.auto_boundary_max_distance_km:g} km)"
        )
    custom_base: CustomBase | None = None
    if config.base_stl is not None:
        if isinstance(numeric_boundary, tuple):
            raise Gpx2StlError(
                "Directional route boundary percentages are not supported with "
                "a custom STL base; use one symmetric percentage."
            )
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
    city_data = CityData((), ())
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
        if config.mode == "city":
            city_clip = (
                shapely.transform(
                    custom_base.terrain_polygon, transform.to_projected
                )
                if custom_base is not None
                else _projected_footprint(footprint)
            )
            progress("Loading cached OpenStreetMap buildings, roads, and water")
            city_data = load_city_data(
                bounds,
                config.city_dir,
                route,
                city_clip,
                config.building_default_height,
                progress=progress,
            )
            progress(
                f"Loaded {len(city_data.buildings):,} buildings and "
                f"{len(city_data.roads):,} roads, including "
                f"{len(city_data.bridges):,} bridge structures, plus "
                f"{len(city_data.water):,} water bodies"
            )
            matched_route = match_route_to_roads(
                route, city_data.roads, config.road_snap_distance
            )
            if _route_fits_footprint(
                matched_route,
                footprint,
                transform,
                config.route_width,
                custom_base,
            ):
                changed_points = sum(
                    not np.array_equal(before, after)
                    for before, after in zip(
                        route.paths, matched_route.paths, strict=True
                    )
                )
                route = matched_route
                progress(
                    f"Road-matched {changed_points} of {len(route.paths)} route "
                    f"{'path' if len(route.paths) == 1 else 'paths'} within "
                    f"{config.road_snap_distance:g} m"
                )
            else:
                progress(
                    "Road matching would exceed the printable route clearance; "
                    "using the original activity geometry"
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
    if config.mode == "city":
        if config.nozzle_diameter is not None:
            source_offset_m = (
                config.nozzle_diameter / 2.0 / transform.scale
            )
            progress(
                f"Applying {config.nozzle_diameter:g} mm nozzle compensation "
                "to buildings and bridge decks "
                f"({source_offset_m:g} source meters outward per side)"
            )
        geometry = build_geometry(
            route,
            footprint,
            transform,
            config,
            dem,
            custom_base,
            buildings=city_data.buildings + city_data.bridges,
            water=city_data.water,
        )
    else:
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
    buildings_mesh = getattr(geometry, "buildings", None)
    if buildings_mesh is not None:
        progress(
            f"Generated buildings mesh ({len(buildings_mesh.vertices):,} "
            f"vertices, {len(buildings_mesh.faces):,} faces)"
        )
    water_mesh = getattr(geometry, "water", None)
    if water_mesh is not None:
        progress(
            f"Generated water mesh ({len(water_mesh.vertices):,} "
            f"vertices, {len(water_mesh.faces):,} faces)"
        )
    export_geometry(geometry, config, progress)
    progress(f"Finished writing {config.output}")
