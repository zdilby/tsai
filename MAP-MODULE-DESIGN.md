# 地图模块（`/map/`）设计方案

> **状态：已落地实现。** 本文档为原始设计稿，保留供追溯设计意图；实际实现和最新细节以 `ARCHITECTURE.md`「十六、地图模块」章节为准，两者如有出入以 ARCHITECTURE.md 为准。
>
> ~~状态：设计定稿，未落地（尚未改任何代码）。~~（原始状态说明，已过期）
> 参考：MapStage（本机 `/Users/tianshu/Srv/Github/MapStage`）及其 Demo `https://hopechen067.github.io/MapStage/`。
> 目标：在 TSAI 内新增与「对话 / 写作 / 作图」并列的第四个模块「地图」，做到 Demo 的**看图 / 调参 / 存预设**程度，**外加** TSAI 自研的**点 / 线 / 信息框标注编辑器**。**不含** HyperFrames → MP4。

---

## 0. 结论速览（第三方调用 & AI 调用）

| 维度 | 结论 |
|---|---|
| **第三方服务** | 全部在**用户浏览器端**直连，**不经 TSAI 服务器代理、无任何 API key**。四类运行时依赖：① MapLibre GL JS 引擎（本地 vendor，用 MapStage 的 5.6.0 patched 构建）② 卫星栅格瓦片 EOX（`tiles.maps.eox.at`）③ 地形 DEM 瓦片 Mapterhorn（`tiles.mapterhorn.com`）④ 矢量瓦片 + glyphs 字体 OpenFreeMap（`tiles.openfreemap.org`）。另 vendor 一批 MapStage 的「拆出」相关 JS/数据文件（约 2 MB，随代码走，不算运行时第三方请求）。 |
| **AI / 大模型** | **全模块零 AI 调用。** 地图渲染、调参、放点连线写信息框，全部是确定性客户端逻辑 + JSON 持久化。不接 Gemini / Codex。 |
| **服务器新增** | 纯 TSAI 内部：新权限 `can_map`、新路由文件 `map.py`、新表 `map_documents` + `map_preset_versions`、`settings.py` 加几个瓦片端点常量、admin 加授权开关。不落盘图片、不调第三方、不调 AI。 |
| **网络** | 按国际网络环境，**不做镜像源 / `.env` 端点配置**。个别用户网络问题 → 前端友好兜底提示 + 只禁用受影响的功能（见 §12）。 |

---

## 1. 范围

### 做
- 一张可交互 MapLibre 地图：卫星底图 + 山影 / 3D 地形（DEM）+ 矢量水系 + 古卷 CSS 滤镜 + 地图/地球投影切换 + **区域拆出（region isolate）**。
- 右侧设置面板分 **两个 Tab**：
  - **Tab A「地图效果」**：相机 / 卫星层 / 山影 / 水系颜色 / CSS 滤镜 / 背景 / 投影 / 拆出 / 复制·下载·粘贴 JSON —— 对齐 Demo。
  - **Tab B「点 / 线 / 标注」**：TSAI 自研编辑器——放点位、点位信息框、点位之间连线（**支持有向箭头**）。
- 左侧边栏：地图列表 + 「新建地图」；每张地图 = 一条命名预设（含样式 + 标注），存 DB，可反复打开继续编辑；支持多张。
- **预设版本历史**：实时自动保存 + 检查点快照，留最近 3 版，可回滚。

### 不做（本期）
- HyperFrames / 逐帧渲染 / 导出 MP4 / 相机路径录制。
- 自由多边形 / 区域涂色 / 手绘曲线标注（只做点、线、信息框）。
- 3D 立体城池模型（HanCity3D / Three.js）——点位用 HTML Marker + CSS 图形。
- 任何 AI 生成能力（"一句话生成预设 / 行程文字自动生成点线"等）。

---

## 2. 第三方服务调用清单

> 全部由**最终用户浏览器**直连。TSAI 后端只发地图页面本身。服务器 `.env` 里的 HTTP 代理（`127.0.0.1:10801`）对这些请求**无效**（那是服务器端 httpx 用的）。按需求：**不配置镜像源，不写进 `.env`**，端点作为 `settings.py` 常量集中管理。

