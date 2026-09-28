# gpx2stl

Convert GPX tracks and routes into printable terrain models:

- **3MF by default:** two named parts and two materials for Bambu Studio. Filament 1 is the GPX route; filament 2 is the terrain/base.
- **STL on request:** one watertight mesh containing the route and terrain/base.
- **Flexible topography:** use local GeoTIFF files first or download SRTMGL1/COP30 data from OpenTopography.
- **Square or circular base:** automatically sized around every track segment and GPX route in the input file.

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

At startup, the command searches from the current directory upward for `settings.json`. Values in that file become defaults; explicit command-line arguments override them. Relative paths are resolved from the directory containing `settings.json`.

Copy `settings.example.json` to `settings.json` and edit the values you want:

```bash
cp settings.example.json settings.json
```

JSON keys use the Python/long-option names with underscores, such as `route_width`, `boundary_percent`, `topo_source`, and `topo_dir`. The positional input can also be defaulted with `gpx_file`. Use `use_3mf` for the `--3mf` / `--no-3mf` setting. Unknown keys, invalid JSON, or incorrect value types produce an explicit error. `settings.json` is ignored by Git; `settings.example.json` is tracked as a complete template.

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

This writes `route.3mf` beside `route.gpx`. Examples:

```bash
# Circular, two-color 3MF with terrain
python -m gpx2stl route.gpx --shape circle --max-size 180

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
| `gpx_file` | required | Input GPX file. All nonempty tracks, segments, and route elements are included. |
| `-o`, `--output` | input stem | Output path. The suffix must match the selected format. |
| `--route-width` | `1` mm | Printed route ribbon width. |
| `--route-height` | `2` mm | Route height above terrain in topo mode. |
| `--topo`, `--no-topo` | topo | Enable or disable terrain. |
| `--boundary-percent` | `10` | Padding on every square side or added to the minimum-circle radius. |
| `--shape` | `square` | `square` or `circle`. Squares remain north-up. |
| `--3mf`, `--no-3mf` | 3MF | Select two-material 3MF or single-mesh STL. |
| `--max-size` | `200` mm | Maximum final X/Y dimension. |
| `--terrain-height` | `20` mm | Normalized min-to-max terrain relief. |
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
- Terrain elevations are normalized into the configured `--terrain-height` range.
- Without topo, GPX altitude controls the route top at physical 1:20,000 vertical scale: 1,000 m becomes 50 mm. Internal missing elevations are interpolated; missing endpoint/all elevations are errors.
- Square output is the smallest north-up square around the route before padding. Circle output uses the true minimum enclosing circle before padding.
- Routes crossing the ±180° antimeridian are supported by split DEM requests.

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

Import the generated 3MF as one object with multiple parts if prompted. The model contains:

1. `GPX route` / `Filament 1 - Route`
2. `Topography` / `Filament 2 - Topography`

Confirm or remap those two parts to the desired AMS/filament slots before slicing.

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

`gpx2stl` 可将 GPX 轨迹和路线转换为适合 3D 打印的地形模型：

- **默认输出 3MF：**包含两个命名部件和两种材料，可导入 Bambu Studio。耗材 1 用于 GPX 路线，耗材 2 用于地形/底座。
- **可选输出 STL：**路线与地形/底座合并为一个水密网格。
- **灵活的真实地形：**优先使用本地 GeoTIFF，或通过 OpenTopography 下载 SRTMGL1/COP30 高程数据。
- **方形或圆形底座：**根据输入文件中的全部轨迹段和 GPX 路线自动确定范围。

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

程序启动时会从当前目录向上查找 `settings.json`。文件中的值会成为默认参数，命令行中显式传入的参数优先级更高。相对路径以 `settings.json` 所在目录为基准解析。

复制完整模板后按需修改：

```bash
cp settings.example.json settings.json
```

JSON 键使用 Python/长参数对应的下划线名称，例如 `route_width`、`boundary_percent`、`topo_source` 和 `topo_dir`。也可用 `gpx_file` 设置默认输入文件；`--3mf` / `--no-3mf` 对应 `use_3mf`。未知键、无效 JSON 或错误的数据类型都会产生明确错误。`settings.json` 已被 Git 忽略，而完整模板 `settings.example.json` 会纳入版本控制。

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

默认在 GPX 文件旁生成 `route.3mf`。示例：

```bash
# 带地形的圆形双色 3MF
python -m gpx2stl route.gpx --shape circle --max-size 180

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
| `gpx_file` | 必填 | 输入 GPX 文件；包含所有非空轨迹、轨迹段和路线元素。 |
| `-o`, `--output` | 输入文件名 | 输出路径；扩展名必须与格式一致。 |
| `--route-width` | `1` mm | 打印路线带宽度。 |
| `--route-height` | `2` mm | 启用地形时路线高出地形的高度。 |
| `--topo`, `--no-topo` | 启用 | 启用或禁用地形。 |
| `--boundary-percent` | `10` | 方形每边或最小包围圆半径增加的百分比。 |
| `--shape` | `square` | `square`（方形）或 `circle`（圆形）；方形保持正北朝上。 |
| `--3mf`, `--no-3mf` | 3MF | 选择双色 3MF 或单网格 STL。 |
| `--max-size` | `200` mm | 最终模型 X/Y 最大尺寸。 |
| `--terrain-height` | `20` mm | 地形最低点到最高点的归一化高度差。 |
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
- DEM 地形高程归一化到 `--terrain-height` 指定的范围。
- 禁用地形时，路线顶部采用 GPX 高程和真实的 1:20,000 垂直比例：1,000 m 对应 50 mm。内部缺失高程会插值；端点或全部高程缺失会报错。
- 方形为加边界前包围路线的最小正北方形；圆形为加边界前的真实最小包围圆。
- 支持跨越 ±180° 日期变更线的路线；此时会拆分 DEM 请求。

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

导入生成的 3MF 时，如有提示请选择作为“一个对象的多个部件”载入。文件包含：

1. `GPX route` / `Filament 1 - Route`
2. `Topography` / `Filament 2 - Topography`

切片前请确认这两个部件分别映射到正确的 AMS/耗材槽位。

## 开发与测试

```bash
uv sync --extra dev
source .venv/bin/activate
python -m pytest
```

自动化测试使用合成 GPX 和 GeoTIFF 数据，不需要联网或真实 API Key。

## 许可证

本项目采用 [MIT License](LICENSE)。地形数据集仍受其各自的许可证与署名要求约束。