# gpx2stl

Convert GPX tracks/routes and Garmin FIT activities into printable terrain models:

- **3MF by default:** separate named objects and materials for Bambu Studio. Filament 1 is the GPX route, filament 2 is topography, filament 3 is optional text, and filament 4 is the base.
- **STL on request:** one watertight mesh containing the base, route, and all enabled features.
- **Flexible topography:** use local GeoTIFF files first or download SRTMGL1/COP30 data from OpenTopography.
- **File or folder input:** convert one `.gpx`/`.fit` file or every supported activity file directly in a folder; files remain separate models.
- **Square, circular, or hexagonal base:** automatically sized around every path in each input activity.
- **Custom STL base:** preserve an existing model and use its highest flat top as the exact terrain shape.
- **Optional perimeter text:** place terrain in a centered inset and wrap raised text around the full flat outer frame.

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

JSON keys use the Python/long-option names with underscores, such as `route_width`, `boundary_percent`, `text_margin`, `text_end_gap`, `inner_size_percent`, `topo_source`, and `topo_dir`. The positional `.gpx`/`.fit` file-or-directory input can also be defaulted with the backward-compatible `gpx_file` key. Use `use_3mf` for the `--3mf` / `--no-3mf` setting. Relative `font_file` paths are resolved from the settings file like other paths. Unknown keys, invalid JSON, or incorrect value types produce an explicit error. `.gpx2stl.settings.json` is ignored by Git; `settings.example.json` is tracked as a complete template.

For terrain paths shared between Windows and Linux/WSL, settings support `topo_file_windows`, `topo_file_linux`, `topo_dir_windows`, and `topo_dir_linux`. A non-null key matching the current OS overrides the corresponding generic `topo_file` or `topo_dir`. A null or absent OS-specific key falls back to the generic value. Paths for the inactive OS are not interpreted or resolved.

```json
{
  "topo_file": null,
  "topo_file_windows": null,
  "topo_file_linux": null,
  "topo_dir": "asset",
  "topo_dir_windows": "E:\\terrain\\glo30",
  "topo_dir_linux": "/mnt/e/terrain/glo30"
}
```

The complete path precedence is explicit `--topo-file` / `--topo-dir`, then the matching non-null OS-specific setting, then the generic setting, then the built-in `asset` directory default. `topo_file` still takes precedence over `topo_dir` after platform selection. Relative selected paths are resolved from the settings-file directory.

```bash
# Use a settings file with any name or location
python -m gpx2stl route.gpx --settings ./profiles/large-model.json
```

## Local terrain assets

Place local `.tif` or `.tiff` elevation files under `./asset`. The directory is scanned recursively and is ignored by Git:

```bash
mkdir -p asset
```

Local files may use any valid georeferenced CRS. The complete padded printable footprint must be covered by the selected file or tiles. If local tiles intersect the footprint but leave a gap, conversion fails rather than silently mixing local and online elevations.

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

# Four-object 3MF: base, circular topography, raised text, and route
python -m gpx2stl route.gpx --shape hex --text "MOUNT RAINIER"

# Add 3 mm to the default eight-space bottom seam
python -m gpx2stl route.gpx --text "  MOUNT RAINIER  " --text-end-gap 3

# Use a custom font for Chinese or another script
python -m gpx2stl route.gpx --text "路线" --font-file ./fonts/NotoSansCJK-Regular.ttc

# Preserve an existing STL base and use its flat top
python -m gpx2stl route.gpx --base-stl ./base.stl --boundary-percent 10

# Force one local GeoTIFF (no API key or network)
python -m gpx2stl route.gpx --topo-source local --topo-file ./terrain.tif

# Recursively use tiles under ./asset
python -m gpx2stl route.gpx --topo-source local

# Force OpenTopography instead of local data
python -m gpx2stl route.gpx --topo-source online --api-key YOUR_KEY

# Single-mesh STL with terrain
python -m gpx2stl route.gpx --no-3mf -o route.stl

# No network/topography; use GPX elevation for route height
python -m gpx2stl route.gpx --no-topo --no-3mf