| # | 依赖 | 端点 / 位置 | 用途 | Key | 许可 / 注意 |
|---|---|---|---|---|---|
| 1 | **MapLibre GL JS + CSS**（MapStage 的 **patched 5.6.0** 构建，含 `__ANTIQUE_TERRAIN_CLIP_PATCH`） | 本地 vendor 到 `static/js/maplibre/`（拷 `MapStage/mapstage/tuner/vendor/maplibre-gl.js` + `maplibre-gl.css`） | 地图引擎 + 拆出所需的地形裁剪补丁 | 无 | BSD-3。**整个 `/map/` 页锁死用这一份**，不能混官方新版。 |
| 2 | **卫星栅格瓦片** | `https://tiles.maps.eox.at/wmts/1.0.0/s2cloudless-2020_3857/default/g/{z}/{y}/{x}.jpg`（模板是 `{z}/{y}/{x}`） | 卫星底图 raster source | 无 | EOX Sentinel-2 cloudless，非商用 + 需署名，maxzoom ~14。 |
| 3 | **地形 DEM 瓦片** | `https://tiles.mapterhorn.com/{z}/{x}/{y}.webp` | 山影 hillshade + `setTerrain` 3D 地形 | 无 | Mapterhorn，**`encoding: 'terrarium'` 必填**，无 SLA，需署名。 |
| 4 | **矢量瓦片 + glyphs 字体** | TileJSON `https://tiles.openfreemap.org/planet`；glyphs `https://tiles.openfreemap.org/fonts/{fontstack}/{range}.pbf`（落地时按 OpenFreeMap 当时的 style JSON 实测确认字体栈名） | 水系几何、地名标注、**有向箭头字形** | 无 | OpenFreeMap / OpenMapTiles，需 **OSM 署名**。 |
| 5 | **拆出（region isolate）整包** | 从 MapStage vendor 到 `static/js/maplibre/mapstage-isolate/`：`terrain-island.js`(2313)、`region-isolate.js`(1049)、`region-isolate-data.js`(1.8 MB 轮廓)、`isolate-workbench.js`(732)、`map-fx.js`(相关部分)、`polar-ice.geojson`(37 KB) | 把地形裁剪到一国轮廓 + 沿边缘挤出侧壁 | 无 | 代码 MIT；轮廓数据 Natural Earth 公有领域 + OSM 抽取 ODbL（需署名）。**原样 vendor，不改动**；在 `static/js/maplibre/mapstage-isolate/NOTICE.md` 记录来源与许可。 |
| 6 | *(不用)* Three.js / Google Fonts | — | MapStage 的 3D 城池 / 装饰字体 | — | 本期不引入。 |

**署名（许可硬性）**：地图上必须有 MapLibre attribution 控件，含 `Sentinel-2 cloudless © EOX`、`© Mapterhorn`、`© OpenStreetMap contributors, OpenMapTiles`；拆出开启时另注 `Natural Earth / © OpenStreetMap (ODbL)`。

**配额**：EOX / Mapterhorn / OpenFreeMap 均为免费公共端点（非商用 / 无 SLA），内部低频工具够用。

---

## 3. AI / 大模型调用

**零。** 全模块无 LLM 调用：MapStage 本身没有任何 AI，TSAI 地图模块的看图、调参、放点连线、信息框全是客户端确定性逻辑。不引入 `settings.client`（Gemini）的任何用法。

---

## 4. 需求 1：模块切换按钮 → 合并为下拉菜单

### 现状
`chat.html` / `writing.html` / `drawing.html` 侧栏底部各有一组"跳其他模块"按钮，按 `{% if can_write %}` / `{% if can_draw %}` 显隐。4 模块后每页要 3 个跳转入口，横排挤。

### 方案
新增 Jinja 片段 **`templates/_module_switch.html`**，用 Materialize `M.Dropdown`（各页已加载 materialize.min.js）：

