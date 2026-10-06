from __future__ import annotations

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
)
from gpx2stl.gpx import interpolate_elevations, project_paths, read_gpx
from gpx2stl.mesh import build_geometry
from gpx2stl.models import Config
from gpx2stl.progress import ProgressCallback, console_progress


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
    progress(f"Reading GPX paths from {config.gpx_file}")
    paths = read_gpx(config.gpx_file)
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
    custom_base: CustomBase | None = None
    if config.base_stl is not None:
        progress(f"Loading and validating custom base STL from {config.base_stl}")
        custom_base = prepare_custom_base(
            config.base_stl,
            config.boundary_percent,
            route,
            config.route_width,
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
        terrain_size = (
            config.max_size * config.inner_size_percent / 100.0
            if config.text is not None
            else config.max_size
        )
        footprint_shape = "circle" if config.text is not None else config.shape
        minimum_footprint = config.route_width / terrain_size
        footprint = create_footprint(
            route.points,
            footprint_shape,
            config.boundary_percent,
            minimum_footprint,
        )
        footprint = add_route_clearance(footprint, config.route_width, terrain_size)
        transform = create_model_transform(footprint, terrain_size)
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
        if custom_base is None:
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