# Override automatic DEM selection
python -m gpx2stl route.gpx --dem-type COP30 --force
```

### Options

| Option | Default | Description |
|---|---:|---|
| `input_path` | required | Input `.gpx`/`.fit` file or directory. A directory converts each directly contained supported file independently and non-recursively. |
| `--settings` | discovered | Explicit settings file path. Overrides `.gpx2stl.settings.json` discovery. |
| `-o`, `--output` | input stem | Output file path for a file input, or an existing output directory for a directory input. |
| `--route-width` | `1` mm | Printed route ribbon width. |
| `--route-height` | `2` mm | Route height above terrain in topo mode. |
| `--topo`, `--no-topo` | topo | Enable or disable terrain. |
| `--boundary-percent` | `10` | Padding added around the minimum selected footprint. |
| `--shape` | `square` | `square`, `circle`, or flat-top `hex`. Squares remain north-up. |
| `--text` | none | Raised text wrapped once around the complete flat frame, with its head/tail seam centered at the bottom. |
| `--text-height` | `1` mm | Raised text thickness above the frame. |
| `--text-margin` | automatic | Minimum clearance in millimeters between glyph outlines and both boundaries of the flat text frame. |
| `--text-end-gap` | `0` mm | Extra bottom seam gap added to the default eight font spaces and any leading/trailing spaces in `--text`. |
| `--inner-size-percent` | `70` | Terrain inset diameter as a percentage of the outer model width; must be above 0 and below 100. |
| `--font-file` | built-in | Custom `.ttf`, `.otf`, or `.ttc` font for scripts not covered by the built-in font. |
| `--base-stl` | none | Existing millimeter-scale, Z-up STL whose highest flat top becomes the custom terrain shape. |
| `--3mf`, `--no-3mf` | 3MF | Select multi-material 3MF or single-mesh STL. |
| `--max-size` | `200` mm | Maximum final X/Y dimension. |
| `--terrain-height` | automatic | Terrain relief in mm. By default, elevation uses the same real-world-to-model ratio as `--max-size`; an explicit value normalizes relief to that height. |
| `--base-height` | `2` mm | Solid base thickness. |
| `--topo-source` | `auto` | `auto` uses complete local coverage first, `online` uses OpenTopography, and `local` disables network fallback. |
| `--topo-file` | none | One local GeoTIFF in any valid CRS; takes precedence over `--topo-dir`. |
| `--topo-dir` | `./asset` | Directory recursively scanned for `.tif` and `.tiff` tiles. |
| `--dem-type` | automatic | OpenTopography DEM identifier override. |
| `--api-key` | environment | OpenTopography API key override. |
| `--force` | off | Replace an existing output file. |

## Geometry and scaling

- Geographic coordinates are projected into a route-centered local metric projection, then uniformly scaled so the padded footprint fits `--max-size`.
- The route width is included as extra footprint clearance, including when boundary padding is zero.
- By default, terrain elevations use the same scale as X/Y: the horizontal ratio derived from `--max-size` is applied to the DEM elevation range. For example, a horizontal scale of 1:20,000 also makes 1,000 m of elevation equal 50 mm.
- Setting `--terrain-height` explicitly overrides true-scale relief and normalizes the DEM minimum-to-maximum range to that many millimeters.
- Without topo, GPX or FIT altitude controls the route top at physical 1:20,000 vertical scale: 1,000 m becomes 50 mm. Internal missing elevations are interpolated; missing endpoint/all elevations are errors.
- Square output is the smallest north-up square around the route before padding. Circle output uses the true minimum enclosing circle. Hex output uses the minimum translated flat-top regular hexagon.
- When `--text` is present, `--max-size` controls the outer square, circle, or hex frame. Terrain and route are instead scaled into a centered circle controlled by `--inner-size-percent`; the rest of the frame stays flat at the base-top height.
- In 3MF output, the generated prism or supplied custom STL remains a separate `Base` object. Enabled relief is a separately watertight `Topography` object with a small intentional overlap into the base for reliable slicing. Disabling topo omits that object.
- Text uses the built-in DejaVu Sans font for Latin letters, digits, and punctuation. Characters wrap once around the full frame, remain tangent to its perimeter, and are automatically reduced to fit. The head/tail seam is centered at the bottom and reserves eight font spaces by default. `--text-end-gap` adds an absolute millimeter gap, while quoted leading/trailing spaces add font-relative gap. Use `--text-margin` to reserve clearance from the inner and outer frame boundaries. Use `--font-file` for Chinese or other scripts; missing glyphs are reported as errors.
- In STL output, raised text is unioned with the frame, route, and terrain into one watertight mesh.
- Routes crossing the ±180° antimeridian are supported by split DEM requests.

### Custom STL bases

`--base-stl` preserves the input mesh’s original position, orientation, and dimensions. STL files have no unit metadata, so the input is assumed to use millimeters and must already be oriented with Z up. Custom-base mode ignores `--shape`, `--base-height`, `--max-size`, and `--inner-size-percent` for base sizing and layout.

The input must be a finite, consistently wound, watertight, positive-volume mesh with exactly one connected horizontal upward-facing region at its global maximum Z. That top region may be concave and may contain holes. Models with only curved/sloped maxima or multiple disconnected highest regions are rejected.

The terrain region follows the exact top outline after an inward offset:

```text
offset distance = boundary-percent / 100 × min(top width, top height)
```

The GPX remains north-up, is centered on the usable inset, and receives the largest uniform scale whose complete route-width ribbon fits inside the region without crossing concave edges or holes. The custom STL itself is never resized by `--max-size`. DEM bounds are derived from this fitted custom region.

When `--text` is supplied, the largest fitting glyph height is selected and the text wraps once around the full remaining flat border. Characters stay tangent to the continuous perimeter path, and extra path length is distributed between characters. The head/tail seam remains centered at the bottom and reserves eight font spaces plus `--text-end-gap` and any quoted leading/trailing spaces. `--text-margin` reserves the requested minimum clearance in millimeters from both the outer shape boundary and the inner terrain boundary. Every glyph must fit entirely in the remaining area. Increase `--boundary-percent`, reduce the text margins/gap, shorten the text, or choose a narrower font if the border cannot contain it.

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

With topo and text enabled, the 3MF therefore contains four objects. Without either optional feature, its object is omitted. The four fixed material slots remain available so the base consistently maps to filament 4. Confirm or remap the parts to the desired AMS/filament slots before slicing.

## Development

```bash
uv sync --extra dev
source .venv/bin/activate
python -m pytest
```

Tests use synthetic GPX and GeoTIFF data and do not require network access or a real API key.

## License

This project is licensed under the [MIT License](LICENSE). Terrain datasets remain subject to their respective licenses and attribution requirements.

---

# 中文说明

`gpx2stl` 可将 GPX 轨迹/路线和 Garmin FIT 活动转换为适合 3D 打印的地形模型：

- **默认输出 3MF：**包含可导入 Bambu Studio 的独立命名对象和材料。耗材 1 用于 GPX 路线，耗材 2 用于地形，耗材 3 用于可选文字，耗材 4 用于底座。
- **可选输出 STL：**底座、路线和所有启用的功能合并为一个水密网格。
- **灵活的真实地形：**优先使用本地 GeoTIFF，或通过 OpenTopography 下载 SRTMGL1/COP30 高程数据。
- **文件或目录输入：**可转换一个 `.gpx`/`.fit` 文件，或目录中直接包含的所有受支持活动文件；不同文件始终生成独立模型。
- **方形、圆形或六边形底座：**根据每个输入活动中的全部路径自动确定范围。
- **自定义 STL 底座：**保留现有模型，并将其最高的平坦顶面作为精确地形外形。
- **可选全周文字：**将地形放入居中的区域，并让凸起文字沿完整平坦外框环绕。

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

JSON 键使用 Python/长参数对应的下划线名称，例如 `route_width`、`boundary_percent`、`text_margin`、`text_end_gap`、`inner_size_percent`、`topo_source` 和 `topo_dir`。也可用向后兼容的 `gpx_file` 键设置默认 `.gpx`/`.fit` 文件或目录；`--3mf` / `--no-3mf` 对应 `use_3mf`。相对 `font_file` 路径与其他路径一样，以设置文件所在目录为基准解析。未知键、无效 JSON 或错误的数据类型都会产生明确错误。`.gpx2stl.settings.json` 已被 Git 忽略，而完整模板 `settings.example.json` 会纳入版本控制。

为了在 Windows 与 Linux/WSL 之间共享地形设置，可使用 `topo_file_windows`、`topo_file_linux`、`topo_dir_windows` 和 `topo_dir_linux`。当前系统对应的非空键会覆盖通用 `topo_file` 或 `topo_dir`；对应键为空或不存在时会回退到通用值。程序不会解释或解析另一个操作系统的路径。

```json
{
  "topo_file": null,
  "topo_file_windows": null,
  "topo_file_linux": null,
  "topo_dir": "asset",
  "topo_dir_windows": "E:\\terrain\\glo30",
  "topo_dir_linux": "/mnt/e/terrain/glo30"
}
```

完整路径优先级为：显式 `--topo-file` / `--topo-dir`、当前系统对应的非空专用设置、通用设置、内置 `asset` 目录默认值。完成平台选择后，`topo_file` 仍优先于 `topo_dir`。选中的相对路径以设置文件所在目录为基准解析。

```bash
# 使用任意名称或位置的设置文件
python -m gpx2stl route.gpx --settings ./profiles/large-model.json
```

## 本地地形资源

将本地 `.tif` 或 `.tiff` 高程文件放入 `./asset`。程序会递归扫描该目录，且该目录已被 Git 忽略：

```bash
mkdir -p asset
```

本地文件可使用任意有效的地理坐标参考系统（CRS）。选中的单个文件或多个瓦片必须完整覆盖加边界后的可打印区域。如果本地瓦片与模型相交但覆盖不完整，程序会报错，不会静默混合本地与在线数据。

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

# 四对象 3MF：底座、圆形地形、凸起全周文字和路线
python -m gpx2stl route.gpx --shape hex --text "MOUNT RAINIER"

# 在默认八个空格的底部接缝上再增加 3 mm
python -m gpx2stl route.gpx --text "  MOUNT RAINIER  " --text-end-gap 3

# 中文或其他文字使用自定义字体
python -m gpx2stl route.gpx --text "路线" --font-file ./fonts/NotoSansCJK-Regular.ttc

# 保留现有 STL 底座并使用其平坦顶面
python -m gpx2stl route.gpx --base-stl ./base.stl --boundary-percent 10

# 强制使用一个本地 GeoTIFF（无需 API Key 或网络）
python -m gpx2stl route.gpx --topo-source local --topo-file ./terrain.tif

# 递归使用 ./asset 中的瓦片
python -m gpx2stl route.gpx --topo-source local

# 强制使用 OpenTopography
python -m gpx2stl route.gpx --topo-source online --api-key YOUR_KEY

# 带地形的单网格 STL
python -m gpx2stl route.gpx --no-3mf -o route.stl

# 不联网、不生成地形；使用 GPX 高程生成路线高度
python -m gpx2stl route.gpx --no-topo --no-3mf

# 手动指定 DEM 数据集
python -m gpx2stl route.gpx --dem-type COP30 --force
```