```html
{# 参数：current ∈ {chat,writing,drawing,map}；can_write / can_draw / can_map；dd_id 唯一 #}
<a class="btn dropdown-trigger module-switch-btn" data-target="{{ dd_id }}" href="#!">
  <span class="btn-label-sm">切换模块</span><i class="material-icons right">arrow_drop_down</i>
</a>
<ul id="{{ dd_id }}" class="dropdown-content">
  {% if current != 'chat' %}                <li><a href="/">对话</a></li>{% endif %}
  {% if current != 'writing' and can_write %}<li><a href="/writing/">写作</a></li>{% endif %}
  {% if current != 'drawing' and can_draw %} <li><a href="/drawing/">作图</a></li>{% endif %}
  {% if current != 'map' and can_map %}      <li><a href="/map/">地图</a></li>{% endif %}
</ul>
```

- 各页 DOMContentLoaded 里 `M.Dropdown.init(...)`。
- 「新建X」按钮保留原样，不进下拉；下拉只收纳"去别的模块"。
- `#slide-out`（移动端）和桌面 `.X-sidebar-actions` 两处各 include 一次，`dd_id` 不同（`module-dd-mobile` / `module-dd-desktop`），避免 Materialize 实例冲突。
- `.module-switch-btn` / `.dropdown-content` 配色在 `style.css` 里给一套项目一致的靛蓝系，不引 MapStage 样式。

### 连带改动：所有模块页都要拿到 `can_map`
- `account.get_user` 是 `SELECT * FROM users` → 加 `can_map` 列后自动带出。
- `main.py:index()` 模板 context 现在显式传 `can_write / can_draw` → **补 `can_map`**。
- `writing.py:writing_page` / `drawing.py:drawing_page` / `drawing_style_page` → 改成都传 `can_write / can_draw / can_map`。
- 新 `map.py` 页面路由同样传三者。
- 4 个模板底部按钮区替换为 `{% include '_module_switch.html' %}`（传对应 `current`）。

---

## 5. 后端设计

### 5.1 权限
- `users.can_map BOOLEAN NOT NULL DEFAULT FALSE`（`init_map_tables()` 里 `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`）。
- `map.py:require_map_access(request, user)` —— **逐字镜像** `drawing.py:require_draw_access`：`is_admin` 或 `can_map` 放行；HTML 页面路由无权限 302 回 `/`，API 403。

### 5.2 路由（`map.py`，`app.include_router(map_router, prefix="/map")`）

| 方法 | 路径 | 功能 |
|---|---|---|
| `GET` | `/map/` | HTML：有地图则 302 到最新一张 `/map/{id}`，无则空态（镜像 `drawing_page`） |
| `GET` | `/map/tile-config` | JSON：把 `settings` 里的瓦片端点 + 署名下发给前端（唯一"配置类"接口，不碰第三方） |
| `POST` | `/map/documents` | 新建地图 `{name?}` → `{id, name}` |
| `GET` | `/map/documents` | 当前用户地图列表 `[{id,name,updated_at,thumb_url}]` |
| `GET` | `/map/documents/{id}` | 单张地图完整信息（含 `preset`） |
| `PATCH` | `/map/documents/{id}` | 实时保存 `{name?, preset?}`（防抖，只发差异；原地更新 `map_documents.preset`，**不入版本表**） |
| `DELETE` | `/map/documents/{id}` | 删除（含尽力删缩略图文件） |
| `GET` | `/map/documents/{id}/versions` | 版本列表 `[{version, created_at, note?}]` |
| `POST` | `/map/documents/{id}/versions` | 立即把当前 `preset` 存一个快照（检查点触发或手动「存快照」按钮），超 3 版删最旧 |
| `POST` | `/map/documents/{id}/versions/{version}/restore` | 把该版 `preset` 拷回 `map_documents.preset` |
| `POST` | `/map/documents/{id}/thumb` | *(可选)* 前端 `canvas.toDataURL()` 截图 → `static/maps/{username}/{id}.png`，侧栏预览 |
| `GET` | `/map/{map_id}` | HTML 地图页（**声明在文件最后**，避免吃掉 `/documents`、`/tile-config`；同 `drawing.py:/{style_id}` 的坑） |

