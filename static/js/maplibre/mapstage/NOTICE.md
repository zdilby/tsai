# Vendored from MapStage — DO NOT hand-edit

These files are copied **verbatim** from MapStage and are only used by TSAI's
地图模块 (`/map/`) for the "拆出" (region isolate) feature, which requires
MapLibre's antique terrain-clip patch and can't be done with stock MapLibre.

- Source repo: https://github.com/hopechen067/MapStage
- Copied: 2026-09-06, from commit `48eecae` (`mapstage/tuner/`)

| File | From | License |
|------|------|---------|
| `../maplibre-gl.js` | `vendor/maplibre-gl.js` (maplibre-gl 5.6.0-dev + `__ANTIQUE_TERRAIN_CLIP_PATCH`, minified) | BSD-3-Clause (MapLibre) |
| `../maplibre-gl.css` | unpkg `maplibre-gl@5.6.0/dist/maplibre-gl.css` | BSD-3-Clause (MapLibre) |
| `map-fx.js` | `assets/map-fx.js` — `window.AntiqueMapFx` | MIT (MapStage) |
| `vector-paint.js` | `assets/vector-paint.js` — `window.AntiqueVectorPaint` (color-relief / vector water paint) | MIT (MapStage) |
| `region-isolate.js` | `assets/region-isolate.js` — `window.REGION_ISOLATE` | MIT (MapStage) |
| `region-isolate-data.js` | `assets/region-isolate-data.js` — `window.REGION_ISOLATE_DATA` (~1.9 MB region polygons) | data: Natural Earth (public domain) + OSM extracts (ODbL 1.0) |
| `terrain-island.js` | `assets/terrain-island.js` — terrain clip + side wall (needs patched maplibre) | MIT (MapStage) |
| `isolate-workbench.js` | `assets/isolate-workbench.js` — `window.AntiqueIsolateWorkbench` (mount/start/setEnabled/setRegion/sync) | MIT (MapStage) |
| `polar-ice.geojson` | `assets/polar-ice.geojson` — polar ice fill for globe poles | Natural Earth (public domain) |

## Attribution (must show in the map's attribution control when isolate is on)

> Natural Earth (public domain) · © OpenStreetMap contributors (ODbL) — narrative silhouette, not an official boundary.

## Upgrade note

The `../maplibre-gl.js` here is a **patched 5.6.0**. The whole `/map/` page is
locked to this build. If MapLibre is ever upgraded, the terrain-clip patch must
be re-applied / re-evaluated against the new version before isolate will work.
See `PRODUCTION.md`.
