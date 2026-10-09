# gpx2stl

Convert GPX tracks/routes and Garmin FIT activities into printable terrain models:

- **3MF by default:** separate named objects and materials for Bambu Studio. Filament 1 is the GPX route, filament 2 is topography, filament 3 is optional text, and filament 4 is the base.
- **STL on request:** one watertight mesh containing the base, route, and all enabled features.
- **Flexible topography:** use local GeoTIFF files first or download SRTMGL1/COP30 data from OpenTopography.
- **Printable city mode:** add cached OpenStreetMap buildings, bridges, water bodies, conservative road matching, and flush route/water inlays to the terrain.
- **File or folder input:** convert one `.gpx`/`.fit` file or every supported activity file directly in a folder; files remain separate models.
- **Square, circular, or hexagonal base:** automatically sized around every path in each input activity.
- **Custom STL base:** preserve an existing model and use its highest flat top as the exact terrain shape.
- **Styled perimeter text:** choose font family, size, weight, style, and alignment, then print it raised or as a flush 3MF inlay.

[中文说明](#中文说明)

## Requirements

- Python 3.11 or newer
- An [OpenTopography API key](https://portal.opentopography.org/myopentopo) only when online terrain is selected

## Installation

With `uv`:

```bash
uv sync
source .venv/bin/activate
```

Or with `pip`:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

## API key

Copy `.env.example` to `.env` in the repository/current project root and replace the placeholder:

```dotenv
OPENTOPOGRAPHY_API_KEY=your-key
```

The command searches for `.env` from the current directory upward. An existing process environment variable takes precedence over `.env`, and `--api-key` takes precedence over both. `.env` is ignored by Git. An API key is not needed when local terrain completely covers the model.

## Settings file

At startup, the command searches from the input file’s directory (or the input directory itself) upward for `.gpx2stl.settings.json`. If the input path must come from settings, discovery instead starts at the current directory. Values in that file become defaults; explicit command-line arguments override them. Use `--settings PATH` to select any settings file explicitly and disable automatic discovery. Relative paths are resolved from the directory containing the selected settings file.

Copy `settings.example.json` beside your activity files as `.gpx2stl.settings.json` and edit the values you want:

```bash
cp settings.example.json /path/to/activities/.gpx2stl.settings.json
```

JSON keys use the Python/long-option names with underscores, such as `mode`, `route_width`, `route_depth`, `road_snap_distance`, `route_boundary_percent`, `text_mode`, `topo_source`, `topo_dir`, `city_dir`, `building_default_height`, and `building_height_scale`. The positional `.gpx`/`.fit` file-or-directory input can also be defaulted with the backward-compatible `gpx_file` key. Use `use_3mf` for the `--3mf` / `--no-3mf` setting. Relative `font_file`, terrain, and city-cache paths are resolved from the settings file. Unknown keys, invalid JSON, or incorrect value types produce an explicit error. `.gpx2stl.settings.json` is ignored by Git; `settings.example.json` is tracked as a complete template.

Directional route padding uses a JSON array in north, east, south, west order, for example `"route_boundary_percent": [10, 15, 10, 15]`. A scalar number retains symmetric padding, and `"auto"` retains DEM-based mountain discovery.

`--boundary-percent` / `boundary_percent` and `--inner-size-percent` / `inner_size_percent` are retired and rejected with migration guidance. For generated bases, rename the old boundary setting to `route_boundary_percent` and replace the old inner-size setting with `text_boundary_percent = (100 - inner_size_percent) / 2`. Thus an inner size of 70 becomes a text boundary of 15. For custom STL bases with text, the old boundary value controlled the text inset: move it to `text_boundary_percent` and use `route_boundary_percent: 0` to preserve the previous maximum route fit. Without text, custom terrain now uses the full top; text boundary is ignored.

For terrain and city-cache paths shared between Windows and Linux/WSL, settings support `topo_file_windows`, `topo_file_linux`, `topo_dir_windows`, `topo_dir_linux`, `city_dir_windows`, and `city_dir_linux`. A non-null key matching the current OS overrides the corresponding generic `topo_file`, `topo_dir`, or `city_dir`. A null or absent OS-specific key falls back to the generic value. Paths for the inactive OS are not interpreted or resolved.

```json
{
  "topo_file": null,
  "topo_file_windows": null,
  "topo_file_linux": null,
  "topo_dir": "asset",
  "topo_dir_windows": "E:\\terrain\\glo30",
  "topo_dir_linux": "/mnt/e/terrain/glo30",
  "city_dir": "asset/city",
  "city_dir_windows": "E:\\gpx-cache\\city",
  "city_dir_linux": "/mnt/e/gpx-cache/city"
}
```

The complete path precedence is an explicit CLI path, then the matching non-null OS-specific setting, then the generic setting, then the built-in default (`asset` for terrain and `asset/city` for city data). `topo_file` still takes precedence over `topo_dir` after platform selection. Relative selected paths are resolved from the settings-file directory.

```bash
# Use a settings file with any name or location
python -m gpx2stl route.gpx --settings ./profiles/large-model.json
```

## Local terrain assets

Place local `.tif` or `.tiff` elevation files under `./asset`. The directory is scanned recursively and is ignored by Git:

```bash
mkdir -p asset
```

Local files may use any valid georeferenced CRS. The complete padded printable footprint must be covered by the selected file or tiles; automatic boundaries also need coverage of their larger discovery windows. If local tiles intersect the requested area but leave a gap, conversion fails rather than silently mixing local and online elevations.

Key-free Copernicus tiles can be downloaded from:

- [GLO-30 Public tile list](https://copernicus-dem-30m.s3.amazonaws.com/tileList.txt) and [bucket documentation](https://copernicus-dem-30m.s3.amazonaws.com/readme.html)
- [GLO-90 tile list](https://copernicus-dem-90m.s3.amazonaws.com/tileList.txt)

Download every 1° tile touched by the padded footprint, not only the raw GPX centerline. Follow the [Copernicus DEM license](https://registry.opendata.aws/copernicus-dem/) and attribution requirements.

## Usage

```bash
python -m gpx2stl route.gpx
```

This writes `route.3mf` beside `route.gpx`. A directory input converts each `.gpx` or `.fit` file directly in that directory into a separate model; files are never joined:

```bash
python -m gpx2stl ./routes

# Write the separate models to an existing directory
python -m gpx2stl ./routes --output ./models
```

Garmin FIT files use the same command:

```bash
python -m gpx2stl activity.fit
```

For FIT input, GPS `record` messages become route points, Garmin semicircle coordinates are converted to degrees, and enhanced altitude is used when available. A missing-GPS record or timer-stop event separates paths instead of connecting positions across a pause or data gap.

Directory scanning is non-recursive and includes `.gpx` and `.fit` case-insensitively. By default, each output is written beside its source activity with the same stem and the selected `.3mf` or `.stl` extension. A `.gpx` and `.fit` with the same stem would target the same output and are rejected explicitly. When the input is a directory, `--output` must name an existing output directory.

Examples:

The command prints timestamped progress messages while it parses the GPX, chooses or downloads terrain, creates meshes, exports the selected format, and validates the output.

```bash
# Circular 3MF with separate route, topography, and base objects
python -m gpx2stl route.gpx --shape circle --max-size 180

# Flat-top hexagonal model
python -m gpx2stl route.gpx --shape hex

# City model with buildings, bridge decks, a flush route inlay, and road matching within 5 m
python -m gpx2stl route.gpx --mode city

# City model with 10% north/south and 20% east/west route padding
python -m gpx2stl route.gpx --mode city --route-boundary-percent 10,20,10,20

# Disable road matching while retaining buildings and the embedded route
python -m gpx2stl route.gpx --mode city --road-snap-distance 0

# Estimate all route-touched mountain extents, searching up to 30 km per side
python -m gpx2stl route.gpx --route-boundary-percent auto --auto-boundary-max-distance-km 30

# Four-object 3MF: base, circular topography, raised text, and route
python -m gpx2stl route.gpx --shape hex --text "MOUNT RAINIER"

# Add 3 mm to the default eight-space bottom seam
python -m gpx2stl route.gpx --text "  MOUNT RAINIER  " --text-end-gap 3

# Use a custom font for Chinese or another script
python -m gpx2stl route.gpx --text "路线" --font-file ./fonts/NotoSansCJK-Regular.ttc

# Use an installed bold italic font at a fixed glyph height, aligned from the seam
python -m gpx2stl route.gpx --text "RIDGELINE" --font-family Arial --font-weight bold --font-style italic --font-size 5 --text-align left

# Create a separate blue text inlay, flush with the 3MF base surface and 0.8 mm deep
python -m gpx2stl route.gpx --text "RIDGELINE" --text-mode embedded --text-depth 0.8

# Preserve an existing STL base and use its flat top
python -m gpx2stl route.gpx --base-stl ./base.stl --text "TRAIL" --text-boundary-percent 15 --route-boundary-percent 10

# Force one local GeoTIFF (no API key or network)
python -m gpx2stl route.gpx --topo-source local --topo-file ./terrain.tif

# Recursively use tiles under ./asset
python -m gpx2stl route.gpx --topo-source local

# Force OpenTopography instead of local data
python -m gpx2stl route.gpx --topo-source online --api-key YOUR_KEY

# Single-mesh STL with terrain
python -m gpx2stl route.gpx --no-3mf -o route.stl

# No network/topography; numeric padding also overrides an auto settings value
python -m gpx2stl route.gpx --no-topo --route-boundary-percent 10 --no-3mf

# Override automatic DEM selection
python -m gpx2stl route.gpx --dem-type COP30 --force
```

### Options

| Option | Default | Description |
|---|---:|---|
| `input_path` | required | Input `.gpx`/`.fit` file or directory. A directory converts each directly contained supported file independently and non-recursively. |
| `--settings` | discovered | Explicit settings file path. Overrides `.gpx2stl.settings.json` discovery. |
| `-o`, `--output` | input stem | Output file path for a file input, or an existing output directory for a directory input. |
| `--mode` | `topo` | `topo` preserves the terrain-route model; `city` adds OSM buildings, road matching, and a flush route inlay. City mode requires topo and 3MF. |
| `--route-width` | city: `0.5` mm; otherwise: `1` mm | Printed route ribbon width. |
| `--route-height` | city: `1.5` mm; otherwise: `2` mm | Route height above terrain in topo mode. City routes remain flush and use `--route-depth`. |
| `--route-depth` | city: `1.5` mm; otherwise: `0.6` mm | Flush route inlay/cavity depth in city mode; must be smaller than the base height. |
| `--road-snap-distance` | `5` m | Maximum source-meter distance for conservative OSM road matching in city mode. Set to `0` to preserve the original activity geometry. |
| `--topo`, `--no-topo` | topo | Enable or disable terrain. |
| `--route-boundary-percent` | city generated base: `10,10,10,10`; city custom base: `10`; topo: `auto`; no topo: `10` | One finite nonnegative symmetric percentage, four comma-separated `N,E,S,W` percentages, or `auto` for DEM-based mountain discovery. Settings use a four-number JSON array. Explicit `auto` requires topo; directional values require a generated base. |
| `--auto-boundary-max-distance-km` | `10` km | Finite positive maximum discovery distance beyond each side of the route bounding box in automatic mode. |
| `--auto-valley-max-relief-m` | `1000` m | Maximum smoothed elevation variation across the approximately 900 m valley neighborhood. Increase to admit uneven valley floors. |
| `--auto-valley-max-slope-percent` | `100` % | Maximum smoothed valley-floor slope as a percentage grade (`2` means 2%, not 200%). |
| `--auto-valley-max-height-m` | `1000` m | Maximum height above the search window's 10th-percentile elevation floor; also limited by the relief percentage below. |
| `--auto-valley-max-height-percent` | `100` % | Maximum height above that floor as a percentage of observed window relief, from 0 to 100. |
| `--shape` | `square` | `square`, `circle`, or flat-top `hex`. Squares remain north-up. |
| `--text` | none | Text placed as one compact run along the flat frame, with its reserved seam centered at the bottom. |
| `--text-height` | `1` mm | Raised text thickness above the frame. |
| `--text-margin` | automatic | Minimum clearance in millimeters between glyph outlines and both boundaries of the flat text frame. |
| `--text-end-gap` | `0` mm | Extra bottom seam gap added to the default eight font spaces and any leading/trailing spaces in `--text`. |
| `--text-align` | `center` | Align the compact text run `left`, `center`, or `right` within the usable perimeter measured from the bottom seam. |
| `--text-mode` | `raised` | Use `raised` text or an `embedded` flush inlay. Embedded text requires 3MF output. |
| `--text-depth` | `0.6` mm | Depth of the cavity and flush text inlay in embedded mode. |
| `--text-boundary-percent` | `7` | Per-side text-band inset as a percentage of outer width (custom bases: smaller top dimension); from 0 inclusive to 50 exclusive. Ignored without text. |
| `--font-family` | `DejaVu Sans` | Installed font family used for text. |
| `--font-file` | none | Exact custom `.ttf`, `.otf`, or `.ttc` face for scripts not covered by an installed font; cannot be combined with family/weight/style options. |
| `--font-size` | automatic | Requested glyph height in millimeters; omitted text is automatically fitted. |
| `--font-weight` | `normal` | Installed-family variant: `normal` or `bold`. |
| `--font-style` | `normal` | Installed-family variant: `normal` or `italic`. |
| `--base-stl` | none | Existing millimeter-scale, Z-up STL whose highest flat top becomes the custom terrain shape. |
| `--3mf`, `--no-3mf` | 3MF | Select multi-material 3MF or single-mesh STL. |
| `--max-size` | `200` mm | Maximum final X/Y dimension. |
| `--terrain-height` | automatic | Terrain relief in mm. By default, elevation uses the same real-world-to-model ratio as `--max-size`; an explicit value normalizes relief to that height. |
| `--base-height` | `2` mm | Solid base thickness. |
| `--topo-source` | `auto` | `auto` uses complete local coverage first, `online` uses OpenTopography, and `local` disables network fallback. |
| `--topo-file` | none | One local GeoTIFF in any valid CRS; takes precedence over `--topo-dir`. |
| `--topo-dir` | `./asset` | Directory recursively scanned for `.tif` and `.tiff` tiles. |
| `--city-dir` | `./asset/city` | Persistent cache for fixed-grid Overpass JSON tiles. Existing nonempty tiles are reused indefinitely; delete them explicitly to refresh OSM data. |
| `--building-default-height` | `10` m | Building height when OSM has neither a valid `height` nor `building:levels` value. |
| `--building-height-scale` | `5` | Multiplier applied after building and bridge height is converted with the model's horizontal map scale. Use `1` for true scale. |
| `--water-depth` | `0.4` mm | Depth of the flush water cavity/inlay in city mode; must be smaller than the base or custom-base thickness. |
| `--dem-type` | automatic | OpenTopography DEM identifier override. |
| `--api-key` | environment | OpenTopography API key override. |
| `--force` | off | Replace an existing output file. |

## Geometry and scaling

- Geographic coordinates are projected into a route-centered local metric projection, then uniformly scaled so the padded footprint fits `--max-size`.
- The route width is included as extra footprint clearance, including when boundary padding is zero.
- By default, terrain elevations use the same scale as X/Y: the horizontal ratio derived from `--max-size` is applied to the DEM elevation range. For example, a horizontal scale of 1:20,000 also makes 1,000 m of elevation equal 50 mm.
- Setting `--terrain-height` explicitly overrides true-scale relief and normalizes the DEM minimum-to-maximum range to that many millimeters.
- Without topo, GPX or FIT altitude controls the route top at physical 1:20,000 vertical scale: 1,000 m becomes 50 mm. Internal missing elevations are interpolated; missing endpoint/all elevations are errors.
- In city mode, route points are matched only to connected OSM road geometry within `--road-snap-distance`; implausible, disconnected, or out-of-range spans retain their GPX geometry. If matching would leave the printable footprint, the original route is used. Buildings that overlap an unmatched route remain complete and hide that route section.
- City buildings use OSM `height`, then `building:levels × 3 m`, then `--building-default-height`. Bridge-tagged ways become printable decks using OSM width, lane-derived width, or a road/rail fallback. Heights use the horizontal model scale and `--building-height-scale`; roofs and bridge decks are flat. The default `5×` vertical multiplier keeps short structures visible on city-scale models; use `1` for true scale. The route is a separate 3MF object filling a matching terrain/base cavity with its top flush to the terrain.
- City water includes OSM lakes, ponds, reservoirs, basins, riverbanks, width-tagged or inferred rivers/streams/canals, and the sea-facing side of directed coastlines. It is clipped to the printable terrain and exported as a separate flush `Water` inlay; `--water-depth` controls its cavity depth. The route takes material priority where it crosses water.
- In numeric boundary mode, square output is the smallest north-up square around the route before padding. Circle output uses the true minimum enclosing circle. Hex output uses the minimum translated flat-top regular hexagon. Automatic mode fits the eight-direction geographic selection polygon together with the route.
- Four-value padding expands the route bounding box independently: N/S percentages use its north-south span, while E/W percentages use its east-west span. The selected square, circle, or hex is then fitted around the padded rectangle. City mode defaults to `10,10,10,10`; a degenerate axis receives no percentage padding on that axis, but printable route-width clearance still applies.
- When `--text` is present, `--max-size` controls the outer square, circle, or hex frame. The centered terrain circle has diameter `max_size × (1 - 2 × text_boundary_percent / 100)`; the rest of the frame stays flat at the base-top height. The default 7% text boundary retains an 86% terrain diameter. Hex frames need more than approximately 6.7% to leave a text band at their narrower sides; glyphs and margins may require more.
- A numeric `--route-boundary-percent` pads the minimum route footprint before fitting it into the terrain: square side length is multiplied by `1 + 2 × route_boundary_percent / 100`, while circle/hex radius is multiplied by `1 + route_boundary_percent / 100`. Route-width clearance is added separately. Changing route boundary does not resize the text band. Without text, text boundary is ignored and the full generated footprint is available.
- In 3MF output, the generated prism or supplied custom STL remains a separate `Base` object. Enabled relief is a separately watertight `Topography` object with a small intentional overlap into the base for reliable slicing. Disabling topo omits that object.
- Text defaults to DejaVu Sans and is placed as a compact tangent run on the perimeter. `--text-align` positions that run in the usable perimeter after the bottom seam; it does not stretch inter-character spacing. Text is automatically fitted unless `--font-size` requests a glyph height. Select an installed family and variant with `--font-family`, `--font-weight`, and `--font-style`, or use `--font-file` to select one exact face. The bottom seam reserves eight font spaces by default; `--text-end-gap` adds an absolute gap and quoted leading/trailing spaces add font-relative gap. Missing fonts, variants, or glyphs are reported explicitly.
- Raised text overlaps the base slightly for reliable slicing and is unioned into STL output. In 3MF-only embedded mode, a cavity is cut into the base and the separate text-material object fills it to a flush top surface; `--text-depth` controls the inlay depth.
- In STL output, raised text is unioned with the frame, route, and terrain into one watertight mesh.
- Routes crossing the ±180° antimeridian are supported by split DEM requests.

### Automatic mountain boundaries

`--route-boundary-percent auto` (JSON: `"route_boundary_percent": "auto"`) uses DEM summit/valley segmentation to estimate the mountain terrain touched by the entire route, including routes crossing multiple peaks. It analyzes progressively larger nested search windows independently in eight directions: N, NE, E, SE, S, SW, W, and NW. Reliable detected bounds require an interior valley boundary and stable directional terrain geometry between windows. Candidates can become unresolved again if later evidence changes. This is a terrain-based estimate, not a guarantee of the exact boundary of a named mountain.

Automatic mode keeps each reliable detected directional constraint, including full analysis-cell extents and a safety margin, even when its padding exceeds 100%. After normal bounded discovery, directions still unresolved at the search cap use 100% of the route's projected span in that direction. If no usable detection exists (no valley markers, flat or ambiguous terrain, or no route-touched mountain regions), all eight directions use fallback with explicit warnings; this is not reported as complete mountain discovery.

Directions use unit normals in the route's local metric projection (X east, Y north). For each normal `u`, fallback is:

```text
route_max(u) = max(route_points dot u)
route_span(u) = max(route_points dot u) - min(route_points dot u)
point dot u <= route_max(u) + route_span(u)
```

NE uses `u = (1, 1) / sqrt(2)` and its own projected span, not the sum of separate north/east paddings. W uses `u = (-1, 0)`, so its fallback pads west by the route's east-west span. A zero directional span uses the largest nonzero span among all eight route projections, with a warning identifying the substituted percentage basis. If all eight spans are zero and fallback is needed, the input is invalid and produces an actionable error.

The intersection of the eight detected/fallback half-planes forms a convex geographic selection polygon with up to eight sides. Constraints can be redundant: it need not have exactly eight edges or be a regular octagon. An unresolved diagonal's fallback can clip terrain detected in an adjacent resolved direction. The fallback limit takes priority; the adjacent detected constraint stays configured, but its actual polygon extent may be smaller. Warnings explicitly state that complete mountain coverage is not guaranteed.

The selection polygon recenters the geographic fit and shrinks the route as needed while preserving the selected square/circle/hex shape, physical model size, and unchanged independent text band. Custom STL bases retain their original shape and dimensions too. At fixed model size, a larger geographic extent also reduces default true-scale relief. The 100% fallback is a geographic-selection limit, not a hard cap on the printed footprint: shape fitting and route-width clearance can include additional terrain. Asymmetric geographic clearance does not make the printed base irregular.

After each search window, logs report the search distance and all eight directional statuses: valley boundary found and stable, candidate awaiting stability, or unresolved with a reason. Final results distinguish complete detection, partial fallback, and all-direction fallback, and list each direction as valley boundary found or fallback, with its padding distance and percentage basis plus the fallback reason where applicable. Separate final printable-footprint clearances report actual projected padding in all eight directions; these may exceed 100% and are not the requested selection limits. Percentages for zero route spans are explicitly undefined rather than misleading.

The outward search cap defaults to 10 km beyond each side of the route bounding box; change it with `--auto-boundary-max-distance-km` or JSON `auto_boundary_max_distance_km`. Larger windows can increase processing time, memory, DEM downloads, and API costs. Additional DEM coverage may be fetched under the existing `--topo-source` policy; `local` never downloads, and an explicit `--topo-file` must cover the required area. The final printable footprint can need coverage beyond the discovery area.

Missing/nonfinite DEM data, insufficient useful resolution, resource limits, invalid inputs, and unexpected failures remain explicit errors, not fallback conditions. The directional fallback does not relax DEM coverage requirements. Use better local DEM data or adjust the search cap where appropriate; explicitly choosing numeric padding such as `--route-boundary-percent 10` remains available.

Detection uses an anchored 90 m analysis grid, at most 1,000,000 cells, and requires contributing DEM pixels no larger than 270 m. It needs at least 60 m of observed relief and usable summit/valley evidence. The very permissive default valley criteria allow up to 1000 m of smoothed elevation variation across an approximately 900 m neighborhood, up to 100% grade (45°), and a height above the window's 10th-percentile elevation floor of at most `min(1000 m, 100% of observed relief)`. These defaults can classify mountain slopes as valleys or erase foreground summits from segmentation, leaving no usable mountain region and leading to fallback rather than successful detection. Evidence and independent directional stability requirements, fallback rules, and explicit error behavior remain unchanged.

All four valley thresholds are configurable in the CLI and JSON settings (replace hyphens with underscores for JSON keys). Values must be finite nonnegative numbers; the height percentage cannot exceed 100. Increasing values loosens conditions; lowering values tightens them. All conditions must pass together: increasing only slope will not help if neighborhood variation or height above the floor still fails. The effective height limit is always the **smaller** of the meter limit and the relief-percentage limit. Controls apply only to automatic boundary mode; numeric padding is unchanged.

For example, to experiment with more conservative conditions than the defaults, lower all four thresholds:

```bash
python -m gpx2stl route.gpx --route-boundary-percent auto --auto-valley-max-relief-m 35 --auto-valley-max-slope-percent 3 --auto-valley-max-height-m 150 --auto-valley-max-height-percent 15
```

Equivalent JSON entries:

```json
{
  "route_boundary_percent": "auto",
  "auto_valley_max_relief_m": 35,
  "auto_valley_max_slope_percent": 3,
  "auto_valley_max_height_m": 150,
  "auto_valley_max_height_percent": 15
}
```

These are exploratory values, not a universal preset or guaranteed solution. Discovery logs print the active thresholds. Loosening them can misclassify gentle foothills as valleys, crop mountain slopes, or change the selected regions; boundaries still need independent directional stability, and unresolved directions still use the existing 100% fallback. The floor is window-relative, not a local drainage/saddle detector: threshold tuning does not guarantee recognition of every visible valley. DEM coverage/resolution and resource checks remain strict.

Omitting the route boundary selects directional `10,10,10,10` in city mode, `auto` in topo mode, and numeric `10` without topography. Explicit `auto` with `--no-topo` is an error, including when `auto` comes from settings. Override it with `--no-topo --route-boundary-percent 10`. There is no separate automatic-boundary boolean. Scalar numeric behavior and the independent text boundary are unchanged. The tracked settings template retains `"auto"` for its default topo profile; use the documented four-number JSON array for a city profile.

### Custom STL bases

`--base-stl` preserves the input mesh’s original position, orientation, and dimensions. STL files have no unit metadata, so the input is assumed to use millimeters and must already be oriented with Z up. Custom-base mode ignores `--shape`, `--base-height`, and `--max-size` for base sizing and layout.

The input must be a finite, consistently wound, watertight, positive-volume mesh with exactly one connected horizontal upward-facing region at its global maximum Z. That top region may be concave and may contain holes. Models with only curved/sloped maxima or multiple disconnected highest regions are rejected.

With text, the terrain region follows the exact top outline after an inward offset:

```text
offset distance = text-boundary-percent / 100 × min(top width, top height)
```

Without text, there is no text inset and terrain uses the full top. The GPX remains north-up. In numeric mode it is centered on the usable terrain; its maximum fitting scale reserves half the route width at the edges, then is divided by `1 + route_boundary_percent / 100` to add independent route padding. In automatic mode the entire eight-direction selection polygon and route ribbon are fitted together, recentering the geography and reducing scale as necessary. Containment is checked; incompatible concave edges or holes produce an explicit error rather than clipping the selection polygon or route. Changing route boundary leaves the terrain region and text band unchanged. The custom STL itself is never resized by `--max-size`. Final DEM bounds are derived from the terrain region.

Custom STL fitting supports city mode. OSM buildings, roads, bridges, and water are clipped to the exact inset custom top; terrain/buildings follow that surface, and the flush route and water cavities are cut into the custom base. Route and water depths must be smaller than the custom base thickness. Custom fitting supports a scalar symmetric percentage or `auto`; four directional values are rejected because the custom top has its own arbitrary outline. An omitted city boundary therefore defaults to scalar `10` for custom bases.

When `--text` is supplied, the largest fitting glyph height is selected unless `--font-size` is set. The compact run stays tangent to the continuous perimeter and follows `--text-align`. The head/tail seam remains centered at the bottom and reserves eight font spaces plus `--text-end-gap` and any quoted leading/trailing spaces. `--text-margin` reserves the requested minimum clearance in millimeters from both the outer shape boundary and the inner terrain boundary. Every glyph must fit entirely in the remaining area. Increase `--text-boundary-percent`, reduce the font size, margins, or gap, shorten the text, or choose a narrower font if the border cannot contain it.

## Topography source behavior

- `auto` (default): use an explicit `--topo-file`, otherwise use intersecting tiles under `--topo-dir`. Missing matching GLO-30 tiles are downloaded anonymously from the public Copernicus AWS bucket into `--topo-dir` and reused as a persistent cache. If GLO-30 is unavailable, OpenTopography is used when an API key is configured.
- `local`: require complete local coverage and never access the network.
- `online`: ignore local data and use OpenTopography.

The explicit file is used exclusively when `--topo-file` is present. Local directory files are metadata-filtered before elevation data is loaded, so unrelated global tiles are not loaded into memory.

### OpenTopography

The tool calls `https://portal.opentopography.org/API/globaldem` with GeoTIFF output. It automatically selects:

- `SRTMGL1` when the complete requested area is between 56°S and 60°N.
- `COP30` otherwise.

OpenTopography requires an API key and applies request/rate limits. API, authentication, no-data, and invalid raster errors stop conversion with a nonzero exit code. Review the [OpenTopography terms of use](https://opentopography.org/terms-use) and provide appropriate data attribution for derived models.

## Bambu Studio

Import the generated 3MF as one object with multiple parts if prompted. The model contains only meaningful mesh objects from this list:

1. `GPX route` / `Filament 1 - Route`
2. `Topography` / `Filament 2 - Topography` when topo is enabled
3. `Text` / `Filament 3 - Text` when `--text` is supplied
4. `Base` / `Filament 4 - Base`
5. `Buildings` / `Filament 5 - Buildings` in city mode
6. `Water` / `Filament 6 - Water` when city water geometry is present

Topo mode retains four material slots so the base consistently maps to filament 4. City mode adds the fifth building material and, when present, the sixth water material. Optional geometry is omitted when absent. Confirm or remap the parts to the desired AMS/filament slots before slicing.

## Development

```bash
uv sync --extra dev
source .venv/bin/activate
python -m pytest
```

Tests use synthetic GPX and GeoTIFF data and do not require network access or a real API key.

## License

This project is licensed under the [MIT License](LICENSE). Terrain datasets remain subject to their respective licenses and attribution requirements. City data is © [OpenStreetMap contributors](https://www.openstreetmap.org/copyright) and is available under the ODbL; exported models that use it must retain the required attribution.

---

# 中文说明

`gpx2stl` 可将 GPX 轨迹/路线和 Garmin FIT 活动转换为适合 3D 打印的地形模型：

- **默认输出 3MF：**包含可导入 Bambu Studio 的独立命名对象和材料。耗材 1 用于 GPX 路线，耗材 2 用于地形，耗材 3 用于可选文字，耗材 4 用于底座。
- **可选输出 STL：**底座、路线和所有启用的功能合并为一个水密网格。
- **灵活的真实地形：**优先使用本地 GeoTIFF，或通过 OpenTopography 下载 SRTMGL1/COP30 高程数据。
- **可打印城市模式：**在地形上加入缓存的 OpenStreetMap 建筑、桥梁、水体、保守道路匹配以及齐平路线/水体嵌件。
- **文件或目录输入：**可转换一个 `.gpx`/`.fit` 文件，或目录中直接包含的所有受支持活动文件；不同文件始终生成独立模型。
- **方形、圆形或六边形底座：**根据每个输入活动中的全部路径自动确定范围。
- **自定义 STL 底座：**保留现有模型，并将其最高的平坦顶面作为精确地形外形。
- **可设置样式的周边文字：**可选择字体族、字高、粗细、样式和对齐方式，并打印为凸起文字或齐平的 3MF 嵌件。

## 环境要求

- Python 3.11 或更高版本
- 仅在选择在线地形时需要 [OpenTopography API Key](https://portal.opentopography.org/myopentopo)

## 安装

使用 `uv`：

```bash
uv sync
source .venv/bin/activate
```

或使用 `pip`：

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

## 配置 API Key

将 `.env.example` 复制为仓库（或当前项目）根目录下的 `.env`，并替换占位值：

```dotenv
OPENTOPOGRAPHY_API_KEY=你的API密钥
```

命令会从当前目录开始向上查找 `.env`。系统环境变量会覆盖 `.env` 中的值，而 `--api-key` 的优先级最高。`.env` 已加入 Git 忽略列表。如果本地地形完整覆盖模型，则不需要 API Key。

## 设置文件

程序启动时会从输入文件所在目录（或输入目录本身）向上查找 `.gpx2stl.settings.json`。如果输入路径本身需要由设置文件提供，则改为从当前目录向上查找。文件中的值会成为默认参数，命令行中显式传入的参数优先级更高。可用 `--settings PATH` 显式选择任意设置文件并禁用自动查找。相对路径以所选设置文件所在目录为基准解析。

将完整模板复制到活动文件旁并按需修改：

```bash
cp settings.example.json /path/to/activities/.gpx2stl.settings.json
```

JSON 键使用 Python/长参数对应的下划线名称，例如 `mode`、`route_width`、`route_depth`、`road_snap_distance`、`route_boundary_percent`、`topo_source`、`topo_dir`、`city_dir`、`building_default_height` 和 `building_height_scale`。也可用向后兼容的 `gpx_file` 键设置默认 `.gpx`/`.fit` 文件或目录；`--3mf` / `--no-3mf` 对应 `use_3mf`。相对字体、地形和城市缓存路径都以设置文件所在目录为基准解析。未知键、无效 JSON 或错误的数据类型都会产生明确错误。`.gpx2stl.settings.json` 已被 Git 忽略，而完整模板 `settings.example.json` 会纳入版本控制。

四方向路线边界在 JSON 中按北、东、南、西顺序使用数组，例如 `"route_boundary_percent": [10, 15, 10, 15]`。单个数值仍表示对称边界，`"auto"` 仍表示基于 DEM 的山体搜索。

`--boundary-percent` / `boundary_percent` 和 `--inner-size-percent` / `inner_size_percent` 已停用，使用时会报错并提示迁移方法。生成底座时，将旧边界键改为 `route_boundary_percent`，并按 `text_boundary_percent = (100 - inner_size_percent) / 2` 换算旧内圈尺寸；例如 70 对应文字边界 15。带文字的自定义 STL 底座中，旧边界值控制文字内缩，应移至 `text_boundary_percent`，并设置 `route_boundary_percent: 0` 以保留原来的最大路线缩放。无文字时，自定义地形现在使用完整顶面，文字边界被忽略。

为了在 Windows 与 Linux/WSL 之间共享地形及城市缓存设置，可使用 `topo_file_windows`、`topo_file_linux`、`topo_dir_windows`、`topo_dir_linux`、`city_dir_windows` 和 `city_dir_linux`。当前系统对应的非空键会覆盖通用 `topo_file`、`topo_dir` 或 `city_dir`；对应键为空或不存在时会回退到通用值。程序不会解释或解析另一个操作系统的路径。

```json
{
  "topo_file": null,
  "topo_file_windows": null,
  "topo_file_linux": null,
  "topo_dir": "asset",
  "topo_dir_windows": "E:\\terrain\\glo30",
  "topo_dir_linux": "/mnt/e/terrain/glo30",
  "city_dir": "asset/city",
  "city_dir_windows": "E:\\gpx-cache\\city",
  "city_dir_linux": "/mnt/e/gpx-cache/city"
}
```

完整路径优先级为：显式 CLI 路径、当前系统对应的非空专用设置、通用设置、内置默认值（地形为 `asset`，城市数据为 `asset/city`）。完成平台选择后，`topo_file` 仍优先于 `topo_dir`。选中的相对路径以设置文件所在目录为基准解析。

```bash
# 使用任意名称或位置的设置文件
python -m gpx2stl route.gpx --settings ./profiles/large-model.json
```

## 本地地形资源

将本地 `.tif` 或 `.tiff` 高程文件放入 `./asset`。程序会递归扫描该目录，且该目录已被 Git 忽略：

```bash
mkdir -p asset
```

本地文件可使用任意有效的地理坐标参考系统（CRS）。选中的单个文件或多个瓦片必须完整覆盖加边界后的可打印区域；自动边界还需要覆盖更大的搜索窗口。如果本地瓦片与请求区域相交但覆盖不完整，程序会报错，不会静默混合本地与在线数据。

可从以下无需密钥的地址下载 Copernicus 瓦片：

- [GLO-30 Public 瓦片列表](https://copernicus-dem-30m.s3.amazonaws.com/tileList.txt)和[存储桶说明](https://copernicus-dem-30m.s3.amazonaws.com/readme.html)
- [GLO-90 瓦片列表](https://copernicus-dem-90m.s3.amazonaws.com/tileList.txt)

请下载带边界模型所涉及的所有 1° 瓦片，而不只是原始 GPX 中心线经过的瓦片，并遵守 [Copernicus DEM 许可与署名要求](https://registry.opendata.aws/copernicus-dem/)。

## 使用方法

```bash
python -m gpx2stl route.gpx
```

默认在 GPX 文件旁生成 `route.3mf`。目录输入会把该目录直接包含的每个 `.gpx` 或 `.fit` 文件转换为独立模型，绝不会连接不同文件中的路线：

```bash
python -m gpx2stl ./routes

# 将各个独立模型写入一个已存在的目录
python -m gpx2stl ./routes --output ./models
```

Garmin FIT 文件使用相同命令：

```bash
python -m gpx2stl activity.fit
```

对于 FIT 输入，程序把带 GPS 坐标的 `record` 消息作为路线点，将 Garmin semicircle 坐标转换为角度，并优先使用增强海拔。缺少 GPS 的记录或计时器停止事件会分隔路径，不会跨越暂停或数据空缺连接位置。

目录扫描不递归，并以不区分大小写的方式包含 `.gpx` 和 `.fit`。默认情况下，每个输出都写在对应源活动文件旁边，文件名主干不变，扩展名为所选的 `.3mf` 或 `.stl`。同名的 `.gpx` 和 `.fit` 会映射到同一输出，因此程序会明确拒绝。输入为目录时，`--output` 必须指向一个已存在的输出目录。

示例：

程序会输出带时间戳的进度日志，包括解析 GPX、选择或下载地形、生成网格、导出所选格式以及验证输出。

```bash
# 路线、地形和底座相互独立的圆形 3MF
python -m gpx2stl route.gpx --shape circle --max-size 180

# 平顶正六边形模型
python -m gpx2stl route.gpx --shape hex

# 带建筑、齐平路线嵌件和默认 5 米道路匹配的城市模型
python -m gpx2stl route.gpx --mode city

# 北/南各 10%、东/西各 20% 路线边界的城市模型
python -m gpx2stl route.gpx --mode city --route-boundary-percent 10,20,10,20

# 保留原始活动路线，不进行道路匹配
python -m gpx2stl route.gpx --mode city --road-snap-distance 0

# 估算路线涉及的所有山体范围，每侧最多向外搜索 30 km
python -m gpx2stl route.gpx --route-boundary-percent auto --auto-boundary-max-distance-km 30

# 四对象 3MF：底座、圆形地形、凸起全周文字和路线
python -m gpx2stl route.gpx --shape hex --text "MOUNT RAINIER"

# 在默认八个空格的底部接缝上再增加 3 mm
python -m gpx2stl route.gpx --text "  MOUNT RAINIER  " --text-end-gap 3

# 中文或其他文字使用自定义字体
python -m gpx2stl route.gpx --text "路线" --font-file ./fonts/NotoSansCJK-Regular.ttc

# 使用已安装的粗斜体、固定字高，并从接缝左对齐
python -m gpx2stl route.gpx --text "RIDGELINE" --font-family Arial --font-weight bold --font-style italic --font-size 5 --text-align left

# 创建与 3MF 底座表面齐平、深 0.8 mm 的独立文字嵌件
python -m gpx2stl route.gpx --text "RIDGELINE" --text-mode embedded --text-depth 0.8

# 保留现有 STL 底座并使用其平坦顶面
python -m gpx2stl route.gpx --base-stl ./base.stl --text "TRAIL" --text-boundary-percent 15 --route-boundary-percent 10

# 强制使用一个本地 GeoTIFF（无需 API Key 或网络）
python -m gpx2stl route.gpx --topo-source local --topo-file ./terrain.tif

# 递归使用 ./asset 中的瓦片
python -m gpx2stl route.gpx --topo-source local

# 强制使用 OpenTopography
python -m gpx2stl route.gpx --topo-source online --api-key YOUR_KEY

# 带地形的单网格 STL
python -m gpx2stl route.gpx --no-3mf -o route.stl

# 不联网、不生成地形；数值边界也会覆盖设置中的 auto
python -m gpx2stl route.gpx --no-topo --route-boundary-percent 10 --no-3mf

# 手动指定 DEM 数据集
python -m gpx2stl route.gpx --dem-type COP30 --force
```

### 参数

| 参数 | 默认值 | 说明 |
|---|---:|---|
| `input_path` | 必填 | 输入 `.gpx`/`.fit` 文件或目录；目录中直接包含的每个受支持文件会被独立、非递归地转换。 |
| `--settings` | 自动查找 | 显式指定设置文件路径，并覆盖 `.gpx2stl.settings.json` 自动查找。 |
| `-o`, `--output` | 输入文件名 | 文件输入时为输出文件路径；目录输入时为已存在的输出目录。 |
| `--route-width` | 城市：`0.5` mm；其他：`1` mm | 打印路线带宽度。 |
| `--route-height` | 城市：`1.5` mm；其他：`2` mm | 地形模式中路线高出地形的高度；城市路线保持齐平并使用 `--route-depth`。 |
| `--route-depth` | 城市：`1.5` mm；其他：`0.6` mm | 城市模式中齐平路线嵌件及凹槽的深度，必须小于底座厚度。 |
| `--water-depth` | `0.4` mm | 城市模式中齐平水体嵌件及凹槽的深度，必须小于生成或自定义底座厚度。 |
| `--topo`, `--no-topo` | 启用 | 启用或禁用地形。 |
| `--route-boundary-percent` | 城市生成底座：`10,10,10,10`；城市自定义底座：`10`；地形：`auto`；无地形：`10` | 一个有限非负对称百分比、按 `北,东,南,西` 排列的四个逗号分隔百分比，或基于 DEM 搜索山体的 `auto`。设置文件使用四数字 JSON 数组。显式 `auto` 必须启用地形；四方向值仅支持生成底座。 |
| `--auto-boundary-max-distance-km` | `10` km | 自动模式下，从路线包围盒每侧向外搜索的最大距离，必须为有限正数。 |
| `--auto-valley-max-relief-m` | `1000` m | 约 900 m 谷底邻域内平滑高程的最大变化；增大可接受不平整谷底。 |
| `--auto-valley-max-slope-percent` | `100` % | 平滑谷底的最大坡度百分比（`2` 表示 2%，不是 200%）。 |
| `--auto-valley-max-height-m` | `1000` m | 高于搜索窗口第 10 百分位高程基准的最大高度，同时受下方高差百分比限制。 |
| `--auto-valley-max-height-percent` | `100` % | 高于该基准的最大高度占窗口可观测高差的百分比，范围为 0 到 100。 |
| `--shape` | `square` | `square`（方形）、`circle`（圆形）或平顶 `hex`（正六边形）；方形保持正北朝上。 |
| `--text` | 无 | 沿平坦外框放置的紧凑文字段；预留接缝位于底部中央。 |
| `--text-height` | `1` mm | 文字高出外框的厚度。 |
| `--text-margin` | 自动 | 字形轮廓与平坦文字边框内外两侧边界之间的最小间距，单位为毫米。 |
| `--text-end-gap` | `0` mm | 在默认八个字体空格及 `--text` 首尾空格之外，额外增加的底部接缝间距。 |
| `--text-align` | `center` | 相对底部接缝在可用周长内将紧凑文字段设为 `left`、`center` 或 `right`。 |
| `--text-mode` | `raised` | 使用 `raised` 凸起文字或 `embedded` 齐平嵌件；嵌入模式仅支持 3MF。 |
| `--text-depth` | `0.6` mm | 嵌入模式中文字凹槽和齐平嵌件的深度。 |
| `--text-boundary-percent` | `7` | 每侧文字边框内缩占外宽的百分比（自定义底座使用顶面较小尺寸）；大于等于 0 且小于 50，无文字时忽略。 |
| `--font-family` | `DejaVu Sans` | 用于文字的已安装字体族。 |
| `--font-file` | 无 | 精确指定自定义 `.ttf`、`.otf` 或 `.ttc` 字体文件；不可与字体族、粗细或样式选项组合。 |
| `--font-size` | 自动 | 字形高度（毫米）；省略时自动适配。 |
| `--font-weight` | `normal` | 已安装字体族的 `normal` 或 `bold` 变体。 |
| `--font-style` | `normal` | 已安装字体族的 `normal` 或 `italic` 变体。 |
| `--base-stl` | 无 | 使用毫米单位、Z 轴朝上的现有 STL；其最高平坦顶面成为自定义地形外形。 |
| `--3mf`, `--no-3mf` | 3MF | 选择多材料 3MF 或单网格 STL。 |
| `--max-size` | `200` mm | 最终模型 X/Y 最大尺寸。 |
| `--terrain-height` | 自动 | 地形高度差（mm）。默认使用与 `--max-size` 相同的真实世界到模型比例；显式设置后会将地形高度差归一化到该值。 |
| `--base-height` | `2` mm | 实体底座厚度。 |
| `--topo-source` | `auto` | `auto` 优先使用完整本地数据，`online` 使用 OpenTopography，`local` 禁止联网回退。 |
| `--topo-file` | 无 | 一个任意有效 CRS 的本地 GeoTIFF；优先于 `--topo-dir`。 |
| `--topo-dir` | `./asset` | 递归扫描 `.tif` 和 `.tiff` 瓦片的目录。 |
| `--dem-type` | 自动 | 手动指定 OpenTopography DEM。 |
| `--api-key` | 环境变量 | 手动指定 OpenTopography API Key。 |
| `--force` | 关闭 | 覆盖已有输出文件。 |

## 几何与缩放规则

- 经纬度先转换到以路线中心为原点的局部米制投影，再进行等比例缩放，使带边界的模型不超过 `--max-size`。
- 计算底座范围时会额外预留路线宽度；即使边界百分比为零，路线也不会超出底座。
- 默认情况下，地形高程与 X/Y 使用相同比例：由 `--max-size` 得出的水平缩放比例也应用于 DEM 高程范围。例如，水平比例为 1:20,000 时，1,000 m 高程同样对应 50 mm。
- 显式设置 `--terrain-height` 会覆盖真实比例，将 DEM 最低点到最高点的高度差归一化到指定毫米数。
- 禁用地形时，路线顶部采用 GPX 或 FIT 高程和真实的 1:20,000 垂直比例：1,000 m 对应 50 mm。内部缺失高程会插值；端点或全部高程缺失会报错。
- 城市模式会把带 OSM `bridge` 标记的道路、铁路和桥梁外形生成为可打印桥面。建筑和桥梁高度在水平地图比例之后默认放大 5 倍，以免较矮结构在城市尺度模型中消失；将 `building_height_scale` 设为 `1` 可恢复真实比例。
- 城市水体包括 OSM 湖泊、池塘、水库、流域、河岸，按标注或推断宽度生成的河流/溪流/运河，以及有向海岸线的临海一侧。水体会裁剪到可打印地形，并作为独立且表面齐平的 `Water` 嵌件输出；路线穿过水面时路线材料优先。
- 数值边界模式下，方形为加边界前包围路线的最小正北方形；圆形为真实最小包围圆；六边形为可平移的最小平顶正六边形。自动模式同时适配八方向地理选择多边形与路线。
- 四方向边界先独立扩大路线包围盒：北/南百分比以南北跨度为基准，东/西百分比以东西跨度为基准，再围绕该矩形适配方形、圆形或六边形。城市模式默认 `10,10,10,10`。某轴跨度为零时，该轴百分比不会增加距离，但仍会预留可打印路线宽度。
- 指定 `--text` 后，`--max-size` 控制方形、圆形或六边形外框尺寸。居中地形圆的直径为 `max_size × (1 - 2 × text_boundary_percent / 100)`，其余外框保持在底座顶面的平坦高度。默认文字边界 7% 保留 86% 地形直径。六边形需要大于约 6.7% 才能在较窄两侧留出文字带；字形及边距可能需要更多空间。
- 数值 `--route-boundary-percent` 在缩放前给最小路线外形增加边界：方形边长乘以 `1 + 2 × route_boundary_percent / 100`，圆形或六边形半径乘以 `1 + route_boundary_percent / 100`，路线宽度另行预留。改变路线边界不会改变文字带。无文字时忽略文字边界，使用完整生成外形。
- 在 3MF 输出中，生成的棱柱或提供的自定义 STL 会保留为独立的 `Base` 对象。启用的起伏地形是另一个独立水密的 `Topography` 对象，并略微伸入底座以确保切片可靠。禁用地形时不会生成该对象。
- 文字默认使用 DejaVu Sans，并沿周长切线方向形成紧凑文字段。`--text-align` 相对底部接缝定位文字段，不会拉伸字符间距。省略 `--font-size` 时自动适配字高；可用字体族、粗细和样式选项选择已安装变体，或用 `--font-file` 精确指定一个字体文件。缺少字体、变体或字形时会明确报错。
- 凸起文字会略微伸入底座，并在 STL 输出中与其他部分合并。仅限 3MF 的嵌入模式会在底座切出凹槽，并以独立文字材料对象填充至表面齐平；`--text-depth` 控制嵌件深度。
- 支持跨越 ±180° 日期变更线的路线；此时会拆分 DEM 请求。

### 自动山体边界

`--route-boundary-percent auto`（JSON：`"route_boundary_percent": "auto"`）基于 DEM 的山顶/山谷分割，估算完整路线涉及的所有山体地形，包括跨越多个山峰的路线。程序逐步扩大嵌套搜索窗口，独立分析八个方向：N（北）、NE（东北）、E（东）、SE（东南）、S（南）、SW（西南）、W（西）、NW（西北）。可靠边界要求山谷边界位于搜索窗口内部，且相邻窗口间该方向的地形几何稳定。后续证据变化时，候选边界可能重新变为未确定。这是基于地形的估算，不能保证精确对应某座具名山峰的边界。

自动模式保留每个可靠识别方向的约束，包括完整分析单元范围与安全余量，即使其留白超过 100%。完成正常的有限搜索后，达到搜索上限仍未确定的方向按该方向的路线投影跨度向外留白 100%。没有可用识别结果时（无山谷标记、平坦或模糊地形、或没有路线涉及的山体区域），八个方向全部回退，并明确警告；不会将其描述为完整山体识别成功。

方向使用路线局部米制投影中的单位法向量（X 向东，Y 向北）。对于每个法向量 `u`，回退规则为：

```text
route_max(u) = max(route_points dot u)
route_span(u) = max(route_points dot u) - min(route_points dot u)
point dot u <= route_max(u) + route_span(u)
```

NE 使用 `u = (1, 1) / sqrt(2)` 及自身的路线投影跨度，而不是北、东留白之和。W 使用 `u = (-1, 0)`，相当于向西增加路线东西跨度的留白。某方向投影跨度为零时，使用八个路线投影中最大的非零跨度，并警告说明替代的百分比基准。如果八个跨度全部为零且需要回退，则输入无效，程序会给出可操作的错误提示。

八个识别/回退半平面的交集形成最多八条边的凸地理选择多边形。部分约束可能冗余，因此不一定恰有八条边，也不是必然的正八边形。未确定的对角方向回退可能裁切相邻已识别方向的地形。回退限制优先；相邻方向已配置的识别约束仍保留，但最终多边形在该方向的实际范围可能缩小。警告会明确说明无法保证完整山体覆盖。

选择多边形用于重新定位地理中心，并按需缩小路线，同时保留选定的方形/圆形/六边形、模型实际尺寸和不变的独立文字带。自定义 STL 的原始外形与尺寸同样保持不变。在固定模型尺寸下，更大的地理范围也会降低默认真实比例的地形起伏高度。100% 回退是地理选择限制，不是打印外形的硬性上限：外形适配和路线宽度余量可能包含额外地形。不对称地理留白不会将打印底座变成不规则形状。

每次搜索窗口后，日志报告搜索距离和全部八方向状态：已找到且稳定的山谷边界、等待稳定的候选边界、或未确定及其原因。最终结果区分完整识别、部分回退、全部回退，逐方向报告已找到山谷边界或使用回退，并列出留白距离、百分比基准及适用的回退原因。另行报告最终打印外形在全部八方向的实际投影留白；这些值可能超过 100%，不等于请求的地理选择限制。路线投影跨度为零时，实际外形留白的百分比会明确标为未定义，避免误导。

默认最多从路线包围盒每侧向外搜索 10 km，可通过 `--auto-boundary-max-distance-km` 或 JSON `auto_boundary_max_distance_km` 修改。更大窗口可能增加处理时间、内存、DEM 下载和 API 成本。程序可按现有 `--topo-source` 策略获取更大 DEM 覆盖；`local` 绝不下载，显式 `--topo-file` 必须覆盖所需区域。最终可打印外形所需的数据范围可能超出搜索区域。

缺失或非有限 DEM 高程、有效分辨率不足、资源限制、无效输入以及意外失败仍会明确报错，不属于回退条件。方向回退不会放宽 DEM 覆盖要求。可改用更好的本地 DEM 或适当调整搜索上限；仍可显式选择数值边界，例如 `--route-boundary-percent 10`。

自动检测使用固定锚点的 90 m 分析网格，最多 1,000,000 个单元，并要求实际参与采样的 DEM 像素不大于 270 m。需要至少 60 m 的可观测高差，以及可用的山顶/山谷证据。默认山谷条件现在非常宽松：约 900 m 邻域内平滑高差可达 1000 m，坡度可达 100%（45°），且高于窗口第 10 百分位高程基准的高度不超过 `min(1000 m, 窗口高差的 100%)`。这些默认值可能把山坡归类为山谷，或在分割中抹去前景山顶，导致没有可用山体区域并触发回退，而非识别成功。证据要求、独立方向稳定性检查、回退规则和明确报错行为保持不变。

四项阈值均可通过 CLI 和 JSON 设置修改（JSON 键将连字符换为下划线）。必须为有限非负数，高度百分比不得超过 100。增大数值可放宽条件，降低数值则收紧条件。所有条件必须同时满足：如果邻域高差或基准高度仍不符合要求，仅放宽坡度不会有效。有效高度上限始终为米制上限与高差百分比上限中的**较小值**。这些选项只影响自动边界，数值边界模式保持不变。

例如，降低全部四项阈值，可尝试比默认值更保守的条件：

```bash
python -m gpx2stl route.gpx --route-boundary-percent auto --auto-valley-max-relief-m 35 --auto-valley-max-slope-percent 3 --auto-valley-max-height-m 150 --auto-valley-max-height-percent 15
```

对应 JSON：

```json
{
  "route_boundary_percent": "auto",
  "auto_valley_max_relief_m": 35,
  "auto_valley_max_slope_percent": 3,
  "auto_valley_max_height_m": 150,
  "auto_valley_max_height_percent": 15
}
```

这些数值仅用于试验，不是通用预设，也不保证识别成功。日志会打印当前阈值。放宽条件可能把缓坡山麓误认为谷底、裁剪山坡或改变所选区域；边界仍必须通过独立方向稳定性检查，未确定方向仍使用现有的 100% 回退。高程基准依赖整个窗口，并非局部水系或鞍部检测，因此调参不能保证识别所有可见山谷。DEM 覆盖、分辨率及资源检查保持严格。

省略路线边界时，城市模式默认使用四方向 `10,10,10,10`，普通地形模式默认使用 `auto`，禁用地形默认使用数值 `10`。显式 `auto` 与 `--no-topo` 同时使用会报错，包括设置文件中的 `auto`；此时用 `--no-topo --route-boundary-percent 10` 覆盖。没有单独的自动边界布尔开关。标量数值边界行为和独立文字边界保持不变。纳入版本控制的设置模板为默认地形配置保留 `"auto"`；城市配置可使用上文的四数字 JSON 数组。

### 自定义 STL 底座

`--base-stl` 会保留输入网格原有的位置、方向和尺寸。STL 文件不包含单位信息，因此程序假定输入使用毫米，并且已按 Z 轴朝上放置。自定义底座模式在确定底座尺寸和布局时忽略 `--shape`、`--base-height` 和 `--max-size`。

输入必须是顶点有限、绕序一致、水密且体积为正的网格，并且在全局最高 Z 处必须恰好有一个相连、水平且朝上的顶面区域。该区域可以是凹多边形，也可以包含孔洞。只有曲面/斜面最高点或有多个不相连最高区域的模型会被拒绝。

有文字时，地形区域使用顶面的精确轮廓，并按以下距离向内缩：

```text
内缩距离 = text-boundary-percent / 100 × min(顶面宽度, 顶面高度)
```

无文字时不进行文字内缩，地形使用完整顶面。GPX 保持正北朝上。数值模式以可用地形区域为中心，先求出在边缘预留半个路线宽度后的最大缩放，再除以 `1 + route_boundary_percent / 100`，以添加独立的路线边界。自动模式将完整八方向选择多边形与路线带一起适配，按需重新定位地理中心并缩小比例。程序检查完整包含关系；与凹边或孔洞冲突时会明确报错，不会裁切选择多边形或路线。改变路线边界不会改变地形区域或文字带。自定义 STL 本身绝不会被 `--max-size` 缩放。最终 DEM 请求范围根据地形区域计算。

自定义 STL 支持城市模式。OSM 建筑、道路、桥梁和水体会裁剪到精确的内缩自定义顶面；地形和建筑贴合该表面，齐平路线及水体凹槽直接切入自定义底座。路线及水体深度必须小于自定义底座厚度。自定义适配支持单个对称百分比或 `auto`；由于自定义顶面可以是任意外形，四方向百分比会被明确拒绝。因此城市模式配合自定义底座且省略边界时默认使用标量 `10`。

指定 `--text` 后，除非设置 `--font-size`，程序会选择可容纳的最大字高。紧凑文字段与连续边框路径相切，并遵循 `--text-align`。文字首尾接缝固定在底部中央，并保留八个字体空格、`--text-end-gap` 以及引号内首尾空格的总间距。`--text-margin` 可指定文字与外侧形状边界及内侧地形边界之间的最小毫米间距。每个字形都必须完整位于剩余区域内。如果空间不足，请增大 `--text-boundary-percent`，减小字高、文字边距或接缝，缩短文字或选择更窄的字体。

## 地形数据源规则

- `auto`（默认）：优先使用 `--topo-file`，否则使用 `--topo-dir` 中相交的瓦片。缺少且匹配的 GLO-30 瓦片会从公开的 Copernicus AWS 存储桶匿名下载到 `--topo-dir`，并作为持久缓存重复使用。如果 GLO-30 不可用且已配置 API Key，则回退到 OpenTopography。
- `local`：要求本地数据完整覆盖，绝不访问网络。
- `online`：忽略本地数据并使用 OpenTopography。

指定 `--topo-file` 后只使用该文件。程序先读取目录瓦片的元数据进行筛选，不会把无关的全球瓦片全部加载到内存。

### OpenTopography

程序通过 `https://portal.opentopography.org/API/globaldem` 获取 GeoTIFF，并自动选择：

- 请求区域完全位于南纬 56° 到北纬 60° 时使用 `SRTMGL1`。
- 其他区域使用 `COP30`。

OpenTopography 要求 API Key，并有请求范围和频率限制。API、认证、无数据或无效栅格错误都会终止转换并返回非零退出码。发布衍生模型时，请遵守 [OpenTopography 使用条款](https://opentopography.org/terms-use)并添加适当的数据来源说明。

## Bambu Studio

导入生成的 3MF 时，如有提示请选择作为“一个对象的多个部件”载入。文件只包含下列具有实际几何体的对象：

1. `GPX route` / `Filament 1 - Route`
2. 启用地形时包含 `Topography` / `Filament 2 - Topography`
3. 指定 `--text` 时包含 `Text` / `Filament 3 - Text`
4. `Base` / `Filament 4 - Base`
5. 城市模式包含 `Buildings` / `Filament 5 - Buildings`
6. 城市模式存在水体时包含 `Water` / `Filament 6 - Water`

地形模式保留四个材料槽位，因此底座始终映射到耗材 4；城市模式增加第五个建筑材料，并在存在水体时增加第六个水体材料。没有实际几何体的可选对象会被省略。切片前请确认各部件分别映射到正确的 AMS/耗材槽位。

## 开发与测试

```bash
uv sync --extra dev
source .venv/bin/activate
python -m pytest
```

自动化测试使用合成 GPX 和 GeoTIFF 数据，不需要联网或真实 API Key。

## 许可证

本项目采用 [MIT License](LICENSE)。地形数据集仍受其各自的许可证与署名要求约束。城市数据 © [OpenStreetMap contributors](https://www.openstreetmap.org/copyright)，按 ODbL 提供；发布使用这些数据的模型时必须保留相应署名。