镜像 `drawing.py` 的 `_serialize_row` / `_ensure_style_owner` → `_ensure_map_owner` / `map_document_owned_by`。

### 5.3 数据库（`backend/db.py:init_map_tables()`，startup 里调用）

```sql
ALTER TABLE users ADD COLUMN IF NOT EXISTS can_map BOOLEAN NOT NULL DEFAULT FALSE;

CREATE TABLE IF NOT EXISTS map_documents (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name        TEXT NOT NULL DEFAULT '未命名地图',
  preset      JSONB NOT NULL DEFAULT '{}',    -- 见 §6：{ style:{…}, annotations:{points,links} }
  thumb_path  TEXT DEFAULT '',
  created_at  TIMESTAMPTZ DEFAULT NOW(),
  updated_at  TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_map_documents_user_id ON map_documents(user_id);

CREATE TABLE IF NOT EXISTS map_preset_versions (
  id          SERIAL PRIMARY KEY,
  map_id      UUID NOT NULL REFERENCES map_documents(id) ON DELETE CASCADE,
  preset      JSONB NOT NULL,
  version     INTEGER NOT NULL,
  note        TEXT DEFAULT '',                -- 'checkpoint' | 'manual' | 'open-diff'
  created_at  TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_map_preset_versions_map ON map_preset_versions(map_id, version DESC);
```

- 一张地图 = `map_documents` 一行；`preset` 单列 JSONB，实时自动保存**原地 PATCH**。
- 不落盘图片、不做向量。DB 函数照 `drawing_*` / `writing_contents` 抄：`create_map_document / get_map_documents / get_map_document / map_document_owned_by / update_map_document / delete_map_document / list_map_versions / snapshot_map_version / restore_map_version`。

### 5.4 `settings.py` 新增（端点常量，不进 `.env`）

```python
MAP_SATELLITE_TILES = "https://tiles.maps.eox.at/wmts/1.0.0/s2cloudless-2020_3857/default/g/{z}/{y}/{x}.jpg"
MAP_SATELLITE_ATTR  = "Sentinel-2 cloudless © EOX"
MAP_TERRAIN_TILES   = "https://tiles.mapterhorn.com/{z}/{x}/{y}.webp"
MAP_TERRAIN_ENCODING= "terrarium"
MAP_TERRAIN_ATTR    = "© Mapterhorn"
MAP_VECTOR_TILEJSON = "https://tiles.openfreemap.org/planet"
MAP_VECTOR_GLYPHS   = "https://tiles.openfreemap.org/fonts/{fontstack}/{range}.pbf"  # 落地时实测确认
MAP_VECTOR_ATTR     = "© OpenStreetMap contributors, OpenMapTiles"
```
`GET /map/tile-config` 直接拼这些成 JSON 返回。

### 5.5 admin
- `POST /admin/user/{id}/set_map`（镜像 `set_draw`）+ `db.update_user_map_permission`。
- `db.get_all_users_with_stats` 的 SELECT 加 `u.can_map`。
- `templates/admin/users.html` 加「开地图 / 撤地图」按钮 + `toggleMapPermission` JS（照 `toggleDrawingPermission` 抄）。

---

## 6. `preset` 结构（version 3）

样式（Tab A）和标注（Tab B）同存一个 JSON。一次快照同时覆盖两者，回滚一步到位。

