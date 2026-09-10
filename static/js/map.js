/* 地图模块 /map/ 前端。
 * 中间是 MapLibre 地图；右侧 Tab A（地图效果）/ Tab B（点·线·标注）。
 * preset 实时防抖自动保存（PATCH /map/documents/{id}）；版本历史见 checkpoint 逻辑。
 * 全部第三方（瓦片/DEM/矢量/字体）由浏览器直连；失败只做友好提示 + 关掉受影响功能。
 * 无任何 AI 调用。
 */
(function () {
  "use strict";
  var initData = JSON.parse(document.getElementById("map-init-data").textContent || "{}");
  var MAP_ID = initData.mapId;

  // ---- DOM ----
  var $mapMain = document.getElementById("map-main");
  var $empty = document.getElementById("map-empty-state");
  var $fatal = document.getElementById("map-fatal");
  var $list = document.getElementById("map-list");
  var $nameInput = document.getElementById("map-name");

  // ---- 状态 ----
  var map = null;
  var tileCfg = null;
  var doc = null;          // { id, name, preset, ... }
  var preset = null;       // doc.preset（内存中实时改）
  var warned = new Set();  // 已提示过的第三方失败，避免刷屏
  var isolateCtl = null;
  var markers = {};        // pointId -> maplibregl.Marker
  var linkHandles = [];    // 选中连线时，其起/终点的可拖动手柄 Marker
  var selectedId = null;   // 选中的点或线 id
  var annotMode = "select";
  var pendingLinkFrom = null;
  var presetAtLoad = null; // 打开文档时的 preset 快照（首次编辑前存 open-diff 版）
  var openDiffDone = false;
  var lastSnapshotKey = null;

  // 版本快照的“是否有变化”判断——排除相机（缩放/平移/俯仰/朝向）。
  // 相机仍照常实时存进 map_documents.preset，只是不再单独触发版本快照。
  function snapshotKey(p) {
    var c;
    try { c = JSON.parse(JSON.stringify(p || {})); } catch (e) { return "" + Math.random(); }
    if (c.style) delete c.style.camera;
    return JSON.stringify(c);
  }

  // ============ 工具 ============
  function toast(msg, cls) { M.toast({ html: msg, classes: cls || "", displayLength: 6000 }); }
  function warnOnce(key, msg) { if (warned.has(key)) return; warned.add(key); toast(msg, "orange darken-2"); }
  function debounce(fn, ms) {
    var t;
    return function () { clearTimeout(t); var a = arguments, self = this; t = setTimeout(function () { fn.apply(self, a); }, ms); };
  }
  function deepGet(o, path) { return path.split(".").reduce(function (x, k) { return x == null ? x : x[k]; }, o); }
  function deepSet(o, path, val) {
    var ks = path.split("."), last = ks.pop();
    var t = ks.reduce(function (x, k) { if (x[k] == null) x[k] = {}; return x[k]; }, o);
    t[last] = val;
  }
  function clone(x) { return JSON.parse(JSON.stringify(x)); }
  function uid(p) { return p + Math.random().toString(36).slice(2, 8); }
  function fmtTime(s) { try { return new Date(s).toLocaleString("zh-CN"); } catch (e) { return String(s || "").slice(0, 16); } }

  // ============ 点位类型默认 ============
  var TIERS = {
    capital:    { label: "都城", color: "#a11d1d", shape: "star" },
    commandery: { label: "郡城", color: "#b8442e", shape: "square" },
    city:       { label: "城池", color: "#c07a2e", shape: "circle" },
    pass:       { label: "关隘", color: "#5b6b7a", shape: "gate" },
    station:    { label: "驿站", color: "#3f7a6a", shape: "diamond" },
    custom:     { label: "自定义", color: "#5c6bc0", shape: "circle" },
  };
  function tierColor(t) { return (TIERS[t] || TIERS.city).color; }
  function tierShape(t) { return (TIERS[t] || TIERS.city).shape; }

  // ============ 启动 ============
  document.addEventListener("DOMContentLoaded", function () {
    M.Sidenav.init(document.querySelectorAll(".sidenav"));
    M.Modal.init(document.querySelectorAll(".modal"));

    document.getElementById("logout-btn").addEventListener("click", function () {
      fetch("/account/logout", { method: "POST", credentials: "include" }).then(function () {
        window.location.href = "/account/login";
      });
    });
    document.getElementById("btn-new-map").addEventListener("click", openNewMapModal);
    document.getElementById("btn-new-map-nav").addEventListener("click", openNewMapModal);
    document.getElementById("btn-confirm-new-map").addEventListener("click", confirmNewMap);
    document.getElementById("form-new-map").addEventListener("submit", function (e) { e.preventDefault(); confirmNewMap(); });
    document.getElementById("btn-confirm-del-map").addEventListener("click", confirmDelMap);

    wireToolbar();
    wireTabs();
    wireStyleControls();
    wireAnnotControls();
    wireAddPointPanel();
    wireAnnotEditorCollapse();
    document.getElementById("btn-new-group").addEventListener("click", startCreateGroup);
    document.getElementById("btn-confirm-group-pick").addEventListener("click", confirmCreateGroup);
    document.getElementById("btn-cancel-group-pick").addEventListener("click", cancelCreateGroup);

    loadMapList();

    if (!MAP_ID) { markShellEmpty(true); return; }

    if (window.__MAPLIBRE_LOAD_FAILED || typeof maplibregl === "undefined") {
      showFatal("地图引擎（MapLibre）因当前网络环境问题无法加载，地图功能暂不可用。请检查网络后刷新。");
      return;
    }
    bootMap();
  });

  function markShellEmpty(isEmpty) {
    document.querySelectorAll(".map-hidden-when-empty").forEach(function (el) {
      el.classList.toggle("map-empty-hidden", isEmpty);
    });
    $empty.style.display = isEmpty ? "flex" : "none";
  }
  function showFatal(msg) {
    markShellEmpty(true);
    $empty.style.display = "none";
    $fatal.textContent = msg;
    $fatal.style.display = "flex";
  }

  // ============ 左侧地图列表 ============
  function loadMapList() {
    authFetch("/map/documents").then(function (r) { return r.ok ? r.json() : []; }).then(function (docs) {
      $list.innerHTML = "";
      if (!docs.length) {
        $list.innerHTML = '<p class="map-empty center-align">还没有地图，点击下方按钮新建</p>';
        return;
      }
      docs.forEach(function (d) {
        var row = document.createElement("div");
        row.className = "map-item" + (d.id === MAP_ID ? " active" : "");
        var t = document.createElement("div");
        t.className = "map-item-title";
        t.textContent = d.name || "未命名地图";
        row.appendChild(t);
        var del = document.createElement("button");
        del.className = "map-item-del";
        del.innerHTML = '<i class="material-icons">close</i>';
        del.addEventListener("click", function (e) { e.stopPropagation(); openDelMapModal(d.id, d.name); });
        row.appendChild(del);
        row.addEventListener("click", function () { if (d.id !== MAP_ID) window.location.href = "/map/" + d.id; });
        $list.appendChild(row);
      });
    });
  }

  function openNewMapModal() {
    document.getElementById("new-map-name").value = "";
    M.Modal.getInstance(document.getElementById("modal-new-map")).open();
  }
  function confirmNewMap() {
    var name = document.getElementById("new-map-name").value.trim() || "未命名地图";
    authFetch("/map/documents", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: name }),
    }).then(function (r) { return r.json(); }).then(function (d) {
      if (d && d.id) window.location.href = "/map/" + d.id;
      else toast("新建失败", "red darken-1");
    });
  }
  var _delId = null;
  function openDelMapModal(id, name) {
    _delId = id;
    document.getElementById("del-map-name").textContent = name || "未命名地图";
    M.Modal.getInstance(document.getElementById("modal-del-map")).open();
  }
  function confirmDelMap() {
    if (!_delId) return;
    var id = _delId; _delId = null;
    authFetch("/map/documents/" + id, { method: "DELETE" }).then(function (r) {
      if (!r.ok) { toast("删除失败", "red darken-1"); return; }
      if (id === MAP_ID) { window.location.href = "/map/"; return; }
      loadMapList();
    });
  }

  // ============ 载入并初始化地图 ============
  function bootMap() {
    Promise.all([
      authFetch("/map/tile-config").then(function (r) { return r.json(); }),
      authFetch("/map/documents/" + MAP_ID).then(function (r) {
        if (r.status === 404) { window.location.href = "/map/"; throw new Error("404"); }
        return r.json();
      }),
    ]).then(function (res) {
      tileCfg = res[0];
      doc = res[1];
      preset = doc.preset && doc.preset.style ? doc.preset : defaultPreset();
      if (!preset.annotations) preset.annotations = { points: [], links: [], groups: [], order: [] };
      migrateLinks();
      ensureGroups();
      ensureOrder();
      presetAtLoad = clone(preset);
      $nameInput.value = doc.name || "";
      M.updateTextFields && M.updateTextFields();
      markShellEmpty(false);
      $empty.style.display = "none";
      initMapLibre();
      syncStyleControls();
      renderList();       // Tab B 列表：加载即列出已有点/线/组，不必先点地图
      renderEditor();
      loadVersions();
      startCheckpointTimer();
    }).catch(function (e) {
      if (String(e && e.message) !== "404") {
        showFatal("地图数据加载失败：" + (e && e.message || e) + "。请刷新重试。");
      }
    });
  }

  function defaultPreset() {
    return {
      version: 3,
      style: {
        view: "map",
        camera: { center: [104.0, 35.5], zoom: 4.2, pitch: 0, bearing: 0 },
        basemap: { satellite: true, relief: false, isolate: false, isolateRegion: "china" },
        mapstage: {
          backgroundColor: "#c8c2b4", terrainExaggeration: 1,
          satellite: { opacity: 0.7, saturation: -0.08, contrast: 0.02, brightnessMin: 0.08, brightnessMax: 0.96, hueRotate: 12 },
          hillshade: { exaggeration: 0.42, illuminationDirection: 315, shadowColor: "#2c2824", highlightColor: "#f0ece4", accentColor: "#6a6458" },
          water: {
            lakeFill: "rgba(50,118,138,0.9)", lakeOutline: "rgba(90,110,105,0.8)",
            riverLevel3: "rgba(48,112,128,0.55)", riverLevel2: "rgba(45,110,125,0.88)", riverLevel1: "rgba(42,105,120,0.95)",
            riverL3Width: 1.2, riverL2Width: 2, riverL1Width: 1.8,
          },
        },
        css: { sepia: 0.08, saturate: 0.98, contrast: 1.03, brightness: 1.0, hueRotate: -1, warmTintAlpha: 0.04, warmTintColor: "#9a8868", vignetteStrength: 0.1, enabled: true },
        admin: { enabled: false, boundary: true, place: true, road: false },
        ui: { showWater: true },
      },
      annotations: { points: [], links: [], groups: [], order: [] },
    };
  }

  function emptyFC() { return { type: "FeatureCollection", features: [] }; }

  function ensureAdmin() {
    if (!preset.style.admin) preset.style.admin = { enabled: false, boundary: true, place: true, road: false };
    return preset.style.admin;
  }
  function _adminVis(kind) {
    var a = preset.style.admin || {};
    return (a.enabled && a[kind]) ? "visible" : "none";
  }
  function applyAdminVis() {
    var a = ensureAdmin();
    var b = !!(a.enabled && a.boundary), p = !!(a.enabled && a.place), r = !!(a.enabled && a.road);
    setLayerVis("admin-boundary-country", b);
    setLayerVis("admin-boundary-state", b);
    setLayerVis("admin-place", p);
    setLayerVis("admin-road", r);
  }

  function buildStyleSpec() {
    var s = preset.style, ms = s.mapstage;
    ensureAdmin();
    var sat = tileCfg.satellite, terr = tileCfg.terrain, vec = tileCfg.vector;
    return {
      version: 8,
      glyphs: vec.glyphs,
      sources: {
        basemapRaster: { type: "raster", tiles: sat.tiles, tileSize: sat.tileSize || 256, maxzoom: sat.maxzoom || 14, attribution: sat.attribution },
        terrain: { type: "raster-dem", tiles: terr.tiles, tileSize: terr.tileSize || 512, maxzoom: terr.maxzoom || 14, encoding: terr.encoding || "terrarium", attribution: terr.attribution },
        openmaptiles: { type: "vector", url: vec.tilejson, attribution: vec.attribution },
        "annot-links": { type: "geojson", data: emptyFC() },
        "annot-arrowheads": { type: "geojson", data: emptyFC() },
        "annot-link-labels": { type: "geojson", data: emptyFC() },
      },
      layers: [
        { id: "bg", type: "background", paint: { "background-color": ms.backgroundColor } },
        { id: "hillshade", type: "hillshade", source: "terrain", paint: {
          "hillshade-exaggeration": ms.hillshade.exaggeration,
          "hillshade-illumination-direction": ms.hillshade.illuminationDirection,
          "hillshade-shadow-color": ms.hillshade.shadowColor,
          "hillshade-highlight-color": ms.hillshade.highlightColor,
          "hillshade-accent-color": ms.hillshade.accentColor,
        } },
        { id: "satellite", type: "raster", source: "basemapRaster",
          layout: { visibility: s.basemap.satellite ? "visible" : "none" },
          paint: {
            "raster-opacity": ms.satellite.opacity, "raster-saturation": ms.satellite.saturation,
            "raster-contrast": ms.satellite.contrast, "raster-brightness-min": ms.satellite.brightnessMin,
            "raster-brightness-max": ms.satellite.brightnessMax, "raster-hue-rotate": ms.satellite.hueRotate,
          } },
        { id: "water-fill", type: "fill", source: "openmaptiles", "source-layer": "water",
          layout: { visibility: s.ui.showWater ? "visible" : "none" },
          paint: { "fill-color": ms.water.lakeFill, "fill-outline-color": ms.water.lakeOutline } },
        { id: "waterway", type: "line", source: "openmaptiles", "source-layer": "waterway",
          layout: { visibility: s.ui.showWater ? "visible" : "none", "line-cap": "round" },
          paint: {
            "line-color": ["match", ["get", "class"], "river", ms.water.riverLevel1, "canal", ms.water.riverLevel2, "stream", ms.water.riverLevel3, ms.water.riverLevel3],
            "line-width": ["match", ["get", "class"], "river", ms.water.riverL1Width, "canal", ms.water.riverL2Width, ms.water.riverL3Width],
          } },
        // ── 行政区划（openmaptiles 矢量源）：道路 → 省界 → 国界 → 地名 ──
        { id: "admin-road", type: "line", source: "openmaptiles", "source-layer": "transportation",
          minzoom: 5,
          filter: ["match", ["get", "class"], ["motorway", "trunk", "primary", "secondary"], true, false],
          layout: { visibility: _adminVis("road"), "line-cap": "round", "line-join": "round" },
          paint: {
            "line-color": "#9a8a6a", "line-opacity": 0.7,
            "line-width": ["interpolate", ["linear"], ["zoom"],
              5, ["match", ["get", "class"], "motorway", 1.2, "trunk", 1, 0.6],
              12, ["match", ["get", "class"], "motorway", 4, "trunk", 3, "primary", 2, 1.2]],
          } },
        { id: "admin-boundary-state", type: "line", source: "openmaptiles", "source-layer": "boundary",
          minzoom: 3,
          filter: ["all", ["==", ["get", "admin_level"], 4], ["!=", ["get", "maritime"], 1]],
          layout: { visibility: _adminVis("boundary"), "line-join": "round" },
          paint: { "line-color": "#8a7a5a", "line-dasharray": [3, 2], "line-opacity": 0.8,
            "line-width": ["interpolate", ["linear"], ["zoom"], 3, 0.5, 10, 1.4] } },
        { id: "admin-boundary-country", type: "line", source: "openmaptiles", "source-layer": "boundary",
          filter: ["all", ["<=", ["get", "admin_level"], 2], ["!=", ["get", "maritime"], 1]],
          layout: { visibility: _adminVis("boundary"), "line-join": "round", "line-cap": "round" },
          paint: { "line-color": "#5a4a38", "line-opacity": 0.9,
            "line-width": ["interpolate", ["linear"], ["zoom"], 2, 0.8, 8, 2.2] } },
        { id: "admin-place", type: "symbol", source: "openmaptiles", "source-layer": "place",
          filter: ["match", ["get", "class"], ["country", "state", "province", "city", "town"], true, false],
          layout: {
            visibility: _adminVis("place"),
            "text-field": ["coalesce", ["get", "name:zh"], ["get", "name:latin"], ["get", "name"]],
            "text-font": ["Noto Sans Regular"],
            "text-size": ["interpolate", ["linear"], ["zoom"],
              2, ["match", ["get", "class"], "country", 12, ["state", "province"], 10, 9],
              8, ["match", ["get", "class"], "country", 18, ["state", "province"], 15, "city", 13, 11]],
            "text-max-width": 7, "text-padding": 4,
            "text-transform": ["match", ["get", "class"], "country", "uppercase", "none"],
            "symbol-sort-key": ["coalesce", ["get", "rank"], 20],
          },
          paint: { "text-color": "#3a3020", "text-halo-color": "rgba(248,244,234,0.92)", "text-halo-width": 1.4 } },

        { id: "annot-link-solid", type: "line", source: "annot-links",
          filter: ["!=", ["get", "dash"], true],
          layout: { "line-cap": "round", "line-join": "round" },
          paint: { "line-color": ["get", "color"], "line-width": ["get", "width"] } },
        { id: "annot-link-dash", type: "line", source: "annot-links",
          filter: ["==", ["get", "dash"], true],
          layout: { "line-cap": "butt", "line-join": "round" },
          paint: { "line-color": ["get", "color"], "line-width": ["get", "width"], "line-dasharray": [2, 2] } },
        // 箭头：单独的点数据源 + 自绘图标（不依赖字体 glyph，➤ 在 Noto Sans 里不存在）
        // icon 按每条连线自己的 arrowStyle 取图（annot-arrow-triangle/narrow/chevron）。
        // 大小 = 原有的按缩放级别插值曲线 × 每条连线自己的 arrowSize 倍数——
        // ["zoom"] 表达式只能作为 interpolate/step 的顶层输入，不能嵌在其他表达式（如 "*"）里面，
        // 所以把 sizeMul 乘法挪进 interpolate 每个档位的输出值里，而不是包在 interpolate 外面。
        { id: "annot-link-arrow", type: "symbol", source: "annot-arrowheads",
          layout: {
            "icon-image": ["get", "icon"],
            "icon-size": ["interpolate", ["linear"], ["zoom"],
              3, ["*", ["get", "sizeMul"], 0.5],
              12, ["*", ["get", "sizeMul"], 1]],
            "icon-rotate": ["get", "bearing"],
            "icon-rotation-alignment": "map",
            "icon-allow-overlap": true, "icon-ignore-placement": true, "icon-anchor": "center",
          },
          paint: { "icon-color": ["get", "color"] } },
        // 连线名称标注：单独的点数据源（各连线中点），角度可独立调整以配合走势
        { id: "annot-link-label", type: "symbol", source: "annot-link-labels",
          layout: {
            "text-field": ["get", "text"],
            "text-font": ["Noto Sans Regular"],
            "text-size": ["get", "size"],
            "text-rotate": ["get", "angle"],
            "text-rotation-alignment": "map",
            "text-allow-overlap": true, "text-ignore-placement": true, "text-anchor": "center",
          },
          paint: { "text-color": ["get", "color"], "text-halo-color": "rgba(255,255,255,0.85)", "text-halo-width": 1.2 } },
      ],
    };
  }

  function initMapLibre() {
    // MapLibre 的 Style.loadJSON 用 requestAnimationFrame 推迟真正的样式加载，
    // 而浏览器在标签页处于后台时会暂停 rAF —— 若地图在后台标签里初始化，
    // 样式永远加载不完、整块空白。等页面可见再创建。
    if (document.hidden) {
      $empty.textContent = "切换到本标签页后加载地图…";
      $empty.style.display = "flex";
      var onVis = function () {
        if (document.hidden) return;
        document.removeEventListener("visibilitychange", onVis);
        $empty.style.display = "none";
        _createMap();
      };
      document.addEventListener("visibilitychange", onVis);
      return;
    }
    _createMap();
  }

  function _createMap() {
    var s = preset.style;
    map = new maplibregl.Map({
      container: "maplibre-map",
      style: buildStyleSpec(),
      center: s.camera.center, zoom: s.camera.zoom, pitch: s.camera.pitch || 0, bearing: s.camera.bearing || 0,
      maxPitch: 85, attributionControl: true, renderWorldCopies: false,
    });
    map.addControl(new maplibregl.NavigationControl({ visualizePitch: true }), "bottom-right");

    window.__map = map; // debug
    map.on("error", onMapError);

    map.on("load", function () {
      try { map.setProjection({ type: s.view === "globe" ? "globe" : "mercator" }); } catch (e) {}
      try {
        ARROW_STYLES.forEach(function (st) {
          var imgId = "annot-arrow-" + st[0];
          if (!map.hasImage(imgId)) map.addImage(imgId, makeArrowImage(st[0], 24), { sdf: true });
        });
      } catch (e) {}
      applyTerrain();
      addColorReliefLayer();
      applyCssOverlays();
      setViewSeg(s.view);
      renderAnnotations();
      wireMapInteractions();
      setupIsolate();
    });

    map.on("moveend", debounce(function () {
      if (!map) return;
      var c = map.getCenter();
      s.camera = { center: [c.lng, c.lat], zoom: map.getZoom(), pitch: map.getPitch(), bearing: map.getBearing() };
      scheduleSave();
    }, 400));

    // 倾斜角变化时重新决定是否启用 3D 地形 mesh（见 applyTerrain 注释）
    map.on("pitchend", applyTerrain);
  }

  function addColorReliefLayer() {
    if (map.getLayer("vl-relief")) return;
    var VP = window.AntiqueVectorPaint;
    if (!VP || typeof VP.elevationColorExpr !== "function") return;
    try {
      map.addLayer({
        id: "vl-relief", type: "color-relief", source: "terrain",
        layout: { visibility: preset.style.basemap.relief ? "visible" : "none" },
        paint: { "color-relief-opacity": 1, "color-relief-color": VP.elevationColorExpr() },
      }, map.getLayer("satellite") ? "satellite" : undefined);
    } catch (e) { /* color-relief 不支持则忽略，海拔设色开关无效 */ }
  }

  function applyTerrain() {
    // 3D 地形 mesh（map.setTerrain）会让 DOM 标记跟随地形高程做屏幕偏移，
    // 平铺俯视时这没有任何视觉收益，反而导致标记在“地形起伏大 + 缩放时 DEM LOD 变化”
    // 的位置上抖动/错位（山区的点尤其明显）。因此只在地球视图或明显倾斜时启用 mesh；
    // 平铺俯视只保留 hillshade / 海拔设色（这两个图层不依赖 setTerrain）。
    var ex = Number(preset.style.mapstage.terrainExaggeration) || 0;
    var pitched = !!(map && typeof map.getPitch === "function" && map.getPitch() > 4);
    var want3D = ex > 0 && (preset.style.view === "globe" || pitched);
    try { map.setTerrain(want3D ? { source: "terrain", exaggeration: ex } : null); } catch (e) {}
  }

  // ============ CSS 古卷滤镜（DOM 叠层）============
  function applyCssOverlays() {
    var c = preset.style.css || {};
    var on = c.enabled !== false;
    var filterStr = on
      ? "sepia(" + (c.sepia || 0) + ") saturate(" + (c.saturate || 1) + ") contrast(" + (c.contrast || 1) + ") brightness(" + (c.brightness || 1) + ") hue-rotate(" + (c.hueRotate || 0) + "deg)"
      : "none";
    var container = document.getElementById("maplibre-map");
    // 滤镜绝不能加在地图容器上：容器里同时有 WebGL 画布和 DOM 标记(maplibregl.Marker)。
    // 给容器加 filter 会把标记也套进同一个被光栅化的滤镜图层，浏览器对该图层的重绘节奏
    // 跟不上 canvas 每帧重绘，缩放时标记的 transform 就“悬浮/滞后”，缩放结束才归位。
    // 解法：只给 canvas 本身加滤镜；标记是容器的直接子元素、不在 canvas 内，因此不受影响。
    container.style.filter = "none";
    container.querySelectorAll(".maplibregl-canvas").forEach(function (el) { el.style.filter = filterStr; });
    $mapMain.style.setProperty("--vignette", on ? (c.vignetteStrength || 0) : 0);
    var tint = document.getElementById("map-warm-tint");
    if (!tint) {
      tint = document.createElement("div");
      tint.id = "map-warm-tint";
      $mapMain.appendChild(tint);
    }
    tint.style.background = c.warmTintColor || "#9a8868";
    tint.style.opacity = on ? (c.warmTintAlpha || 0) : 0;
  }

  // ============ 第三方失败兜底 ============
  function onMapError(e) {
    var sid = e && e.sourceId;
    var msg = String((e && e.error && e.error.message) || e || "");
    if (sid === "basemapRaster") {
      warnOnce("sat", "卫星底图服务（EOX）因当前网络环境问题无法访问，当前功能暂不可用");
      setToggle("tg-satellite", false); preset.style.basemap.satellite = false; setLayerVis("satellite", false);
    } else if (sid === "terrain") {
      warnOnce("terr", "地形服务（Mapterhorn）因当前网络环境问题无法访问，当前功能暂不可用");
      try { map.setTerrain(null); } catch (x) {}
      setLayerVis("hillshade", false); setLayerVis("vl-relief", false);
      setToggle("tg-relief", false); preset.style.basemap.relief = false;
    } else if (sid === "openmaptiles") {
      warnOnce("vec", "矢量瓦片服务（OpenFreeMap）因当前网络环境问题无法访问，当前功能暂不可用");
      setLayerVis("water-fill", false); setLayerVis("waterway", false);
      setToggle("tg-water", false); preset.style.ui.showWater = false;
      ["admin-road", "admin-boundary-state", "admin-boundary-country", "admin-place"].forEach(function (l) { setLayerVis(l, false); });
      setToggle("tg-admin", false); ensureAdmin().enabled = false;
      document.getElementById("admin-sub").style.display = "none";
    } else if (/glyph|font|\.pbf/i.test(msg)) {
      warnOnce("glyphs", "文字字体服务（OpenFreeMap）无法访问，行政区划地名 / 连线名称标注暂不显示，其余功能不受影响");
    } else if (msg) {
      console.warn("[map] error:", msg);
    }
  }
  function setLayerVis(id, on) { try { if (map.getLayer(id)) map.setLayoutProperty(id, "visibility", on ? "visible" : "none"); } catch (e) {} }
  function setToggle(id, on) { var el = document.getElementById(id); if (el) el.checked = !!on; }

  // ============ Tab 切换 ============
  function wireTabs() {
    document.querySelectorAll(".map-tabs a").forEach(function (a) {
      a.addEventListener("click", function (e) {
        e.preventDefault();
        document.querySelectorAll(".map-tabs a").forEach(function (x) { x.classList.remove("active"); });
        a.classList.add("active");
        var tab = a.getAttribute("data-tab");
        document.getElementById("tab-style").style.display = tab === "style" ? "" : "none";
        document.getElementById("tab-annot").style.display = tab === "annot" ? "" : "none";
      });
    });
  }

  // ============ Tab A：地图效果控件 ============
  function wireStyleControls() {
    $nameInput.addEventListener("input", debounce(function () {
      authFetch("/map/documents/" + MAP_ID, {
        method: "PATCH", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: $nameInput.value }),
      }).then(function () { loadMapList(); });
    }, 700));

    // 滑块 / 颜色 / rgba 文本：data-key 落到 preset.style.mapstage.<key> 或 css.<key> 或 backgroundColor / terrainExaggeration
    document.querySelectorAll("#tab-style [data-key]").forEach(function (el) {
      var key = el.getAttribute("data-key");
      var evt = el.type === "range" || el.classList.contains("map-color") ? "input" : "change";
      el.addEventListener(evt, function () {
        var v = el.type === "range" ? Number(el.value) : el.value;
        setStyleField(key, v);
        var lab = document.querySelector('#tab-style .v[data-for="' + key + '"]');
        if (lab) lab.textContent = el.type === "range" ? el.value : "";
        scheduleSave();
      });
    });

    var toggles = {
      "tg-satellite": function (on) { preset.style.basemap.satellite = on; setLayerVis("satellite", on); },
      "tg-relief": function (on) { preset.style.basemap.relief = on; setLayerVis("vl-relief", on); },
      "tg-water": function (on) { preset.style.ui.showWater = on; setLayerVis("water-fill", on); setLayerVis("waterway", on); },
      "tg-css": function (on) { preset.style.css.enabled = on; applyCssOverlays(); },
      "tg-admin": function (on) {
        ensureAdmin().enabled = on;
        document.getElementById("admin-sub").style.display = on ? "" : "none";
        applyAdminVis();
      },
      "tg-admin-boundary": function (on) { ensureAdmin().boundary = on; applyAdminVis(); },
      "tg-admin-place": function (on) { ensureAdmin().place = on; applyAdminVis(); },
      "tg-admin-road": function (on) { ensureAdmin().road = on; applyAdminVis(); },
      "tg-isolate": function (on) {
        preset.style.basemap.isolate = on;
        document.getElementById("isolate-region-wrap").style.display = on ? "" : "none";
        // 第 2 个参数 frame=false：不重新框选区域，保持当前视角/倾角不变
        if (isolateCtl) isolateCtl.setEnabled(on, false);
        else warnOnce("iso", "拆出组件未就绪（可能是轮廓数据未加载），请稍后重试");
      },
    };
    Object.keys(toggles).forEach(function (id) {
      var el = document.getElementById(id);
      el.addEventListener("change", function () { toggles[id](el.checked); scheduleSave(); });
    });
  }

  function setStyleField(key, v) {
    var s = preset.style, ms = s.mapstage;
    if (key === "backgroundColor") { ms.backgroundColor = v; try { map.setPaintProperty("bg", "background-color", v); } catch (e) {} return; }
    if (key === "terrainExaggeration") { ms.terrainExaggeration = v; applyTerrain(); return; }
    if (key.indexOf("css.") === 0) { deepSet(s.css, key.slice(4), v); applyCssOverlays(); return; }
    if (key.indexOf("satellite.") === 0) {
      var sk = key.split(".")[1]; ms.satellite[sk] = v;
      var pmap = { opacity: "raster-opacity", saturation: "raster-saturation", contrast: "raster-contrast", brightnessMin: "raster-brightness-min", brightnessMax: "raster-brightness-max", hueRotate: "raster-hue-rotate" };
      try { map.setPaintProperty("satellite", pmap[sk], v); } catch (e) {}
      return;
    }
    if (key.indexOf("hillshade.") === 0) {
      var hk = key.split(".")[1]; ms.hillshade[hk] = v;
      var hmap = { exaggeration: "hillshade-exaggeration", illuminationDirection: "hillshade-illumination-direction", shadowColor: "hillshade-shadow-color", highlightColor: "hillshade-highlight-color", accentColor: "hillshade-accent-color" };
      try { map.setPaintProperty("hillshade", hmap[hk], v); } catch (e) {}
      return;
    }
    if (key.indexOf("water.") === 0) {
      var wk = key.split(".")[1]; ms.water[wk] = v;
      try {
        map.setPaintProperty("water-fill", "fill-color", ms.water.lakeFill);
        map.setPaintProperty("water-fill", "fill-outline-color", ms.water.lakeOutline);
        map.setPaintProperty("waterway", "line-color", ["match", ["get", "class"], "river", ms.water.riverLevel1, "canal", ms.water.riverLevel2, "stream", ms.water.riverLevel3, ms.water.riverLevel3]);
      } catch (e) {}
      return;
    }
  }

  function syncStyleControls() {
    var s = preset.style, ms = s.mapstage;
    document.querySelectorAll("#tab-style [data-key]").forEach(function (el) {
      var key = el.getAttribute("data-key"), v;
      if (key === "backgroundColor") v = ms.backgroundColor;
      else if (key === "terrainExaggeration") v = ms.terrainExaggeration;
      else if (key.indexOf("css.") === 0) v = deepGet(s.css, key.slice(4));
      else v = deepGet(ms, key);
      if (v == null) return;
      el.value = v;
      var lab = document.querySelector('#tab-style .v[data-for="' + key + '"]');
      if (lab && el.type === "range") lab.textContent = el.value;
    });
    setToggle("tg-satellite", s.basemap.satellite);
    setToggle("tg-relief", s.basemap.relief);
    setToggle("tg-water", s.ui.showWater);
    setToggle("tg-css", s.css.enabled !== false);
    var ad = ensureAdmin();
    setToggle("tg-admin", ad.enabled);
    setToggle("tg-admin-boundary", ad.boundary);
    setToggle("tg-admin-place", ad.place);
    setToggle("tg-admin-road", ad.road);
    document.getElementById("admin-sub").style.display = ad.enabled ? "" : "none";
    setToggle("tg-isolate", s.basemap.isolate);
    document.getElementById("isolate-region-wrap").style.display = s.basemap.isolate ? "" : "none";
  }

  // ============ 顶部工具条 ============
  function wireToolbar() {
    document.querySelectorAll("#viewmode-seg button").forEach(function (b) {
      b.addEventListener("click", function () { setView(b.getAttribute("data-mode")); });
    });
    document.getElementById("btn-recenter").addEventListener("click", function () {
      if (map) map.easeTo({ center: [104.0, 35.5], zoom: 4.2, pitch: 0, bearing: 0, duration: 600 });
    });
    document.getElementById("btn-copy-json").addEventListener("click", function () {
      navigator.clipboard.writeText(JSON.stringify(exportPreset(), null, 2)).then(function () { toast("已复制 JSON", "teal"); });
    });
    document.getElementById("btn-download-json").addEventListener("click", function () {
      var blob = new Blob([JSON.stringify(exportPreset(), null, 2)], { type: "application/json" });
      var a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = (doc && doc.name || "map") + ".json";
      a.click();
      URL.revokeObjectURL(a.href);
    });
    document.getElementById("btn-paste-json").addEventListener("click", function () {
      document.getElementById("paste-json-text").value = "";
      M.Modal.getInstance(document.getElementById("modal-paste-json")).open();
    });
    document.getElementById("btn-apply-paste-json").addEventListener("click", applyPastedJson);
    document.getElementById("btn-snapshot").addEventListener("click", function () { snapshot("manual"); });
    document.getElementById("btn-history").addEventListener("click", function () {
      loadVersions(true);
      M.Modal.getInstance(document.getElementById("modal-history")).open();
    });
  }

  function exportPreset() { return { id: doc && doc.id, label: doc && doc.name, version: 3, style: preset.style, annotations: preset.annotations }; }

  function setView(mode) {
    var next = mode === "globe" ? "globe" : "map";
    preset.style.view = next;
    setViewSeg(next);
    if (map) {
      try { map.setProjection({ type: next === "globe" ? "globe" : "mercator" }); } catch (e) {}
      try { map.setMinZoom(next === "globe" ? 0.8 : 2); } catch (e) {}
      applyTerrain();   // globe 视图启用 3D 地形，回到平铺则关掉
      if (isolateCtl && isolateCtl.sync) isolateCtl.sync();
    }
    scheduleSave();
  }
  function setViewSeg(v) {
    document.querySelectorAll("#viewmode-seg button").forEach(function (b) {
      b.classList.toggle("active", b.getAttribute("data-mode") === v);
    });
  }

  function applyPastedJson() {
    var raw = document.getElementById("paste-json-text").value.trim();
    if (!raw) return;
    var obj;
    try { obj = JSON.parse(raw); } catch (e) { toast("不是有效 JSON", "red darken-1"); return; }
    var incoming = obj.style || obj.mapstage ? (obj.style ? obj.style : { mapstage: obj.mapstage, css: obj.css, camera: obj.camera, ui: obj.ui, basemap: {} }) : null;
    if (!incoming) { toast("JSON 里没有可识别的样式字段", "red darken-1"); return; }
    // 合并到当前 style（保留未提供的字段），annotations 若有则一并
    preset.style = Object.assign({}, preset.style, incoming);
    if (!preset.style.basemap) preset.style.basemap = defaultPreset().style.basemap;
    if (obj.annotations && obj.annotations.points) preset.annotations = obj.annotations;
    M.Modal.getInstance(document.getElementById("modal-paste-json")).close();
    toast("已套用，正在重载…", "teal");
    scheduleSave(true);
    setTimeout(function () { window.location.reload(); }, 600);
  }

  // ============ 版本历史（做法2）============
  var saveDebounced = debounce(function () { doSave(); }, 800);
  function scheduleSave(immediate) {
    // 首次“真正的编辑”（非相机变化）前，先把打开时的状态存一版 open-diff
    if (!openDiffDone && presetAtLoad && snapshotKey(preset) !== snapshotKey(presetAtLoad)) {
      openDiffDone = true;
      authFetch("/map/documents/" + MAP_ID + "/versions", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ preset: exportPresetFrom(presetAtLoad), note: "open-diff" }),
      }).then(function () { lastSnapshotKey = snapshotKey(presetAtLoad); loadVersions(); });
    }
    if (immediate) doSave(); else saveDebounced();
  }
  function exportPresetFrom(p) { return { id: doc && doc.id, label: doc && doc.name, version: 3, style: p.style, annotations: p.annotations }; }
  function doSave() {
    authFetch("/map/documents/" + MAP_ID, {
      method: "PATCH", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ preset: exportPreset() }),
    });
  }
  function syncCameraNow() {
    if (!map || !map.getCenter) return;
    var c = map.getCenter();
    preset.style.camera = { center: [c.lng, c.lat], zoom: map.getZoom(), pitch: map.getPitch(), bearing: map.getBearing() };
  }
  function snapshot(note) {
    syncCameraNow();   // 立即抓取当前视角，避免 moveend 400ms 防抖竞态
    authFetch("/map/documents/" + MAP_ID + "/versions", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ preset: exportPreset(), note: note || "manual" }),
    }).then(function (r) {
      if (r.ok) { lastSnapshotKey = snapshotKey(preset); toast("已存快照", "teal"); loadVersions(); }
    });
  }
  function startCheckpointTimer() {
    lastSnapshotKey = snapshotKey(preset);
    setInterval(function () {
      var k = snapshotKey(preset);
      if (k !== lastSnapshotKey) { lastSnapshotKey = k; snapshot("checkpoint"); }
    }, 120000);
  }
  function loadVersions(renderModal) {
    authFetch("/map/documents/" + MAP_ID + "/versions").then(function (r) { return r.ok ? r.json() : []; }).then(function (vs) {
      if (!renderModal) return;
      var box = document.getElementById("history-list");
      box.innerHTML = "";
      if (!vs.length) { box.innerHTML = '<p class="annot-hint">还没有快照。</p>'; return; }
      vs.forEach(function (v) {
        var row = document.createElement("div");
        row.className = "annot-list-item";
        row.innerHTML = '<span class="nm">v' + v.version + " · " + ({ "open-diff": "打开前", checkpoint: "自动", manual: "手动" }[v.note] || v.note) + " · " + fmtTime(v.created_at) + "</span>";
        var b = document.createElement("a");
        b.href = "#!"; b.className = "annot-danger"; b.textContent = "回滚";
        b.addEventListener("click", function (e) {
          e.preventDefault();
          if (!confirm("回滚到 v" + v.version + "？当前未快照的改动会丢失。")) return;
          authFetch("/map/documents/" + MAP_ID + "/versions/" + v.version + "/restore", { method: "POST" })
            .then(function (r) { if (r.ok) window.location.reload(); else toast("回滚失败", "red darken-1"); });
        });
        row.appendChild(b);
        box.appendChild(row);
      });
    });
  }

  // ============ Tab B：点 / 线 / 标注 ============
  function wireAnnotControls() {
    document.querySelectorAll(".annot-tool").forEach(function (b) {
      b.addEventListener("click", function () {
        document.querySelectorAll(".annot-tool").forEach(function (x) { x.classList.remove("active"); });
        b.classList.add("active");
        annotMode = b.getAttribute("data-mode");
        pendingLinkFrom = null;
        var hints = {
          select: "点标记 / 线选中后在下方编辑；拖动标记可改位置。删除在下方列表里操作。",
          "add-point": "三种方式任选：① 在地图上点击 ② 输入经纬度 ③ 搜索地名。",
          "add-link": "先点起点，再点终点，生成一条连线。",
        };
        document.getElementById("annot-mode-hint").textContent = hints[annotMode] || "";
        var addPanel = document.getElementById("add-point-panel");
        if (addPanel) {
          addPanel.style.display = annotMode === "add-point" ? "" : "none";
          if (annotMode !== "add-point") document.getElementById("geocode-results").innerHTML = "";
        }
        renderPoints();
        renderList();
        renderLinkHandles();
      });
    });
  }

  // 属性面板折叠：点「属性」标题栏或其小按钮，折叠 / 展开 #annot-editor（状态记 localStorage）
  function wireAnnotEditorCollapse() {
    var head = document.getElementById("annot-editor-head");
    var box = document.getElementById("annot-editor");
    if (!head || !box) return;
    function apply(collapsed) {
      head.classList.toggle("collapsed", collapsed);
      box.style.display = collapsed ? "none" : "";
    }
    var start = false;
    try { start = localStorage.getItem("map.annotEditorCollapsed") === "1"; } catch (e) {}
    apply(start);
    head.addEventListener("click", function () {
      var collapsed = !head.classList.contains("collapsed");
      apply(collapsed);
      try { localStorage.setItem("map.annotEditorCollapsed", collapsed ? "1" : "0"); } catch (e) {}
    });
  }

  // 「加点」子面板：方式二（经纬度）、方式三（地名搜索）
  function wireAddPointPanel() {
    var btnLL = document.getElementById("btn-add-latlng");
    if (btnLL) {
      btnLL.addEventListener("click", function () {
        var lat = parseFloat(document.getElementById("add-lat").value);
        var lng = parseFloat(document.getElementById("add-lng").value);
        if (!isFinite(lat) || !isFinite(lng)) { toast("请输入有效的经纬度", "red darken-1"); return; }
        if (lat < -90 || lat > 90 || lng < -180 || lng > 180) { toast("经纬度超出范围", "red darken-1"); return; }
        addPointAt(lng, lat, lat.toFixed(4) + ", " + lng.toFixed(4));
        document.getElementById("add-lat").value = "";
        document.getElementById("add-lng").value = "";
      });
    }

    var q = document.getElementById("geocode-q");
    var btnG = document.getElementById("btn-geocode");
    var box = document.getElementById("geocode-results");
    function runGeocode() {
      var term = (q.value || "").trim();
      if (term.length < 2) { toast("请输入至少 2 个字", "orange darken-2"); return; }
      box.innerHTML = '<p class="annot-hint">搜索中…</p>';
      authFetch("/map/geocode?q=" + encodeURIComponent(term)).then(function (r) {
        if (!r.ok) return r.json().then(function (d) { throw new Error(d.detail || ("HTTP " + r.status)); });
        return r.json();
      }).then(function (data) {
        var list = (data && data.results) || [];
        box.innerHTML = "";
        if (!list.length) { box.innerHTML = '<p class="annot-hint">未找到「' + term + '」</p>'; return; }
        list.forEach(function (res) {
          var row = document.createElement("div");
          row.className = "geocode-item";
          row.innerHTML =
            '<div class="gc-name">' + (res.short || res.name) + "</div>" +
            '<div class="gc-sub">' + (res.name || "") + " · " + res.lat.toFixed(4) + ", " + res.lng.toFixed(4) + "</div>";
          row.addEventListener("click", function () {
            addPointAt(res.lng, res.lat, res.short || res.name.split(",")[0]);
            box.innerHTML = "";
            q.value = "";
          });
          box.appendChild(row);
        });
      }).catch(function (e) {
        box.innerHTML = '<p class="annot-hint annot-danger">' + (e && e.message || "搜索失败") + "</p>";
      });
    }
    if (btnG) btnG.addEventListener("click", runGeocode);
    if (q) q.addEventListener("keydown", function (e) { if (e.key === "Enter") { e.preventDefault(); runGeocode(); } });
  }

  function wireMapInteractions() {
    map.on("click", "annot-link-solid", onLinkClick);
    map.on("click", "annot-link-dash", onLinkClick);
    map.on("click", function (e) {
      if (e._annotHandled) return;
      if (annotMode === "add-point") {
        addPointAt(e.lngLat.lng, e.lngLat.lat);
      } else {
        selectedId = null; renderEditor(); renderList(); renderPoints();
      }
    });
  }

  // 统一的建点入口（地图点击 / 经纬度 / 地名搜索 三种方式共用）
  function addPointAt(lng, lat, name) {
    var pt = {
      id: uid("p_"), name: name || "新地点", lng: lng, lat: lat,
      tier: "city", shape: "circle", size: 1,
      markerColor: tierColor("city"),
      label: {},
    };
    ensureLabel(pt);
    preset.annotations.points.push(pt);
    selectedId = pt.id;
    renderAnnotations(); renderEditor(); renderList(); scheduleSave();
    if (map) map.easeTo({ center: [lng, lat], zoom: Math.max(map.getZoom(), 6), duration: 500 });
    return pt;
  }
  function onLinkClick(e) {
    e._annotHandled = true;
    if (!e.features || !e.features.length) return;
    var id = e.features[0].properties.id;
    selectedId = id; renderEditor(); renderList();
  }

  function pointById(id) { return preset.annotations.points.find(function (p) { return p.id === id; }); }
  function linkById(id) { return preset.annotations.links.find(function (l) { return l.id === id; }); }

  function renderAnnotations() { renderLinks(); renderPoints(); renderLinkHandles(); }

  // 选择模式下、且选中的是连线时，在其起点/终点放可拖动手柄（空心蓝环，区别于实心点位）。
  // 手柄在点位标记之后创建 → DOM 顺序在上，端点与点位重合时优先抓到手柄；
  // 取消选中连线 / 切出选择模式，手柄消失，点位恢复可点可拖。
  function clearLinkHandles() {
    linkHandles.forEach(function (m) { try { m.remove(); } catch (e) {} });
    linkHandles = [];
  }
  function renderLinkHandles() {
    clearLinkHandles();
    if (!map || annotMode !== "select") return;
    var lk = linkById(selectedId);
    if (!lk) return;
    ensureLink(lk);
    ["from", "to"].forEach(function (key) {
      var el = document.createElement("div");
      el.className = "link-handle";
      el.title = key === "from" ? "拖动移动起点" : "拖动移动终点";
      var m = new maplibregl.Marker({ element: el, anchor: "center", draggable: true });
      m.setLngLat(lk[key]).addTo(map);
      m.on("drag", function () {
        var ll = m.getLngLat();
        lk[key] = [ll.lng, ll.lat];
        renderLinks();
      });
      m.on("dragend", function () {
        var ll = m.getLngLat();
        lk[key] = [ll.lng, ll.lat];
        renderLinks(); renderList(); scheduleSave();
        setTimeout(function () { if (selectedId === lk.id) renderEditor(); }, 0);
      });
      linkHandles.push(m);
    });
  }

  // 连线与点位解耦：from/to 存 [lng,lat] 自有坐标。加载时把旧数据里
  // from/to 是点位 id（字符串）的，一次性解析成坐标；之后连线完全独立，
  // 改它的起终点不影响任何点位，删/移点位也不影响已有连线。
  function migrateLinks() {
    var byId = {};
    (preset.annotations.points || []).forEach(function (p) { byId[p.id] = p; });
    (preset.annotations.links || []).forEach(function (l) {
      l.from = toCoordPair(l.from, byId);
      l.to = toCoordPair(l.to, byId);
      ensureLink(l);
    });
  }
  function toCoordPair(v, byId) {
    if (Array.isArray(v) && v.length === 2 && isFinite(v[0]) && isFinite(v[1])) return [Number(v[0]), Number(v[1])];
    if (typeof v === "string" && byId && byId[v]) return [byId[v].lng, byId[v].lat];
    return [104.0, 35.5];
  }

  // 箭头样式（[值, 中文标签]，也用作 map.addImage 要注册的图标 id 列表）
  var ARROW_STYLES = [["triangle", "三角形"], ["narrow", "窄三角"], ["chevron", "尖角（>）"]];

  // 连线归一化（curve:"geodesic" -> "arc"；补默认值；from/to 兜底成坐标对）
  function ensureLink(lk) {
    if (lk.curve === "geodesic") lk.curve = "arc";
    if (lk.curve !== "arc" && lk.curve !== "straight") lk.curve = "straight";
    if (typeof lk.directed !== "boolean") lk.directed = true;
    if (typeof lk.dash !== "boolean") lk.dash = false;
    if (typeof lk.bend !== "number") lk.bend = 0.25;
    if (!lk.color) lk.color = "#000000";
    if (!lk.width) lk.width = 2;
    if (!ARROW_STYLES.some(function (o) { return o[0] === lk.arrowStyle; })) lk.arrowStyle = "triangle";
    if (typeof lk.arrowSize !== "number") lk.arrowSize = 1;
    if (!Array.isArray(lk.from) || !isFinite(lk.from[0])) lk.from = [104.0, 35.5];
    if (!Array.isArray(lk.to) || !isFinite(lk.to[0])) lk.to = [104.5, 35.5];
    // 默认名称用经纬度形式（和之前列表里显示的一样），仅在没有名称时补一次；
    // 之后名称完全独立、可编辑，不随起终点移动自动重算。
    if (!lk.name) lk.name = fmtLL(lk.from) + " ⇢ " + fmtLL(lk.to);
    return lk;
  }
  // 连线名称标注：默认隐藏；角度默认取连线当前方位角，之后独立可调（配合走势，不随拖动自动重算）
  function ensureLinkLabel(lk) {
    var L = lk.label || {};
    if (typeof L.show !== "boolean") L.show = false;
    if (!L.fontSize) L.fontSize = 12;
    if (!L.color) L.color = "#1e140c";
    if (typeof L.angle !== "number") L.angle = Math.round(bearingDeg(lk.from, lk.to));
    lk.label = L;
    return L;
  }

  // 二次贝塞尔弧线：控制点 = 中点沿弦的垂线偏移 bend*弦长（bend 可正可负，0=直线）
  function arcCoords(a, b, bend, n) {
    n = n || 40;
    if (!bend) return [a, b];
    var mx = (a[0] + b[0]) / 2, my = (a[1] + b[1]) / 2;
    var dx = b[0] - a[0], dy = b[1] - a[1];
    var len = Math.sqrt(dx * dx + dy * dy) || 1e-9;
    var px = -dy / len, py = dx / len;         // 垂直单位向量
    var off = bend * len;
    var cx = mx + px * off, cy = my + py * off;
    var out = [];
    for (var i = 0; i <= n; i++) {
      var t = i / n, mt = 1 - t;
      out.push([
        mt * mt * a[0] + 2 * mt * t * cx + t * t * b[0],
        mt * mt * a[1] + 2 * mt * t * cy + t * t * b[1],
      ]);
    }
    return out;
  }

  // 罗盘方位角：0=正北，顺时针到 90=正东
  function bearingDeg(a, b) {
    var north = b[1] - a[1];
    var east = (b[0] - a[0]) * Math.cos(((a[1] + b[1]) / 2) * Math.PI / 180);
    return (Math.atan2(east, north) * 180 / Math.PI + 360) % 360;
  }

  // 箭头图标：canvas 画一个朝上（北）的图形，addImage(sdf:true) 后可用 icon-color 着色。
  // 三种样式对应 ARROW_STYLES：triangle（默认，宽三角，缺口）/ narrow（窄三角，无缺口，更尖）/
  // chevron（">"，只描边不填充，两笔角）。全部朝北画，配合 icon-rotate 的 bearing 转向。
  function makeArrowImage(style, s) {
    s = s || 24;
    var cv = document.createElement("canvas"); cv.width = cv.height = s;
    var ctx = cv.getContext("2d");
    if (style === "chevron") {
      ctx.strokeStyle = "#000";
      ctx.lineWidth = s * 0.16;
      ctx.lineCap = "round"; ctx.lineJoin = "round";
      ctx.beginPath();
      ctx.moveTo(s * 0.16, s * 0.86);
      ctx.lineTo(s * 0.5, s * 0.1);
      ctx.lineTo(s * 0.84, s * 0.86);
      ctx.stroke();
    } else if (style === "narrow") {
      ctx.fillStyle = "#000";
      ctx.beginPath();
      ctx.moveTo(s * 0.5, s * 0.03);
      ctx.lineTo(s * 0.66, s * 0.95);
      ctx.lineTo(s * 0.34, s * 0.95);
      ctx.closePath();
      ctx.fill();
    } else {
      ctx.fillStyle = "#000";
      ctx.beginPath();
      ctx.moveTo(s * 0.5, s * 0.06);
      ctx.lineTo(s * 0.88, s * 0.84);
      ctx.lineTo(s * 0.5, s * 0.6);
      ctx.lineTo(s * 0.12, s * 0.84);
      ctx.closePath();
      ctx.fill();
    }
    return ctx.getImageData(0, 0, s, s);
  }

  function setLinkSrc(id, feats) {
    var srcObj = map && map.getSource && map.getSource(id);
    if (srcObj) srcObj.setData({ type: "FeatureCollection", features: feats });
  }

  function renderLinks() {
    var lineFeats = [], arrowFeats = [], labelFeats = [];
    preset.annotations.links.forEach(function (l) {
      ensureLink(l);
      var Lb = ensureLinkLabel(l);
      var A = l.from, B = l.to;                     // 自有坐标，与点位无关
      var coords = l.curve === "arc" ? arcCoords(A, B, l.bend, 40) : [A, B];
      lineFeats.push({
        type: "Feature",
        geometry: { type: "LineString", coordinates: coords },
        properties: { id: l.id, color: l.color, width: l.width, dash: !!l.dash },
      });
      if (l.directed && coords.length >= 2) {
        var p1 = coords[coords.length - 2], p2 = coords[coords.length - 1];
        arrowFeats.push({
          type: "Feature",
          geometry: { type: "Point", coordinates: p2 },
          properties: { color: l.color, bearing: bearingDeg(p1, p2), icon: "annot-arrow-" + l.arrowStyle, sizeMul: l.arrowSize },
        });
      }
      if (Lb.show) {
        var mid = coords[Math.floor((coords.length - 1) / 2)];
        labelFeats.push({
          type: "Feature",
          geometry: { type: "Point", coordinates: mid },
          properties: { id: l.id, text: l.name || "", color: Lb.color, size: Lb.fontSize, angle: Lb.angle },
        });
      }
    });
    setLinkSrc("annot-links", lineFeats);
    setLinkSrc("annot-arrowheads", arrowFeats);
    setLinkSrc("annot-link-labels", labelFeats);
  }

  function renderPoints() {
    Object.keys(markers).forEach(function (id) { markers[id].remove(); });
    markers = {};
    if (!map) return;
    preset.annotations.points.forEach(function (pt) {
      var el = makePointEl(pt);
      var mk = new maplibregl.Marker({ element: el, anchor: "center", draggable: annotMode === "select" });
      mk.setLngLat([pt.lng, pt.lat]).addTo(map);
      mk.on("dragend", function () {
        var ll = mk.getLngLat();
        pt.lng = ll.lng; pt.lat = ll.lat;
        renderLinks(); renderList();
        if (selectedId === pt.id) renderEditor();   // 同步刷新属性面板里的经纬度
        scheduleSave();
      });
      el.addEventListener("click", function (ev) {
        ev.stopPropagation();
        onPointClick(pt.id);
      });
      markers[pt.id] = mk;
    });
  }

  // 前缀图标（16x16 viewBox，单路径，fill=currentColor）
  var PREFIX_ICONS = {
    city: '<path d="M1 15V6l3 2V5l3 2V5l3 2V5l3 2V6l1-.7V15z"/>',
    capital: '<path d="M8 1l1.6 3.6L13 3.4 11.5 7 15 8l-3.5 1 1.5 3.6-3.4-1.2L8 15l-1.6-3.6L3 12.6 4.5 9 1 8l3.5-1L3 3.4l3.4 1.2z"/>',
    pass: '<path d="M2 15V7l6-4 6 4v8h-3V9H5v6z"/>',
    village: '<path d="M8 2l6 4.5V15H2V6.5zM6.5 15v-4h3v4z"/>',
    station: '<path d="M3 2h2v5l5-3v10l-5-3v3H3z"/>',
    mountain: '<path d="M1 14l4.5-8L9 11l2-3 4 6z"/>',
    river: '<path d="M0 6c2.5-3 5-3 8 0s5.5 3 8 0v4c-2.5 3-5 3-8 0s-5.5-3-8 0z"/>',
  };
  var PREFIX_ICON_OPTS = [
    ["city", "城池"], ["capital", "都城"], ["pass", "关隘"], ["village", "村庄"],
    ["station", "驿站"], ["mountain", "山"], ["river", "水"],
  ];

  // 归一化 / 迁移 pt.label（旧数据只有 {show,color}）
  function ensureLabel(pt) {
    var L = pt.label || {};
    if (typeof L.show !== "boolean") L.show = true;
    if (!L.fontSize) L.fontSize = 13;
    if (!L.color) L.color = "#3a2a18";
    L.box = L.box || {};
    if (typeof L.box.show !== "boolean") L.box.show = true;
    if (!L.box.bg) L.box.bg = "#f4ecd8";
    if (!L.box.border) L.box.border = "#b89562";
    if (L.box.padX == null) L.box.padX = 6;
    if (L.box.padY == null) L.box.padY = 3;
    L.prefix = L.prefix || {};
    if (!L.prefix.type) L.prefix.type = "none";
    if (!L.prefix.icon) L.prefix.icon = "city";
    if (L.prefix.text == null) L.prefix.text = "";
    if (!L.prefix.bg) L.prefix.bg = "#b8442e";
    if (!L.prefix.fg) L.prefix.fg = "#ffffff";
    if (!L.prefix.fontSize) L.prefix.fontSize = 12;
    pt.label = L;
    return L;
  }

  function makePointEl(pt) {
    var wrap = document.createElement("div");
    var selG = groupById(selectedId);
    var inSelG = selG && selG.members.indexOf(pt.id) >= 0;
    wrap.className = "map-pt" + (pt.id === selectedId ? " sel" : "") + (inSelG ? " in-group" : "");

    var sh = document.createElement("div");
    sh.className = "map-pt-shape " + (pt.shape || tierShape(pt.tier));
    sh.style.background = pt.markerColor || tierColor(pt.tier);
    var px = Math.round(16 * (pt.size || 1));
    sh.style.width = sh.style.height = px + "px";
    wrap.appendChild(sh);

    var L = ensureLabel(pt);
    if (L.show) {
      var badge = document.createElement("div");
      badge.className = "map-pt-badge" + (L.box.show ? " boxed" : "");
      if (L.box.show) badge.style.borderColor = L.box.border;

      if (L.prefix.type === "icon" || L.prefix.type === "text") {
        var pf = document.createElement("span");
        pf.className = "map-pt-prefix";
        pf.style.background = L.prefix.bg;
        pf.style.color = L.prefix.fg;
        pf.style.fontSize = (L.prefix.fontSize || 12) + "px";
        if (L.prefix.type === "icon") {
          pf.innerHTML = '<svg viewBox="0 0 16 16" width="1em" height="1em" fill="currentColor" style="display:block">' +
            (PREFIX_ICONS[L.prefix.icon] || PREFIX_ICONS.city) + "</svg>";
        } else {
          pf.textContent = L.prefix.text || "";
        }
        badge.appendChild(pf);
      }

      var nm = document.createElement("span");
      nm.className = "map-pt-name";
      nm.textContent = pt.name || "";
      nm.style.color = L.color;
      nm.style.fontSize = (L.fontSize || 13) + "px";
      if (L.box.show) {
        nm.style.background = L.box.bg;
        nm.style.padding = (L.box.padY || 0) + "px " + (L.box.padX || 0) + "px";
      }
      badge.appendChild(nm);
      wrap.appendChild(badge);
    }
    return wrap;
  }

  function onPointClick(id) {
    if (annotMode === "add-link") {
      if (!pendingLinkFrom) { pendingLinkFrom = id; toast("已选起点，再点终点", "teal"); return; }
      if (pendingLinkFrom === id) { pendingLinkFrom = null; return; }
      var pa = pointById(pendingLinkFrom), pb = pointById(id);
      if (!pa || !pb) { pendingLinkFrom = null; return; }
      // 生成时从两个点位复制坐标；之后连线独立，不再引用点位
      var link = { id: uid("l_"), from: [pa.lng, pa.lat], to: [pb.lng, pb.lat] };
      ensureLink(link);
      ensureLinkLabel(link);
      preset.annotations.links.push(link);
      pendingLinkFrom = null;
      selectedId = link.id;
      renderLinks(); renderEditor(); renderList(); scheduleSave();
      return;
    }
    selectedId = id;
    renderEditor(); renderList(); renderPoints();
  }

  function removePoint(id) {
    // 连线与点位解耦：删点位不影响任何连线
    preset.annotations.points = preset.annotations.points.filter(function (p) { return p.id !== id; });
    pruneGroupMember(id);
    if (selectedId === id) selectedId = null;
    renderAnnotations(); renderEditor(); renderList(); scheduleSave();
  }
  function removeLink(id) {
    preset.annotations.links = preset.annotations.links.filter(function (l) { return l.id !== id; });
    pruneGroupMember(id);
    if (selectedId === id) selectedId = null;
    renderLinks(); renderEditor(); renderList(); scheduleSave();
  }

  // ---- 属性编辑区 ----
  function renderEditor() {
    var box = document.getElementById("annot-editor");
    var pt = pointById(selectedId), lk = linkById(selectedId), g = groupById(selectedId);
    box.innerHTML = "";
    if (pt) box.appendChild(pointEditor(pt));
    else if (lk) box.appendChild(linkEditor(lk));
    else if (g) box.appendChild(groupEditor(g));
    else box.innerHTML = '<p class="annot-hint">选中一个点 / 线 / 组来编辑；或用「加点」在地图上点击。</p>';
    renderLinkHandles();
  }

  function field(labelText, inputEl) {
    var d = document.createElement("div");
    d.className = "annot-field";
    var l = document.createElement("label"); l.textContent = labelText;
    d.appendChild(l); d.appendChild(inputEl);
    return d;
  }
  function inlineField(labelText, inputEl) {
    var d = document.createElement("div");
    d.className = "annot-field inline";
    var l = document.createElement("label"); l.textContent = labelText;
    d.appendChild(l); d.appendChild(inputEl);
    return d;
  }
  // 注意：所有原生控件都要 class="browser-default"，否则 materialize.min.css 会把
  // 裸 <input>/<select> 隐藏或改成下划线样式；checkbox 更是被设成 opacity:0，
  // 所以复选框统一用自定义 .map-switch 开关（label 包 input，点 label 即可切换）。
  function txt(val, on) {
    var i = document.createElement("input"); i.type = "text"; i.className = "browser-default";
    i.value = val || ""; i.addEventListener("input", function () { on(i.value); }); return i;
  }
  function col(val, on) {
    var i = document.createElement("input"); i.type = "color"; i.className = "map-color browser-default";
    i.value = val || "#000000"; i.addEventListener("input", function () { on(i.value); }); return i;
  }
  function chk(val, on, disabled) {
    var l = document.createElement("label"); l.className = "map-switch" + (disabled ? " disabled" : "");
    var i = document.createElement("input"); i.type = "checkbox"; i.checked = !!val;
    if (disabled) i.disabled = true;
    else i.addEventListener("change", function () { on(i.checked); });
    l.appendChild(i);
    return l;
  }
  function sel(opts, val, on) {
    var s = document.createElement("select"); s.className = "browser-default";
    opts.forEach(function (o) { var op = document.createElement("option"); op.value = o[0]; op.textContent = o[1]; s.appendChild(op); });
    s.value = val;
    s.addEventListener("change", function () { on(s.value); });
    return s;
  }
  function num(val, min, max, step, on) {
    var wrap = document.createElement("div"); wrap.className = "annot-num";
    var i = document.createElement("input"); i.type = "range"; i.className = "browser-default";
    i.min = min; i.max = max; i.step = step; i.value = val;
    var v = document.createElement("span"); v.className = "annot-num-v"; v.textContent = val;
    i.addEventListener("input", function () { v.textContent = i.value; on(Number(i.value)); });
    wrap.appendChild(i); wrap.appendChild(v);
    return wrap;
  }
  function numInput(val, on) {
    var i = document.createElement("input");
    i.type = "number"; i.className = "annot-in browser-default"; i.step = "any";
    i.value = typeof val === "number" ? Number(val.toFixed(6)) : val;
    i.addEventListener("change", function () { var v = parseFloat(i.value); if (isFinite(v)) on(v); });
    return i;
  }
  // 经纬度微调行：改的是所引用点位的坐标
  function coordRow(labelText, pt) {
    var wrap = document.createElement("div"); wrap.className = "annot-field";
    var l = document.createElement("label"); l.textContent = labelText + "（纬度, 经度）"; wrap.appendChild(l);
    var row = document.createElement("div"); row.className = "annot-inline";
    row.appendChild(numInput(pt.lat, function (v) {
      if (v < -90 || v > 90) return;
      pt.lat = v; afterCoordEdit(pt);
    }));
    row.appendChild(numInput(pt.lng, function (v) {
      if (v < -180 || v > 180) return;
      pt.lng = v; afterCoordEdit(pt);
    }));
    wrap.appendChild(row);
    return wrap;
  }
  function afterCoordEdit(pt) {
    renderPoints(); renderLinks(); renderList();
    if (map) map.easeTo({ center: [pt.lng, pt.lat], duration: 300 });
    scheduleSave();
  }
  // 连线端点经纬度行：ll 是 [lng,lat] 数组，就地改，不碰任何点位
  function coordRowLL(labelText, ll, onDone) {
    var wrap = document.createElement("div"); wrap.className = "annot-field";
    var l = document.createElement("label"); l.textContent = labelText + "（纬度, 经度）"; wrap.appendChild(l);
    var row = document.createElement("div"); row.className = "annot-inline";
    row.appendChild(numInput(ll[1], function (v) { if (v >= -90 && v <= 90) { ll[1] = v; onDone(); } }));
    row.appendChild(numInput(ll[0], function (v) { if (v >= -180 && v <= 180) { ll[0] = v; onDone(); } }));
    wrap.appendChild(row);
    return wrap;
  }
  function fmtLL(ll) { return Number(ll[1]).toFixed(2) + "," + Number(ll[0]).toFixed(2); }

  function pointEditor(pt) {
    var frag = document.createDocumentFragment();
    frag.appendChild(field("名称", txt(pt.name, function (v) { pt.name = v; renderPoints(); renderList(); scheduleSave(); })));
    frag.appendChild(coordRow("位置", pt));
    frag.appendChild(field("类型", sel(Object.keys(TIERS).map(function (k) { return [k, TIERS[k].label]; }), pt.tier, function (v) {
      pt.tier = v; pt.markerColor = TIERS[v].color; pt.shape = TIERS[v].shape;
      renderPoints(); renderList(); renderEditor(); scheduleSave();
    })));
    frag.appendChild(field("形状", sel([["square", "方形"], ["circle", "圆形"], ["diamond", "菱形"], ["gate", "关门"], ["star", "星形"]], pt.shape || tierShape(pt.tier), function (v) { pt.shape = v; renderPoints(); scheduleSave(); })));
    frag.appendChild(field("点位颜色", col(pt.markerColor || tierColor(pt.tier), function (v) { pt.markerColor = v; renderPoints(); renderList(); scheduleSave(); })));
    frag.appendChild(field("大小", num(pt.size || 1, 0.5, 2.5, 0.1, function (v) { pt.size = v; renderPoints(); scheduleSave(); })));
    var L = ensureLabel(pt);
    frag.appendChild(inlineField("显示名称标注", chk(L.show, function (v) { L.show = v; renderPoints(); renderEditor(); renderList(); scheduleSave(); })));
    if (L.show) {
      frag.appendChild(field("名称字号", num(L.fontSize, 9, 28, 1, function (v) { L.fontSize = v; renderPoints(); scheduleSave(); })));
      frag.appendChild(field("名称文字色", col(L.color, function (v) { L.color = v; renderPoints(); scheduleSave(); })));

      frag.appendChild(inlineField("显示信息框", chk(L.box.show, function (v) { L.box.show = v; renderPoints(); renderEditor(); scheduleSave(); })));
      if (L.box.show) {
        frag.appendChild(field("信息框底色", col(L.box.bg, function (v) { L.box.bg = v; renderPoints(); scheduleSave(); })));
        frag.appendChild(field("信息框边线色", col(L.box.border, function (v) { L.box.border = v; renderPoints(); scheduleSave(); })));
        frag.appendChild(field("水平内边距", num(L.box.padX, 0, 16, 1, function (v) { L.box.padX = v; renderPoints(); scheduleSave(); })));
        frag.appendChild(field("垂直内边距", num(L.box.padY, 0, 12, 1, function (v) { L.box.padY = v; renderPoints(); scheduleSave(); })));
      }

      frag.appendChild(field("文字前的元素", sel([["none", "无"], ["icon", "图标"], ["text", "文字（A / 一 / B3）"]], L.prefix.type, function (v) {
        L.prefix.type = v; renderPoints(); renderEditor(); scheduleSave();
      })));
      if (L.prefix.type === "icon") {
        frag.appendChild(field("图标", sel(PREFIX_ICON_OPTS, L.prefix.icon, function (v) { L.prefix.icon = v; renderPoints(); scheduleSave(); })));
      } else if (L.prefix.type === "text") {
        frag.appendChild(field("前缀文字", txt(L.prefix.text, function (v) { L.prefix.text = v; renderPoints(); scheduleSave(); })));
      }
      if (L.prefix.type !== "none") {
        frag.appendChild(field("前缀底色", col(L.prefix.bg, function (v) { L.prefix.bg = v; renderPoints(); scheduleSave(); })));
        frag.appendChild(field("前缀文字色", col(L.prefix.fg, function (v) { L.prefix.fg = v; renderPoints(); scheduleSave(); })));
        frag.appendChild(field("前缀字号", num(L.prefix.fontSize, 9, 24, 1, function (v) { L.prefix.fontSize = v; renderPoints(); scheduleSave(); })));
      }
    }
    var loc = document.createElement("a");
    loc.href = "#!"; loc.textContent = "定位到此点"; loc.style.fontSize = "12px";
    loc.addEventListener("click", function (e) { e.preventDefault(); if (map) map.easeTo({ center: [pt.lng, pt.lat], zoom: Math.max(map.getZoom(), 6), duration: 600 }); });
    frag.appendChild(loc);
    return frag;
  }

  function linkEditor(lk) {
    ensureLink(lk);
    var Lb = ensureLinkLabel(lk);
    var frag = document.createDocumentFragment();
    var info = document.createElement("p"); info.className = "annot-hint";
    info.textContent = "连线（独立于点位，可自由微调起终点）";
    frag.appendChild(info);
    frag.appendChild(field("名称", txt(lk.name, function (v) { lk.name = v; renderLinks(); renderList(); scheduleSave(); })));
    frag.appendChild(inlineField("有向（箭头）", chk(lk.directed, function (v) { lk.directed = v; renderLinks(); renderEditor(); scheduleSave(); })));
    if (lk.directed) {
      frag.appendChild(field("箭头样式", sel(ARROW_STYLES, lk.arrowStyle, function (v) { lk.arrowStyle = v; renderLinks(); scheduleSave(); })));
      frag.appendChild(field("箭头大小", num(lk.arrowSize, 0.5, 3, 0.1, function (v) { lk.arrowSize = v; renderLinks(); scheduleSave(); })));
    }
    frag.appendChild(inlineField("虚线", chk(lk.dash, function (v) { lk.dash = v; renderLinks(); scheduleSave(); })));
    frag.appendChild(field("线型", sel([["straight", "直线"], ["arc", "弧线"]], lk.curve, function (v) {
      lk.curve = v; renderLinks(); renderEditor(); scheduleSave();
    })));
    if (lk.curve === "arc") {
      frag.appendChild(field("弧度（可正负，0=直线）", num(lk.bend, -1, 1, 0.05, function (v) { lk.bend = v; renderLinks(); scheduleSave(); })));
    }
    frag.appendChild(field("颜色", col(lk.color, function (v) { lk.color = v; renderLinks(); renderList(); scheduleSave(); })));
    frag.appendChild(field("线宽", num(lk.width, 0.5, 8, 0.5, function (v) { lk.width = v; renderLinks(); scheduleSave(); })));

    frag.appendChild(inlineField("显示名称标注", chk(Lb.show, function (v) { Lb.show = v; renderLinks(); renderEditor(); scheduleSave(); })));
    if (Lb.show) {
      frag.appendChild(field("名称字号", num(Lb.fontSize, 9, 28, 1, function (v) { Lb.fontSize = v; renderLinks(); scheduleSave(); })));
      frag.appendChild(field("名称文字色", col(Lb.color, function (v) { Lb.color = v; renderLinks(); scheduleSave(); })));
      frag.appendChild(field("名称角度（配合连线走势）", num(Lb.angle, -180, 180, 1, function (v) { Lb.angle = v; renderLinks(); scheduleSave(); })));
      var alignLink = document.createElement("a");
      alignLink.href = "#!"; alignLink.textContent = "对齐连线方向"; alignLink.style.fontSize = "12px";
      alignLink.addEventListener("click", function (e) {
        e.preventDefault();
        Lb.angle = Math.round(bearingDeg(lk.from, lk.to));
        renderLinks(); renderEditor(); scheduleSave();
      });
      frag.appendChild(alignLink);
    }

    var onEndpoint = function () { renderLinks(); renderList(); renderLinkHandles(); scheduleSave(); };
    frag.appendChild(coordRowLL("起点", lk.from, onEndpoint));
    frag.appendChild(coordRowLL("终点", lk.to, onEndpoint));
    return frag;
  }

  // ============ 组（统一修改，不是样式叠层）============
  // 一个组 = 一批成员 id（点 + 线混装）+ 一份“最近一次统一设定的属性值”。
  // 调组里任一控件 → 立刻把该值写进组内每个成员自己的属性字段（点写点、线写线），
  // 不在渲染时做任何叠加/继承。之后单独改某个点 / 线也照常生效，反过来会让组值“过时”——
  // 这是允许的：两者没有优先级，组的意义只是“一次改一批”。组值只在打开组面板时用来回显
  // 与“应用到全部成员”，永远不会在加载时自动重放（否则组就变成了有优先级的样式层）。
  var GROUP_PROP_DEFAULTS = {
    markerColor: "#b8442e", labelColor: "#3a2a18", boxBg: "#f4ecd8", boxBorder: "#b89562",
    nameFontSize: 13, prefixBg: "#b8442e", prefixFg: "#ffffff", prefixFontSize: 12,
    linkColor: "#000000", linkWidth: 2, linkCurve: "straight", arrowStyle: "triangle", arrowSize: 1,
  };
  var GROUP_POINT_APPLY = {
    markerColor: function (p, v) { p.markerColor = v; },
    labelColor: function (p, v) { ensureLabel(p).color = v; },
    boxBg: function (p, v) { ensureLabel(p).box.bg = v; },
    boxBorder: function (p, v) { ensureLabel(p).box.border = v; },
    nameFontSize: function (p, v) { ensureLabel(p).fontSize = v; },
    prefixBg: function (p, v) { ensureLabel(p).prefix.bg = v; },
    prefixFg: function (p, v) { ensureLabel(p).prefix.fg = v; },
    prefixFontSize: function (p, v) { ensureLabel(p).prefix.fontSize = v; },
  };
  var GROUP_LINK_APPLY = {
    linkColor: function (l, v) { l.color = v; },
    linkWidth: function (l, v) { l.width = v; },
    linkCurve: function (l, v) { l.curve = v; },
    arrowStyle: function (l, v) { l.arrowStyle = v; },
    arrowSize: function (l, v) { l.arrowSize = v; },
  };

  function ensureGroups() {
    if (!preset.annotations.groups) preset.annotations.groups = [];
    preset.annotations.groups.forEach(ensureGroup);
    return preset.annotations.groups;
  }
  function ensureGroup(g) {
    if (!g.id) g.id = uid("g_");
    if (!g.name) g.name = "未命名组";
    if (!Array.isArray(g.members)) g.members = [];
    g.props = g.props || {};
    Object.keys(GROUP_PROP_DEFAULTS).forEach(function (k) {
      if (g.props[k] == null) g.props[k] = GROUP_PROP_DEFAULTS[k];
    });
    return g;
  }
  function groupById(id) { return (preset.annotations.groups || []).find(function (g) { return g.id === id; }); }
  // 一个点/线当前所属的那个组（最多一个），没有就是 undefined——用来在勾选式的成员选择
  // 界面里把“已经在别的组里”的条目从候选列表里过滤掉（新建组/组属性面板都只列本组成员+
  // 未分组条目），不让勾选框直接把它偷过来（拖拽排序仍然可以跨组移动）。
  function groupOfMember(id) {
    return (preset.annotations.groups || []).find(function (g) { return g.members.indexOf(id) >= 0; });
  }
  function groupMemberPoints(g) { return preset.annotations.points.filter(function (p) { return g.members.indexOf(p.id) >= 0; }); }
  function groupMemberLinks(g) { return preset.annotations.links.filter(function (l) { return g.members.indexOf(l.id) >= 0; }); }
  function pruneGroupMember(id) {
    (preset.annotations.groups || []).forEach(function (g) {
      g.members = g.members.filter(function (m) { return m !== id; });
    });
  }
  function isPointId(id) { return typeof id === "string" && id.slice(0, 2) === "p_"; }
  function isLinkId(id) { return typeof id === "string" && id.slice(0, 2) === "l_"; }

  // ============ 列表顶层顺序（点 / 线 / 组混排 + 拖拽排序）============
  // preset.annotations.order：顶层条目 id 数组，元素是「组 id」或「未分组的点/线 id」。
  // 已被某个组吞掉的点/线不出现在这里——它们的先后顺序由所在组的 members 数组决定。
  // 这个数组具备自愈能力：每次 renderList() 前都会 ensureOrder() 一次，
  //   ①去掉已经不存在、或已被分组吞掉的 id ②把新出现但还没登记的点/线/组追加到末尾。
  // 所以新建点/线/组、解散组之后的“归还未分组”都不需要在各自的创建/删除逻辑里手动维护它。
  function ensureOrder() {
    if (!Array.isArray(preset.annotations.order)) {
      var grouped0 = {};
      (preset.annotations.groups || []).forEach(function (g) { g.members.forEach(function (m) { grouped0[m] = true; }); });
      var order0 = (preset.annotations.groups || []).map(function (g) { return g.id; });
      preset.annotations.points.forEach(function (p) { if (!grouped0[p.id]) order0.push(p.id); });
      preset.annotations.links.forEach(function (l) { if (!grouped0[l.id]) order0.push(l.id); });
      preset.annotations.order = order0;
    }
    var grouped = {};
    (preset.annotations.groups || []).forEach(function (g) { g.members.forEach(function (m) { grouped[m] = true; }); });
    preset.annotations.order = preset.annotations.order.filter(function (id) {
      if (groupById(id)) return true;
      if (grouped[id]) return false;
      return !!(pointById(id) || linkById(id));
    });
    var seen = {};
    preset.annotations.order.forEach(function (id) { seen[id] = true; });
    (preset.annotations.groups || []).forEach(function (g) { if (!seen[g.id]) { preset.annotations.order.push(g.id); seen[g.id] = true; } });
    preset.annotations.points.forEach(function (p) { if (!grouped[p.id] && !seen[p.id]) { preset.annotations.order.push(p.id); seen[p.id] = true; } });
    preset.annotations.links.forEach(function (l) { if (!grouped[l.id] && !seen[l.id]) { preset.annotations.order.push(l.id); seen[l.id] = true; } });
    return preset.annotations.order;
  }
  // 顶层重新定位：把 id 挪到 beforeId 前面（beforeId 为空则放末尾）
  function moveTopLevel(id, beforeId) {
    var order = preset.annotations.order;
    var oi = order.indexOf(id);
    if (oi >= 0) order.splice(oi, 1);
    var ai = beforeId ? order.indexOf(beforeId) : -1;
    if (ai >= 0) order.splice(ai, 0, id);
    else order.push(id);
  }
  // “插到 refId 后面”的锚点，但先假装把 excludeId（通常是正在拖的那个 id 自己）从
  // list 里摘掉再算——不然「把 X 拖到紧跟在它原本后一位的 Y 后面」这种几乎等于不移动的
  // 操作，会因为“Y 后面那个就是 X 自己”而把 X 错误地弹到整个列表末尾。
  function anchorAfterExcluding(list, refId, excludeId) {
    var filtered = list.filter(function (x) { return x !== excludeId; });
    var i = filtered.indexOf(refId);
    return i >= 0 && i + 1 < filtered.length ? filtered[i + 1] : null;
  }
  // 单一归属：把 id 从所有组里摘出来，再按需加入目标组（targetGroupId 为空则只是变成未分组，
  // 顶层 order 的插入位置由调用方另外处理）。加入/移出组本身不改任何属性值——组的属性只在
  // setGroupProp / applyAllGroupProps 里写，membership 变化永远不触碰 markerColor 等字段。
  function moveToGroup(id, targetGroupId, insertBeforeMemberId) {
    (preset.annotations.groups || []).forEach(function (g) {
      var i = g.members.indexOf(id);
      if (i >= 0) g.members.splice(i, 1);
    });
    var oi = preset.annotations.order.indexOf(id);
    if (oi >= 0) preset.annotations.order.splice(oi, 1);
    if (targetGroupId) {
      var g2 = groupById(targetGroupId);
      if (!g2) return;
      var idx = insertBeforeMemberId ? g2.members.indexOf(insertBeforeMemberId) : -1;
      if (idx >= 0) g2.members.splice(idx, 0, id);
      else g2.members.push(id);
    }
  }

  // ============ 新建组：先选子条目，不允许空组 ============
  var pendingGroupPick = null;   // null = 未在建组；否则是正在勾选的点/线 id 数组
  function startCreateGroup() {
    pendingGroupPick = [];
    renderList();
  }
  function cancelCreateGroup() {
    pendingGroupPick = null;
    renderList();
  }
  function confirmCreateGroup() {
    if (!pendingGroupPick || !pendingGroupPick.length) { toast("请至少选择一个点或线", "orange darken-2"); return; }
    ensureGroups();
    ensureOrder();
    var g = ensureGroup({ id: uid("g_"), name: "组 " + (preset.annotations.groups.length + 1), members: [], props: {} });
    preset.annotations.groups.push(g);
    // 组头插在被选中的这批条目里、原本顶层位置最靠前的那个位置，观感上更符合直觉
    var order = preset.annotations.order, firstIdx = order.length;
    pendingGroupPick.forEach(function (id) {
      var i = order.indexOf(id);
      if (i >= 0 && i < firstIdx) firstIdx = i;
    });
    order.splice(Math.min(firstIdx, order.length), 0, g.id);
    pendingGroupPick.forEach(function (id) { moveToGroup(id, g.id); });
    pendingGroupPick = null;
    selectedId = g.id;
    var head = document.getElementById("annot-editor-head");
    if (head && head.classList.contains("collapsed")) head.click();   // 展开属性面板
    renderList(); renderEditor(); renderPoints(); scheduleSave();
    toast("已新建组", "teal");
  }
  function removeGroup(id) {
    ensureGroups();
    ensureOrder();
    var g = groupById(id);
    if (g) {
      // 解散后子条目回到未分组状态，插回组头原来的顶层位置，保持相对顺序不被打乱
      var pos = preset.annotations.order.indexOf(id);
      var kids = g.members.slice();
      preset.annotations.order = preset.annotations.order.filter(function (oid) { return oid !== id; });
      if (pos < 0) pos = preset.annotations.order.length;
      pos = Math.min(pos, preset.annotations.order.length);
      Array.prototype.splice.apply(preset.annotations.order, [pos, 0].concat(kids));
    }
    preset.annotations.groups = preset.annotations.groups.filter(function (gg) { return gg.id !== id; });
    if (selectedId === id) selectedId = null;
    renderEditor(); renderList(); renderPoints(); scheduleSave();
  }

  // 改一个组属性：写进 g.props，并立刻下发到当前所有成员的自有字段
  function setGroupProp(g, key, v) {
    g.props[key] = v;
    var pf = GROUP_POINT_APPLY[key], lf = GROUP_LINK_APPLY[key];
    if (pf) groupMemberPoints(g).forEach(function (p) { pf(p, v); });
    if (lf) groupMemberLinks(g).forEach(function (l) { lf(l, v); });
    renderPoints(); renderLinks(); renderList(); scheduleSave();
  }
  // 把组里所有属性一次性重放到全部成员（用于成员被单独改过之后“拉齐”，或新加成员后同步）
  function applyAllGroupProps(g) {
    Object.keys(GROUP_POINT_APPLY).forEach(function (key) {
      groupMemberPoints(g).forEach(function (p) { GROUP_POINT_APPLY[key](p, g.props[key]); });
    });
    Object.keys(GROUP_LINK_APPLY).forEach(function (key) {
      groupMemberLinks(g).forEach(function (l) { GROUP_LINK_APPLY[key](l, g.props[key]); });
    });
    renderPoints(); renderLinks(); renderList(); renderEditor(); scheduleSave();
    toast("已把组属性应用到全部成员", "teal");
  }
  // 组内是否存在“成员当前值 ≠ 组设定值”（成员被单独改过）
  function groupDivergence(g) {
    var diff = false;
    groupMemberPoints(g).forEach(function (p) {
      var L = ensureLabel(p);
      if (p.markerColor !== g.props.markerColor) diff = true;
      if (L.color !== g.props.labelColor) diff = true;
      if (L.box.bg !== g.props.boxBg) diff = true;
      if (L.box.border !== g.props.boxBorder) diff = true;
      if (Number(L.fontSize) !== Number(g.props.nameFontSize)) diff = true;
      if (L.prefix.bg !== g.props.prefixBg) diff = true;
      if (L.prefix.fg !== g.props.prefixFg) diff = true;
      if (Number(L.prefix.fontSize) !== Number(g.props.prefixFontSize)) diff = true;
    });
    groupMemberLinks(g).forEach(function (l) {
      if (l.color !== g.props.linkColor) diff = true;
      if (Number(l.width) !== Number(g.props.linkWidth)) diff = true;
      if ((l.curve || "straight") !== g.props.linkCurve) diff = true;
      if ((l.arrowStyle || "triangle") !== g.props.arrowStyle) diff = true;
      if (Number(l.arrowSize || 1) !== Number(g.props.arrowSize)) diff = true;
    });
    return diff;
  }

  function groupMemberRow(g, id, color, text) {
    var row = document.createElement("div");
    row.className = "grp-mem";
    // 调用方（groupEditor）已经把「属于别的组」的条目过滤掉了，这里能看到的只有本组成员
    // 和未分组条目，所以勾选框始终可操作：勾上＝加入本组，取消＝退回未分组。
    var mine = !!groupOfMember(id);
    row.appendChild(chk(mine, function (on) {
      if (on) {
        moveToGroup(id, g.id);
      } else {
        moveToGroup(id, null);
        var order = preset.annotations.order;
        var gi = order.indexOf(g.id);
        if (gi >= 0) order.splice(gi + 1, 0, id); else order.push(id);
      }
      renderList(); renderPoints(); renderLinks(); renderLinkHandles(); renderEditor();
      scheduleSave();
    }, false));
    var sw = document.createElement("span"); sw.className = "swatch"; sw.style.background = color;
    var nm = document.createElement("span"); nm.className = "grp-mem-nm"; nm.textContent = text;
    row.appendChild(sw); row.appendChild(nm);
    return row;
  }

  function groupEditor(g) {
    ensureGroup(g);
    var frag = document.createDocumentFragment();

    var info = document.createElement("p"); info.className = "annot-hint";
    info.textContent = "组 = 统一修改：调下面任一项，立刻写入组内所有成员。之后单独改某个点 / 线也行，两者互不覆盖。";
    frag.appendChild(info);

    frag.appendChild(field("组名", txt(g.name, function (v) { g.name = v; renderList(); scheduleSave(); })));

    // 成员选择
    var mh = document.createElement("div"); mh.className = "grp-sub";
    var mhl = document.createElement("span"); mhl.textContent = "成员（勾选加入）";
    var mhc = document.createElement("span"); mhc.className = "grp-mem-count"; mhc.textContent = g.members.length + " 个成员";
    mh.appendChild(mhl); mh.appendChild(mhc);
    frag.appendChild(mh);

    var memWrap = document.createElement("div"); memWrap.className = "grp-members";
    // 只列「本组成员」+「未分配到任何组」的点/线——已经在别的组里的条目不出现在这里，
    // 列表不会因为别的组的成员而变长。要把别的组的成员转到这个组，先去那个组的面板取消勾选
    // （变成未分组）再回来勾选，或者直接用拖拽跨组移动。
    var pts = preset.annotations.points.filter(function (p) { var o = groupOfMember(p.id); return !o || o.id === g.id; });
    var lks = preset.annotations.links.filter(function (l) { var o = groupOfMember(l.id); return !o || o.id === g.id; });
    if (!pts.length && !lks.length) {
      memWrap.innerHTML = '<p class="annot-hint" style="margin:6px 8px;">还没有可选的点或线（未分组的都已在本组，或还没创建）。</p>';
    } else {
      pts.forEach(function (p) {
        memWrap.appendChild(groupMemberRow(g, p.id, p.markerColor || tierColor(p.tier), "● " + (p.name || "地点")));
      });
      lks.forEach(function (l) {
        ensureLink(l);
        memWrap.appendChild(groupMemberRow(g, l.id, l.color || "#000000", (l.directed ? "→ " : "— ") + (l.name || fmtLL(l.from))));
      });
    }
    frag.appendChild(memWrap);

    var np = groupMemberPoints(g).length, nl = groupMemberLinks(g).length;

    var sp = document.createElement("div"); sp.className = "grp-sub";
    sp.textContent = "点位公共属性" + (np ? "（作用于 " + np + " 个点）" : "（组内暂无点）");
    frag.appendChild(sp);
    frag.appendChild(field("颜色", col(g.props.markerColor, function (v) { setGroupProp(g, "markerColor", v); })));
    frag.appendChild(field("名称文字色", col(g.props.labelColor, function (v) { setGroupProp(g, "labelColor", v); })));
    frag.appendChild(field("信息框底色", col(g.props.boxBg, function (v) { setGroupProp(g, "boxBg", v); })));
    frag.appendChild(field("信息框边线色", col(g.props.boxBorder, function (v) { setGroupProp(g, "boxBorder", v); })));
    frag.appendChild(field("名称字号", num(g.props.nameFontSize, 9, 28, 1, function (v) { setGroupProp(g, "nameFontSize", v); })));
    frag.appendChild(field("前缀底色", col(g.props.prefixBg, function (v) { setGroupProp(g, "prefixBg", v); })));
    frag.appendChild(field("前缀文字色", col(g.props.prefixFg, function (v) { setGroupProp(g, "prefixFg", v); })));
    frag.appendChild(field("前缀字号", num(g.props.prefixFontSize, 9, 24, 1, function (v) { setGroupProp(g, "prefixFontSize", v); })));

    var sl = document.createElement("div"); sl.className = "grp-sub";
    sl.textContent = "连线公共属性" + (nl ? "（作用于 " + nl + " 条线）" : "（组内暂无线）");
    frag.appendChild(sl);
    frag.appendChild(field("连线颜色", col(g.props.linkColor, function (v) { setGroupProp(g, "linkColor", v); })));
    frag.appendChild(field("连线线宽", num(g.props.linkWidth, 0.5, 8, 0.5, function (v) { setGroupProp(g, "linkWidth", v); })));
    frag.appendChild(field("连线线形", sel([["straight", "直线"], ["arc", "弧线"]], g.props.linkCurve, function (v) { setGroupProp(g, "linkCurve", v); })));
    frag.appendChild(field("箭头样式", sel(ARROW_STYLES, g.props.arrowStyle, function (v) { setGroupProp(g, "arrowStyle", v); })));
    frag.appendChild(field("箭头大小", num(g.props.arrowSize, 0.5, 3, 0.1, function (v) { setGroupProp(g, "arrowSize", v); })));

    var applyBtn = document.createElement("button");
    applyBtn.type = "button"; applyBtn.className = "grp-apply-btn";
    applyBtn.textContent = "把以上全部应用到组内成员";
    applyBtn.addEventListener("click", function () { applyAllGroupProps(g); });
    frag.appendChild(applyBtn);

    if (groupDivergence(g)) {
      var warn = document.createElement("p"); warn.className = "annot-hint annot-danger";
      warn.textContent = "组内部分成员被单独改过，当前值与上面的组设定不一致。点上面的按钮可把它们拉齐。";
      frag.appendChild(warn);
    }

    var delBtn = document.createElement("button");
    delBtn.type = "button"; delBtn.className = "annot-del-btn grp-dissolve-btn";
    delBtn.textContent = "解散该组（点 / 线保留）";
    delBtn.addEventListener("click", function () {
      if (confirm("解散组「" + (g.name || "未命名组") + "」？组内的点和线本身都会保留。")) removeGroup(g.id);
    });
    frag.appendChild(delBtn);

    return frag;
  }

  // ---- 列表（点 / 线 / 组统一按 preset.annotations.order 顺序渲染，支持拖拽排序）----
  var collapsedGroups = {};   // groupId -> bool，纯前端视图状态，不进 preset（不占版本快照）
  var dragRow = null;         // { id, isGroup }：正在拖拽的顶层条目或子条目

  function pickCheckbox(id) {
    // 新建组的候选列表（见 renderList）已经只剩未分组的点/线，这里不会遇到已经属于
    // 别的组的条目，勾选框始终可操作。
    return chk(pendingGroupPick.indexOf(id) >= 0, function (on) {
      var i = pendingGroupPick.indexOf(id);
      if (on && i < 0) pendingGroupPick.push(id);
      else if (!on && i >= 0) pendingGroupPick.splice(i, 1);
      updatePickCount();
    }, false);
  }
  function updatePickCount() {
    var el = document.getElementById("annot-group-pick-count");
    if (el) el.textContent = "已选 " + (pendingGroupPick ? pendingGroupPick.length : 0) + " 个";
  }

  function clearDragMarks() {
    document.querySelectorAll(".annot-list-item.drag-over-before,.annot-list-item.drag-over-after").forEach(function (r) {
      r.classList.remove("drag-over-before", "drag-over-after");
    });
  }
  // 拖拽落到 row（对应 target.id / isGroup / groupId(子条目所属组) / before(松手时鼠标在上半还是下半)）
  function wireRowDrag(row, id, isGroup, groupId) {
    row.addEventListener("dragstart", function (e) {
      if (pendingGroupPick) { e.preventDefault(); return; }
      dragRow = { id: id, isGroup: isGroup };
      row.classList.add("dragging");
      try { e.dataTransfer.effectAllowed = "move"; e.dataTransfer.setData("text/plain", id); } catch (err) {}
    });
    row.addEventListener("dragend", function () {
      row.classList.remove("dragging");
      clearDragMarks();
      dragRow = null;
    });
    row.addEventListener("dragover", function (e) {
      if (!dragRow || dragRow.id === id) return;
      e.preventDefault();
      var rect = row.getBoundingClientRect();
      var before = (e.clientY - rect.top) < rect.height / 2;
      clearDragMarks();
      row.classList.add(before ? "drag-over-before" : "drag-over-after");
    });
    row.addEventListener("drop", function (e) {
      if (!dragRow || dragRow.id === id) return;
      e.preventDefault();
      var rect = row.getBoundingClientRect();
      var before = (e.clientY - rect.top) < rect.height / 2;
      var kind = isGroup ? "group" : (groupId ? "child" : "ungrouped");
      handleRowDrop(dragRow.id, dragRow.isGroup, { kind: kind, id: id, groupId: groupId || null, before: before });
      clearDragMarks();
    });
  }
  // 落点语义：
  // - 拖的是组：只能在顶层挪位置（落在子条目上时，换算成落在其所属组块的边界）
  // - 拖的是点/线，落在子条目上：加入（或留在）那条子条目所属的组，插到它前/后
  // - 拖的是点/线，落在组头上：上半＝变成未分组、插到该组前面；下半＝加入该组，成为第一个子条目
  // - 拖的是点/线，落在未分组条目上：变成未分组，插到该条目前/后
  // 加入/离开组本身绝不写任何组公共属性——membership 和属性是两回事。
  function handleRowDrop(dragId, dragIsGroup, target) {
    if (dragIsGroup) {
      var anchorId = target.kind === "child" ? target.groupId : target.id;
      if (anchorId === dragId) return;
      var beforeAnchor = target.kind === "child" ? true : target.before;
      moveTopLevel(dragId, beforeAnchor ? anchorId : anchorAfterExcluding(preset.annotations.order, anchorId, dragId));
    } else if (target.kind === "child") {
      var g0 = groupById(target.groupId);
      moveToGroup(dragId, target.groupId, target.before ? target.id : (g0 && anchorAfterExcluding(g0.members, target.id, dragId)));
    } else if (target.kind === "group") {
      if (target.before) {
        moveToGroup(dragId, null);
        moveTopLevel(dragId, target.id);
      } else {
        var g = groupById(target.id);
        moveToGroup(dragId, target.id, g && g.members.length ? g.members[0] : null);
      }
    } else {
      moveToGroup(dragId, null);
      moveTopLevel(dragId, target.before ? target.id : anchorAfterExcluding(preset.annotations.order, target.id, dragId));
    }
    renderList(); renderPoints(); renderLinks(); renderLinkHandles(); scheduleSave();
  }

  function buildPointRow(p, isChild, groupId) {
    var row = document.createElement("div");
    row.className = "annot-list-item" + (isChild ? " child-row" : "") + (p.id === selectedId ? " sel" : "");
    row.draggable = !pendingGroupPick;
    if (pendingGroupPick) row.appendChild(pickCheckbox(p.id));
    var sw = document.createElement("span"); sw.className = "swatch"; sw.style.background = p.markerColor || tierColor(p.tier);
    var nm = document.createElement("span"); nm.className = "nm"; nm.textContent = "● " + (p.name || "地点");
    row.appendChild(sw); row.appendChild(nm);
    if (!pendingGroupPick) {
      var del = document.createElement("button"); del.type = "button"; del.className = "annot-del-btn"; del.textContent = "删除";
      del.addEventListener("click", function (e) { e.preventDefault(); e.stopPropagation(); if (confirm("删除点「" + (p.name || "地点") + "」？")) removePoint(p.id); });
      row.appendChild(del);
    }
    row.addEventListener("click", function () {
      if (pendingGroupPick) return;
      selectedId = p.id; renderEditor(); renderList(); renderPoints();
      if (map) map.easeTo({ center: [p.lng, p.lat], zoom: Math.max(map.getZoom(), 6), duration: 500 });
    });
    wireRowDrag(row, p.id, false, groupId);
    return row;
  }
  function buildLinkRow(l, isChild, groupId) {
    ensureLink(l);
    var row = document.createElement("div");
    row.className = "annot-list-item" + (isChild ? " child-row" : "") + (l.id === selectedId ? " sel" : "");
    row.draggable = !pendingGroupPick;
    if (pendingGroupPick) row.appendChild(pickCheckbox(l.id));
    var sw = document.createElement("span"); sw.className = "swatch"; sw.style.background = l.color || "#000000";
    var nm = document.createElement("span"); nm.className = "nm"; nm.textContent = (l.directed ? "→ " : "— ") + (l.name || fmtLL(l.from) + " ⇢ " + fmtLL(l.to));
    row.appendChild(sw); row.appendChild(nm);
    if (!pendingGroupPick) {
      var del = document.createElement("button"); del.type = "button"; del.className = "annot-del-btn"; del.textContent = "删除";
      del.addEventListener("click", function (e) { e.preventDefault(); e.stopPropagation(); removeLink(l.id); });
      row.appendChild(del);
    }
    row.addEventListener("click", function () {
      if (pendingGroupPick) return;
      selectedId = l.id; renderEditor(); renderList();
    });
    wireRowDrag(row, l.id, false, groupId);
    return row;
  }
  function buildGroupRow(g) {
    var row = document.createElement("div");
    row.className = "annot-list-item group-row" + (g.id === selectedId ? " sel" : "");
    row.draggable = !pendingGroupPick;
    var collapsed = !!collapsedGroups[g.id];
    var cbtn = document.createElement("button");
    cbtn.type = "button"; cbtn.className = "collapse-btn grp-collapse-btn" + (collapsed ? " collapsed" : "");
    cbtn.title = "折叠 / 展开该组"; cbtn.setAttribute("aria-label", "折叠或展开该组"); cbtn.textContent = "▾";
    cbtn.addEventListener("click", function (e) {
      e.preventDefault(); e.stopPropagation();
      collapsedGroups[g.id] = !collapsedGroups[g.id];
      renderList();
    });
    row.appendChild(cbtn);
    var nm = document.createElement("span"); nm.className = "nm";
    nm.textContent = "▣ " + (g.name || "未命名组") + " · " + g.members.length + " 个成员";
    row.appendChild(nm);
    if (!pendingGroupPick) {
      var del = document.createElement("button");
      del.type = "button"; del.className = "annot-del-btn"; del.textContent = "解散";
      del.addEventListener("click", function (e) {
        e.preventDefault(); e.stopPropagation();
        if (confirm("解散组「" + (g.name || "未命名组") + "」？组内的点和线本身都会保留。")) removeGroup(g.id);
      });
      row.appendChild(del);
    }
    row.addEventListener("click", function (e) {
      if (pendingGroupPick || e.target === cbtn) return;
      selectedId = g.id; renderEditor(); renderList(); renderPoints();
    });
    wireRowDrag(row, g.id, true, null);
    return row;
  }

  function renderList() {
    ensureOrder();
    var box = document.getElementById("annot-list");
    box.innerHTML = "";
    // 加点 Tab 只列点位、连线 Tab 只列连线；选择 Tab（两者都不是）两种都列
    var showPoints = annotMode !== "add-link";
    var showLinks = annotMode !== "add-point";
    var onlySelectTab = showPoints && showLinks;

    var pickBar = document.getElementById("annot-group-pick-bar");
    if (pickBar) pickBar.style.display = pendingGroupPick ? "" : "none";
    updatePickCount();

    var renderedAny = false;
    preset.annotations.order.forEach(function (id) {
      var g = groupById(id);
      if (g) {
        // 新建组只能从未分组的点/线里选——已有的组和它们的成员在这个流程里完全不出现，
        // 列表不会被别的组的成员撑长。想把别的组的成员拉进新组，先去那个组的面板取消勾选。
        if (pendingGroupPick) return;
        var kids = g.members.filter(function (mid) {
          if (isPointId(mid)) return showPoints && pointById(mid);
          if (isLinkId(mid)) return showLinks && linkById(mid);
          return false;
        });
        if (!onlySelectTab && !kids.length) return;   // 加点/连线 Tab 下，组内没有匹配类型成员就不占位
        box.appendChild(buildGroupRow(g));
        renderedAny = true;
        if (!collapsedGroups[g.id]) {
          kids.forEach(function (mid) {
            box.appendChild(isPointId(mid) ? buildPointRow(pointById(mid), true, g.id) : buildLinkRow(linkById(mid), true, g.id));
          });
        }
      } else if (isPointId(id)) {
        if (showPoints && pointById(id)) { box.appendChild(buildPointRow(pointById(id), false, null)); renderedAny = true; }
      } else if (isLinkId(id)) {
        if (showLinks && linkById(id)) { box.appendChild(buildLinkRow(linkById(id), false, null)); renderedAny = true; }
      }
    });

    if (!renderedAny) {
      var msg = onlySelectTab ? "还没有点或线。" : (showPoints ? "还没有点位。" : "还没有连线。");
      var hint = document.createElement("p"); hint.className = "annot-hint"; hint.textContent = msg;
      box.appendChild(hint);
    }
  }

  // ============ 拆出（region isolate）—— 复用 MapStage 的 AntiqueIsolateWorkbench ============
  // 集成路径逐字参考 MapStage/mapstage/tuner/index.html:setupGlobeAndIsolate()。
  // 需要浏览器实测：patched maplibre 构建 + terrain-island.js + region-isolate*.js。
  function populateIsolateRegions() {
    var sel = document.getElementById("isolate-region");
    if (!sel || !window.REGION_ISOLATE_DATA || !window.REGION_ISOLATE) return;
    var regions = REGION_ISOLATE.listRegions(REGION_ISOLATE_DATA) || [];
    if (!regions.length) return;
    sel.innerHTML = "";
    regions.forEach(function (r) {
      var op = document.createElement("option");
      op.value = r.id;
      op.textContent = r.label || r.id;
      sel.appendChild(op);
    });
    sel.value = preset.style.basemap.isolateRegion || REGION_ISOLATE_DATA.defaultId || "china";
  }

  function setupIsolate() {
    if (!window.AntiqueIsolateWorkbench) return;
    var s = preset.style;
    var wantIsolate = !!s.basemap.isolate;
    try {
      // 不传 selectEl：否则 workbench 自己绑的 change 会带 frame:true 去重新框选区域、改视角。
      // enabled 一律传 false：让 start() 不触发 frameRegion；随后按需 setEnabled(true, false) 无视角变化地开启。
      isolateCtl = AntiqueIsolateWorkbench.mount({
        map: map,
        maplibregl: maplibregl,
        FX: window.AntiqueMapFx,
        getViewMode: function () { return preset.style.view; },
        toast: function (m) { toast(m); },
        wrapEl: document.getElementById("maplibre-map"),
        enabled: false,
        regionId: s.basemap.isolateRegion || "china",
        sideColor: "#6d4a36",
        onNeedMap: function () { setView("map"); },
        onChange: function (st) {
          var newIso = !!(st && st.enabled);
          var newReg = (st && st.regionId) || "china";
          var changed = newIso !== preset.style.basemap.isolate || newReg !== preset.style.basemap.isolateRegion;
          preset.style.basemap.isolate = newIso;
          preset.style.basemap.isolateRegion = newReg;
          setToggle("tg-isolate", newIso);
          document.getElementById("isolate-region-wrap").style.display = newIso ? "" : "none";
          if (changed) scheduleSave();
        },
      });
      isolateCtl.start();

      var applyWanted = function () {
        populateIsolateRegions();
        if (wantIsolate && isolateCtl) isolateCtl.setEnabled(true, false); // frame=false → 不改视角
      };
      if (AntiqueIsolateWorkbench.ensureIsolateData) {
        AntiqueIsolateWorkbench.ensureIsolateData().then(applyWanted).catch(function () {});
      } else {
        applyWanted();
      }

      // 自己接管区域下拉：切换区域也不重新框选（frame:false）
      var regSel = document.getElementById("isolate-region");
      if (regSel) {
        regSel.addEventListener("change", function () {
          preset.style.basemap.isolateRegion = regSel.value;
          if (isolateCtl) isolateCtl.setRegion(regSel.value, { frame: false, enable: true });
          scheduleSave();
        });
      }
    } catch (e) {
      console.warn("[map] isolate init failed", e);
      warnOnce("iso", "拆出功能初始化失败（第三方组件），其余功能不受影响");
    }
  }
})();
