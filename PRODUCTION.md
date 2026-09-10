# 生产环境部署注意事项

> 每次往生产环境推送前，对照本清单。Claude 在被要求"推送生产环境"时应主动把本文件相关项再念一遍。

## 1. 作图模块 —— 长耗时出图请求（重要）

作图走第三方中转（`CODEX_BASE_URL`），`gpt-image-1` 出图常见 **1–3 分钟，偶尔更久**。相关配置：

- **`.env`**：`CODEX_IMAGE_TIMEOUT`（秒，默认 `300`）—— `backend/image_gen.py` 里 httpx 的 read 超时。
- **gunicorn worker 超时**：默认 `--timeout 30` 会在出图完成前把 worker 杀掉。启动命令必须显式加大，且要 **大于 `CODEX_IMAGE_TIMEOUT`**：

  ```bash
  gunicorn main:app -w 4 -k uvicorn.workers.UvicornWorker \
    --bind 0.0.0.0:8000 --timeout 360 --graceful-timeout 30
  ```

  > 端口号（此处 `8000`）只是示例，按部署环境实际使用的端口填写即可（本机测试常见 `8080`/`8000` 都在用），和功能无关，不需要和文档强绑定。

- **反向代理（Nginx 等）**：`proxy_read_timeout` / `proxy_send_timeout` 也要 ≥ `CODEX_IMAGE_TIMEOUT`（建议 `360s`），否则 502 依旧来自代理层：

  ```nginx
  location / {
      proxy_pass http://127.0.0.1:8000;  # 端口需与 gunicorn --bind 保持一致，按实际部署端口调整
      proxy_read_timeout 360s;
      proxy_send_timeout 360s;
  }
  ```

  > 注：出图已改成"提交即返回 + 前端轮询"（见下），`POST /drawing/styles/{id}/generate` 本身很快返回。真正长耗时的是 FastAPI `BackgroundTasks` 里的出图调用，它不占 HTTP 连接，但仍受 `CODEX_IMAGE_TIMEOUT` 约束。代理/worker 超时主要影响写作模块的 Codex 排版（那个仍是同步长请求）。

## 2. 作图出图是后台任务，进程重启会丢

`_run_generation_job` 用 FastAPI `BackgroundTasks` 在同一进程内跑。若在出图过程中重启/部署，该条 `drawing_generations` 会永远停在 `status='processing'`，前端轮询 12 分钟后放弃并提示刷新。

- 部署尽量避开有 `processing` 记录的时段；或部署后手动把残留 `processing` 置为 `failed`：
  ```sql
  UPDATE drawing_generations
     SET status='failed', error_msg='服务重启导致本次出图中断，请重试'
   WHERE status IN ('processing','pending') AND created_at < NOW() - INTERVAL '15 minutes';
  ```
- 后续可考虑接入 Celery（`backend/celery_app.py` 已有骨架）把出图做成真正的持久化任务队列。

## 3. 第三方中转（`gpt.hinature.cn`）健康检查

作图 + 写作排版都依赖这个中转。它挂过一次（images 和 chat 全线 404 `Upstream request failed`）。随时自检：

```bash
# 从项目根跑，自动读 .env 里的 key
KEY=$(grep '^CODEX_API_KEY=' .env | cut -d= -f2-)

# 出图端点（成功 = HTTP 200 且响应里有 data[0].b64_json；耗时可能 1-3 分钟）
curl -sS --max-time 240 -w '\nHTTP %{http_code}  time=%{time_total}s\n' \
  -X POST "$(grep '^CODEX_BASE_URL=' .env | cut -d= -f2-)/images/generations" \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"model":"gpt-image-1","prompt":"a red apple on a table","n":1,"size":"1024x1024"}' | tail -c 400

# chat 端点（写作排版用）
curl -sS -w '\nHTTP %{http_code}\n' \
  -X POST "$(grep '^CODEX_BASE_URL=' .env | cut -d= -f2-)/chat/completions" \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"model":"gpt-5.5","messages":[{"role":"user","content":"hi"}],"max_tokens":5}'
```

- 全线 4xx/5xx 且 body 是 `Upstream ...` → 中转的上游挂了，联系中转服务提供商，不是本站代码问题。
- 前端此时会给出明确提示（"第三方图像服务暂时不可用…这不是本站的问题"），并把该条生成记为 `failed`。

## 4. 地图模块（`/map/`）

- 地图模块的第三方（EOX 卫星 / Mapterhorn DEM / OpenFreeMap 矢量+字体）全部由**用户浏览器直连**，服务器不代理、无 key、无 `.env` 项。用户网络需能直连这些域名；个别用户不通时前端已有友好兜底提示，不需要运维处理。
- **`static/js/maplibre/maplibre-gl.js` 是 MapStage 的 patched 5.6.0 构建**（`__ANTIQUE_TERRAIN_CLIP_PATCH`，拆出功能依赖它）。`/map/` 整页锁死用这一份。**升级 MapLibre 时必须连带重新应用 / 评估这个地形裁剪补丁**，否则「拆出」会坏。来源见 `static/js/maplibre/mapstage/NOTICE.md`。
- 新表 `map_documents` / `map_preset_versions` + `users.can_map` 列由 `init_map_tables()` 幂等创建（startup 自动跑）；老库无需手动迁移。
- `static/maps/`（地图侧栏缩略图）已在 `.gitignore`，生产上是运行期生成目录。

## 5. 常规检查

- `.env` 的 `DATABASE_URL` 指向生产库（当前本机开发用 `localhost:5434`）。
- `SECRET_KEY` 生产用独立值（≥32 字符）。
- Cookie 已是 `secure=True`，生产必须走 HTTPS，否则登录 Cookie 不下发。
- 静态资源改动（`static/js/*`、`static/css/*`）上线后提醒用户强刷；或给静态资源加版本号/指纹。
- 数据库新表：`backend/db.py` 的 `init_phase3_tables` / `init_writing_tables` / `init_drawing_tables` 幂等，启动自动跑；老库补字段跑 `python -m scripts.migrate`。