```jsonc
{
  "id": "…", "label": "…", "version": 3,

  "style": {                              // ── Tab A：与 MapStage schema 对齐 ──
    "view": "map",                        // "map" | "globe"
    "camera": { "center": [104.0, 35.5], "zoom": 4.2, "pitch": 28, "bearing": 0 },
    "basemap": { "satellite": true, "relief": false,
                 "isolate": false, "isolateRegion": "china" },
    "mapstage": {
      "backgroundColor": "#c8c2b4", "terrainExaggeration": 1,
      "satellite": { "opacity":0.7,"saturation":-0.08,"contrast":0.02,
                     "brightnessMin":0.08,"brightnessMax":0.96,"hueRotate":12 },
      "hillshade": { "exaggeration":0.42,"illuminationDirection":315,
                     "shadowColor":"#2c2824","highlightColor":"#f0ece4","accentColor":"#6a6458" },
      "water":     { "lakeFill":"…","lakeOutline":"…",
                     "riverLevel3":"…","riverLevel2":"…","riverLevel1":"…","highlightRiver":"…",
                     "riverL3Width":1.2,"riverL2Width":2,"riverL1Width":1.8,"highlightWidth":2.4 }
    },
    "css": { "sepia":0.08,"saturate":0.98,"contrast":1.03,"brightness":1.0,"hueRotate":-1,
             "warmTintAlpha":0.04,"warmTintColor":"#9a8868","vignetteStrength":0.1 },
    "ui":  { "showWater":true,"showHighlight":true }
  },

  "annotations": {                        // ── Tab B：TSAI 自研 ──
    "points": [{
      "id": "p_k3f9",
      "name": "武威",
      "lng": 102.6, "lat": 37.9,
      "tier": "commandery",               // capital 都城 | commandery 郡城 | city 城池 | pass 关隘 | station 驿站 | custom
      "shape": "square",                  // square 方城 | circle | diamond 驿站 | gate 关隘 | star 都城（默认按 tier）
      "size": 1.0,
      "markerColor": "#b8442e",           // 默认按 tier 取色
      "label": { "show": true, "color": "#2c2824" },
      "callout": {                        // 信息显示框，可选
        "show": true,                     // 默认展开 / 收起
        "text": "河西四郡之一……",
        "bg": "#f4ecd8", "fg": "#3a2a18", "border": "#b89562",
        "anchor": "top", "maxWidth": 220
      }
    }],
    "links": [{
      "id": "l_9m2a",
      "from": "p_k3f9", "to": "p_x7qd",   // 引用 point id；也允许自由端点 fromLngLat / toLngLat
      "directed": true,                   // 有向（画箭头）| 无向
      "color": "#2a6a78",
      "width": 2.0,
      "dash": false,                      // 实线 | 虚线
      "curve": "geodesic"                 // "straight" 直线 | "geodesic" 大圆弧
    }]
  }
}
```

- 与 MapStage Demo 的「复制 JSON」在 `style` 部分同构 → 在 Demo 调好的样式可直接粘进 TSAI（`annotations` 部分 Demo 没有，忽略即可）。
- `tier` 内置 5 种 + `custom`；每种给默认色 + 默认形状，用户可逐点覆盖。

---

## 7. 前端页面结构（`/map/`）

镜像 `drawing.html`，用项目现有 Materialize + `style.css` 风格，**不引 MapStage 的 CSS / HTML / JS 结构**（拆出整包除外，见 §11）。

```
body (flex row, ≥993px)
├── #slide-out           移动端滑出侧栏：TSAI 品牌 + 新建地图 + {% include _module_switch %}
├── #map-sidebar         桌面常驻左栏 flex:0 0 260px
│   ├── .map-sidebar-brand "TSAI"
│   ├── #map-list         地图列表（缩略图/名字 → /map/{id}）
│   └── .map-sidebar-actions  新建地图 + 「切换模块」下拉
└── .map-shell           flex:1
    ├── <nav> 顶栏        与 chat/writing/drawing 完全一致
    └── .map-layout (grid: 1fr 340px)
        ├── #map-main      #maplibre-map（铺满）+ 左上浮层工具条（地图/地球、相机复位、拆出快捷、复制/下载/粘贴 JSON、存快照）
        │                  + 右下 attribution 控件
        └── #map-settings  右侧面板，顶部 2-Tab 切换
            ├── #tab-style   Tab A（§8）
            └── #tab-annot   Tab B（§9）
```

响应式与 writing/drawing 一致：≤992px 隐藏 `#map-sidebar` 与 `#map-settings`，单列，汉堡唤出。