### 参数

| 参数 | 默认值 | 说明 |
|---|---:|---|
| `input_path` | 必填 | 输入 `.gpx`/`.fit` 文件或目录；目录中直接包含的每个受支持文件会被独立、非递归地转换。 |
| `--settings` | 自动查找 | 显式指定设置文件路径，并覆盖 `.gpx2stl.settings.json` 自动查找。 |
| `-o`, `--output` | 输入文件名 | 文件输入时为输出文件路径；目录输入时为已存在的输出目录。 |
| `--route-width` | `1` mm | 打印路线带宽度。 |
| `--route-height` | `2` mm | 启用地形时路线高出地形的高度。 |
| `--topo`, `--no-topo` | 启用 | 启用或禁用地形。 |
| `--boundary-percent` | `10` | 在所选最小外形周围增加的边界百分比。 |
| `--shape` | `square` | `square`（方形）、`circle`（圆形）或平顶 `hex`（正六边形）；方形保持正北朝上。 |
| `--text` | 无 | 沿完整平坦外框环绕一周的凸起文字；文字首尾接缝位于底部中央。 |
| `--text-height` | `1` mm | 文字高出外框的厚度。 |
| `--text-margin` | 自动 | 字形轮廓与平坦文字边框内外两侧边界之间的最小间距，单位为毫米。 |
| `--text-end-gap` | `0` mm | 在默认八个字体空格及 `--text` 首尾空格之外，额外增加的底部接缝间距。 |
| `--inner-size-percent` | `70` | 圆形地形区域直径占模型外宽的百分比；必须大于 0 且小于 100。 |
| `--font-file` | 内置字体 | 为内置字体未覆盖的文字系统指定自定义 `.ttf`、`.otf` 或 `.ttc` 字体。 |
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
- 方形为加边界前包围路线的最小正北方形；圆形为真实最小包围圆；六边形为可平移的最小平顶正六边形。
- 指定 `--text` 后，`--max-size` 控制方形、圆形或六边形外框尺寸。地形和路线会缩放到由 `--inner-size-percent` 控制的居中圆形区域，其余外框保持在底座顶面的平坦高度。
- 在 3MF 输出中，生成的棱柱或提供的自定义 STL 会保留为独立的 `Base` 对象。启用的起伏地形是另一个独立水密的 `Topography` 对象，并略微伸入底座以确保切片可靠。禁用地形时不会生成该对象。
- 拉丁字母、数字和标点默认使用内置 DejaVu Sans 字体。字符沿完整外框环绕一周，与边框路径相切并自动缩小以适应空间。文字首尾接缝固定在底部中央，默认保留八个字体空格。`--text-end-gap` 可增加绝对毫米间距，用引号保留的首尾空格会增加随字体缩放的间距。可用 `--text-margin` 保留文字与内外边框之间的间距。中文或其他文字请使用 `--font-file`；字体缺少字形时会明确报错。
- 输出 STL 时，凸起文字会与外框、路线和地形合并为一个水密网格。
- 支持跨越 ±180° 日期变更线的路线；此时会拆分 DEM 请求。

