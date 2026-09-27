# gpx2stl

Convert GPX tracks and routes into printable terrain models:

- **3MF by default:** two named parts and two materials for Bambu Studio. Filament 1 is the GPX route; filament 2 is the terrain/base.
- **STL on request:** one watertight mesh containing the route and terrain/base.
- **Optional topography:** downloads SRTMGL1 or COP30 elevation data from OpenTopography.
- **Square or circular base:** automatically sized around every track segment and GPX route in the input file.

[中文说明](#中文说明)

## Requirements

- Python 3.11 or newer
- An [OpenTopography API key](https://portal.opentopography.org/myopentopo) when topo is enabled

## Installation

With `uv`:

```powershell
uv sync
```

Or with `pip`:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
```

## API key

Copy `.env.example` to `.env` in the repository/current project root and replace the placeholder:

```dotenv
OPENTOPOGRAPHY_API_KEY=your-key
```

The command searches for `.env` from the current directory upward. An existing process environment variable takes precedence over `.env`, and `--api-key` takes precedence over both. `.env` is ignored by Git.

## Usage

```powershell
.\.venv\Scripts\gpx2stl.exe route.gpx
```

This writes `route.3mf` beside `route.gpx`. Examples:

```powershell
# Circular, two-color 3MF with terrain
gpx2stl route.gpx --shape circle --max-size 180

# Single-mesh STL with terrain
gpx2stl route.gpx --no-3mf -o route.stl

# No network/topography; use GPX elevation for route height
gpx2stl route.gpx --no-topo --no-3mf

# Override automatic DEM selection
gpx2stl route.gpx --dem-type COP30 --force
```

### Options

| Option | Default | Description |
|---|---:|---|
| `gpx_file` | required | Input GPX file. All nonempty tracks, segments, and route elements are included. |
| `-o`, `--output` | input stem | Output path. The suffix must match the selected format. |
| `--route-width` | `1` mm | Printed route ribbon width. |
| `--route-height` | `2` mm | Route height above terrain in topo mode. |
| `--topo`, `--no-topo` | topo | Enable or disable downloaded terrain. |
| `--boundary-percent` | `10` | Padding on every square side or added to the minimum-circle radius. |
| `--shape` | `square` | `square` or `circle`. Squares remain north-up. |
| `--3mf`, `--no-3mf` | 3MF | Select two-material 3MF or single-mesh STL. |
| `--max-size` | `200` mm | Maximum final X/Y dimension. |
| `--terrain-height` | `20` mm | Normalized min-to-max terrain relief. |
| `--base-height` | `2` mm | Solid base thickness. |
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

## OpenTopography behavior

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

```powershell
uv sync --extra dev
.\.venv\Scripts\python.exe -m pytest
```

Tests use synthetic GPX and GeoTIFF data and do not require network access or a real API key.

---

# 中文说明

`gpx2stl` 可将 GPX 轨迹和路线转换为适合 3D 打印的地形模型：

- **默认输出 3MF：**包含两个命名部件和两种材料，可导入 Bambu Studio。耗材 1 用于 GPX 路线，耗材 2 用于地形/底座。
- **可选输出 STL：**路线与地形/底座合并为一个水密网格。
- **可选真实地形：**通过 OpenTopography 下载 SRTMGL1 或 COP30 高程数据。
- **方形或圆形底座：**根据输入文件中的全部轨迹段和 GPX 路线自动确定范围。

## 环境要求

- Python 3.11 或更高版本
- 启用地形时需要 [OpenTopography API Key](https://portal.opentopography.org/myopentopo)

## 安装

使用 `uv`：

```powershell
uv sync
```

或使用 `pip`：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
```

## 配置 API Key

将 `.env.example` 复制为仓库（或当前项目）根目录下的 `.env`，并替换占位值：

```dotenv
OPENTOPOGRAPHY_API_KEY=你的API密钥
```

命令会从当前目录开始向上查找 `.env`。系统环境变量会覆盖 `.env` 中的值，而 `--api-key` 的优先级最高。`.env` 已加入 Git 忽略列表。

## 使用方法

```powershell
.\.venv\Scripts\gpx2stl.exe route.gpx
```

默认在 GPX 文件旁生成 `route.3mf`。示例：

```powershell
# 带地形的圆形双色 3MF
gpx2stl route.gpx --shape circle --max-size 180

# 带地形的单网格 STL
gpx2stl route.gpx --no-3mf -o route.stl

# 不联网、不生成地形；使用 GPX 高程生成路线高度
gpx2stl route.gpx --no-topo --no-3mf

# 手动指定 DEM 数据集
gpx2stl route.gpx --dem-type COP30 --force
```

### 参数

| 参数 | 默认值 | 说明 |
|---|---:|---|
| `gpx_file` | 必填 | 输入 GPX 文件；包含所有非空轨迹、轨迹段和路线元素。 |
| `-o`, `--output` | 输入文件名 | 输出路径；扩展名必须与格式一致。 |
| `--route-width` | `1` mm | 打印路线带宽度。 |
| `--route-height` | `2` mm | 启用地形时路线高出地形的高度。 |
| `--topo`, `--no-topo` | 启用 | 启用或禁用下载地形。 |
| `--boundary-percent` | `10` | 方形每边或最小包围圆半径增加的百分比。 |
| `--shape` | `square` | `square`（方形）或 `circle`（圆形）；方形保持正北朝上。 |
| `--3mf`, `--no-3mf` | 3MF | 选择双色 3MF 或单网格 STL。 |
| `--max-size` | `200` mm | 最终模型 X/Y 最大尺寸。 |
| `--terrain-height` | `20` mm | 地形最低点到最高点的归一化高度差。 |
| `--base-height` | `2` mm | 实体底座厚度。 |
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

## OpenTopography 规则

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

```powershell
uv sync --extra dev
.\.venv\Scripts\python.exe -m pytest
```

自动化测试使用合成 GPX 和 GeoTIFF 数据，不需要联网或真实 API Key。