Tab 切换用手写 2 段按钮（风格同 writing 的预览/编辑切换），不用 `M.Tabs`。

新增文件：`templates/map.html`、`static/css/map.css`、`static/js/map.js`；vendor `static/js/maplibre/`（patched maplibre + isolate 整包）。

---

## 8. Tab A「地图效果」设置项（对齐 Demo）

| 分组 | 控件（data-key，写进 `preset.style`） |
|---|---|
| 视图 | 地图/地球 `style.view`；相机复位到中国；`style.camera.zoom/pitch/bearing/center`（随地图 `moveend` 实时回写） |
| 图层开关 | 卫星 `style.basemap.satellite`、海拔设色 `style.basemap.relief`、水系 `style.ui.showWater`、高亮水系 `style.ui.showHighlight`、**拆出 `style.basemap.isolate` + 区域下拉 `style.basemap.isolateRegion`** |
| 卫星层 | `style.mapstage.satellite.opacity/saturation/contrast/brightnessMin/brightnessMax/hueRotate` |
| 山影 | `style.mapstage.hillshade.exaggeration/illuminationDirection` + `shadowColor/highlightColor/accentColor`（颜色选择器）；`style.mapstage.terrainExaggeration` |
| 水系颜色 | `style.mapstage.water.*`（湖填充/描边、三级河颜色+线宽、高亮河） |
| CSS 古卷滤镜 | `style.css.sepia/saturate/contrast/brightness/hueRotate/warmTintAlpha/warmTintColor/vignetteStrength`；总开关 |
| 背景 | `style.mapstage.backgroundColor` |
| 预设 | 复制 JSON / 下载 JSON / 粘贴 JSON / 重置为默认 / 存快照 |

滑块/颜色输入沿用 `drawing.css` 的 `.setting-item` 既有样式。

---

## 9. Tab B「点 / 线 / 信息框」编辑器（TSAI 自研）

### 9.1 渲染（全部标准 MapLibre 图元，无额外库）

- **连线**：一个 `line` 图层吃一份 LineString FeatureCollection；`line-color` / `line-width` / `line-dasharray` 用 `["get", …]` 从要素属性取 → 一层画所有线。
  - `curve:"geodesic"` 时前端算大圆弧折线（内联 great-circle 插值，~30 段），`"straight"` 则两点直连。
  - **有向箭头**：叠一个 `symbol` 图层，`symbol-placement:"line"`，`text-field:"➤"`，`text-rotation-alignment:"map"`，`symbol-spacing` 调大使靠近终点出现 1 个箭头（依赖 glyphs 字体）。无向链接不加。
- **点位 + 信息框**：每点一个 `maplibregl.Marker`（CSS 画的方 / 圆 / 菱 / 梯形 / 星，填充 = `markerColor`，描边墨色）；下方文字 `<span>`（色 = `label.color`）。
  - **信息框**：同一 Marker 内的子元素——羊皮纸小卡片（`bg` / `fg` / `border`）+ 一根 leader 线指向点；逐点收起/展开；另有全局「全部展开 / 悬停显示 / 全部收起」。
  - 用 HTML Marker 而非 GeoJSON symbol：自定义形状、描边、卡片、拖拽都靠 CSS + DOM 事件；几百个点仍 OK（MapStage 城池标签也是这么挂的）。
- **选中态**：选中的点/线加高亮描边；右栏 Tab B 属性区显示其字段。
- 图层顺序：连线在点位之下；点位/信息框是 DOM Marker，天然在地图画布之上。

### 9.2 编辑交互

**工具条**（4 模式）：`选择` / `加点` / `连线` / `删除`
- 加点：地图点一下 → 在该经纬度建点（默认 `tier=city`）→ 打开其属性编辑。
- 连线：点 A → 点 B → 建连线 → 打开其属性编辑。（端点必须是已存在的点位；本期不做自由端点 UI，`fromLngLat/toLngLat` 仅数据结构预留。）
- 选择：点标记 / 线选中；拖动标记改经纬度，**拖拽结束**防抖保存。
- 删除：点标记 / 线 → 确认后删；删点时级联删除挂在它上面的连线。