### 自定义 STL 底座

`--base-stl` 会保留输入网格原有的位置、方向和尺寸。STL 文件不包含单位信息，因此程序假定输入使用毫米，并且已按 Z 轴朝上放置。自定义底座模式在确定底座尺寸和布局时忽略 `--shape`、`--base-height`、`--max-size` 和 `--inner-size-percent`。

输入必须是顶点有限、绕序一致、水密且体积为正的网格，并且在全局最高 Z 处必须恰好有一个相连、水平且朝上的顶面区域。该区域可以是凹多边形，也可以包含孔洞。只有曲面/斜面最高点或有多个不相连最高区域的模型会被拒绝。

地形区域使用顶面的精确轮廓，并按以下距离向内缩：

```text
内缩距离 = boundary-percent / 100 × min(顶面宽度, 顶面高度)
```

GPX 保持正北朝上，以可用内缩区域为中心，并采用能使完整路线宽度带位于区域内、不穿过凹边或孔洞的最大等比例缩放。自定义 STL 本身绝不会被 `--max-size` 缩放。DEM 请求范围根据拟合后的自定义区域计算。

指定 `--text` 后，程序会选择可容纳的最大字高，让文字沿剩余的完整平坦边框环绕一周。每个字符都与连续的边框路径相切，多余路径长度会分配到字符之间。文字首尾接缝固定在底部中央，并保留八个字体空格、`--text-end-gap` 以及引号内首尾空格的总间距。`--text-margin` 可指定文字与外侧形状边界及内侧地形边界之间的最小毫米间距。每个字形都必须完整位于剩余区域内。如果空间不足，请增大 `--boundary-percent`、减小文字边距或接缝、缩短文字或选择更窄的字体。

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

同时启用地形和文字时，3MF 共包含四个对象。未启用地形或文字时，对应对象会被省略。四个固定材料槽位仍会保留，因此底座始终映射到耗材 4。切片前请确认各部件分别映射到正确的 AMS/耗材槽位。

## 开发与测试

```bash
uv sync --extra dev
source .venv/bin/activate
python -m pytest
```

自动化测试使用合成 GPX 和 GeoTIFF 数据，不需要联网或真实 API Key。

## 许可证

本项目采用 [MIT License](LICENSE)。地形数据集仍受其各自的许可证与署名要求约束。