**属性编辑区**（随选中对象切换）
- 点：名称 / 类型下拉（都城·郡城·城池·关隘·驿站·自定义）/ 点位颜色 / 形状 / 大小 / 标注开关 + 文字色 / 信息框（开关·文字·背景色·文字色·边框色·方位·默认展开）
- 线：起点·终点（只读名）/ 有向·无向 / 颜色 / 线宽 / 实线·虚线 / 直线·弧线

**列表区**：所有点 + 所有线，点击定位并选中，行内删除。

不引 `maplibre-gl-draw`——点击建点 + 点选连线 + Marker 拖拽用原生事件即可。

### 9.3 保存
Tab B 的每次改动写入内存 `preset.annotations`，与 Tab A 共用同一条防抖 `PATCH /map/documents/{id}`（~800 ms）。

---

## 10. 版本历史（做法 2：实时自动保存 + 检查点快照）

- **`map_documents.preset`**：始终是最新，防抖 PATCH 原地更新，**不进版本表**。
- **`map_preset_versions`**：只在"有意义的检查点"写快照，超 3 版删最旧（`ORDER BY version ASC LIMIT 1`）：
  1. **打开文档时**：若当前 `preset` 与最新快照有差异，先补存一版（`note='open-diff'`）——保证"上次编辑前的样子"有留存。
  2. **周期**：编辑过程中每 ~2 分钟若 `preset` 相比最后一版有变化，存一版（`note='checkpoint'`）。
  3. **手动**：工具条「存快照」按钮（`note='manual'`）。
- **回滚**：Tab A 预设区 / 一个「历史」弹窗列出 3 版（时间 + note），选一版 → `POST …/versions/{v}/restore` 把该版 `preset` 拷回 `map_documents.preset` → 前端重载。
- 版本号单调递增；删最旧只删行不回收号。

---

## 11. 拆出（region isolate）—— vendor MapStage 整包

拆出需要 MapStage 打过补丁的 MapLibre（地形裁剪 `__ANTIQUE_TERRAIN_CLIP_PATCH`），无法用官方版实现，因此**这是本模块唯一直接复用 MapStage 代码的地方**：

- **vendor（原样，不改）** 到 `static/js/maplibre/`：
  - `maplibre-gl.js`（patched 5.6.0）+ `maplibre-gl.css` —— `/map/` 整页用这一份。
  - `mapstage-isolate/`：`terrain-island.js`、`region-isolate.js`、`region-isolate-data.js`、`isolate-workbench.js`、`polar-ice.geojson`，以及 `map-fx.js` 里 isolate 依赖的部分（评估后可能整拷 `map-fx.js`）。
- `static/js/maplibre/mapstage-isolate/NOTICE.md` 记录来源 commit、许可（代码 MIT / 数据 Natural Earth 公有领域 + OSM ODbL）与署名要求。
- `map.js` 在需要时按 MapStage 的调用约定初始化 isolate；Tab A 的「拆出」开关 + 区域 `<select>`（选项从 `REGION_ISOLATE_DATA` 读）驱动 `style.basemap.isolate` / `isolateRegion`。
- 升级 MapLibre 时要连带重新评估这份补丁（记进 `PRODUCTION.md` / `ARCHITECTURE.md`）。

---

## 12. 第三方加载失败的前端兜底

统一 `map.on('error', e => …)` + 引擎 `<script onerror>`，按 `e.sourceId` / 失败类型映射到"友好提示 + 只禁用受影响功能"：

| 失败项 | 处理 |
|---|---|
| MapLibre 引擎脚本 | 整个 `#map-main` 显示：**「地图引擎（MapLibre）因当前网络环境问题无法加载，地图功能暂不可用」**，右栏禁用 |
| 卫星底图（EOX）source error | toast + 自动关「卫星」开关：**「卫星底图服务（EOX）因当前网络环境问题无法访问，当前功能暂不可用」** |
| 地形（Mapterhorn）source error | 自动关山影 / 3D 地形 / 拆出：**「地形服务（Mapterhorn）因当前网络环境问题无法访问，当前功能暂不可用」** |
| 矢量瓦片（OpenFreeMap）source error | 自动关水系 / 地名：**「矢量瓦片服务（OpenFreeMap）因当前网络环境问题无法访问，当前功能暂不可用」** |
| glyphs 字体加载失败 | 地名标注 + 有向箭头字形退化为不显示文字（线本身仍在），一次性 toast 说明 |

提示文案统一模板：**「XXX 服务因当前网络环境问题无法使用 / 访问，当前功能暂不可用」**。不做重试轰炸、不阻塞其余功能。

---

## 13. 落地步骤清单（供实现参考，本次不执行）

1. `backend/db.py`：`init_map_tables()` + `map_documents` + `map_preset_versions` + 一组 `map_*` DB 函数 + `update_user_map_permission` + `get_all_users_with_stats` 加 `can_map`。
2. `main.py`：`from map import map_router` → `include_router(prefix="/map")`；startup 调 `init_map_tables()`；`index()` context 加 `can_map`。
3. `settings.py`：加 §5.4 的 `MAP_*` 常量。
4. `map.py`：新文件，路由见 §5.2，`require_map_access` 抄 `require_draw_access`；版本快照 / 回滚逻辑抄 `save_writing_content` 的留 3 版模式。
5. `templates/_module_switch.html`：新片段；`chat.html / writing.html / drawing.html` 底部按钮区替换为 include，并给 `writing.py` / `drawing.py` 页面路由 context 补 `can_map`。
6. `templates/map.html` + `static/css/map.css` + `static/js/map.js`：三栏 + 双 Tab；地图 style 组装、Tab A 控件、Tab B 编辑器、防抖自动保存、版本弹窗、错误兜底。
7. `static/js/maplibre/`：vendor patched maplibre + `mapstage-isolate/` + `NOTICE.md`。
8. `admin.py` + `templates/admin/users.html`：`set_map` 路由 + 授权按钮 + JS。
9. `ARCHITECTURE.md`：新增「十六、地图模块」章节（与写作/作图对称），更新目录结构、路由表、DB Schema 图、env/常量说明。
10. `PRODUCTION.md`：补一条「升级 MapLibre 需重新评估 isolate 补丁」。

---

## 14. 与项目现有约定的一致性

| 关注点 | 做法 |
|---|---|
| 权限门控 | 新增 `can_map`，`require_map_access` 逐字镜像 `require_draw_access`（HTML 302 / API 403）。 |
| 模块目录约定 | `map.py` 顶层路由文件（同 `writing.py` / `drawing.py`）；`/map/{id}` 通配路由声明在文件末尾。 |
| DB | `databases` 裸 SQL；无向量。`init_map_tables()` 幂等，startup 自动跑。版本历史抄 `writing_contents` 的留 3 版模式。 |
| 前端 | Jinja + Materialize + `style.css`；镜像 `drawing.html` 布局与断点。除拆出整包外不引入 MapStage 前端结构。 |
| 静态资源 | MapLibre + isolate 整包本地 vendor（同 jquery / marked / materialize 的做法）。 |
| AI | 不接。 |
| 磁盘 | 不存地图图片；仅可选侧栏缩略图 `static/maps/{username}/{id}.png`（同 `static/images/` 约定）。 |

---

## 15. 许可 / 署名

- 代码 & 文档：随 TSAI。
- vendor 的 MapStage 代码：MIT（`static/js/maplibre/mapstage-isolate/NOTICE.md` 注明来源 commit）。
- 运行时数据署名（地图上 attribution 控件必须显示）：
  - `Sentinel-2 cloudless © EOX · modified Copernicus Sentinel data`
  - `© Mapterhorn`
  - `© OpenStreetMap contributors, OpenMapTiles`
  - 拆出开启时另注 `Natural Earth (public domain) · © OpenStreetMap contributors (ODbL)`
