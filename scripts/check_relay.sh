#!/usr/bin/env bash
# 第三方中转（CODEX_BASE_URL）健康检查。从项目根运行：bash scripts/check_relay.sh
# 自动读取 .env 里的 CODEX_API_KEY / CODEX_BASE_URL，不用手动 export。
set -u
cd "$(dirname "$0")/.." || exit 1

KEY=$(grep -E '^CODEX_API_KEY=' .env | cut -d= -f2-)
BASE=$(grep -E '^CODEX_BASE_URL=' .env | cut -d= -f2-)
IMG_MODEL=$(grep -E '^CODEX_IMAGE_MODEL=' .env | cut -d= -f2- || echo gpt-image-1)
CHAT_MODEL=$(grep -E '^CODEX_MODEL=' .env | cut -d= -f2- || echo gpt-5.5)

echo "BASE = $BASE"
echo "KEY  = ${KEY:0:10}…${KEY: -6}  (len ${#KEY})"
echo

echo "── 1/3  GET /models ─────────────────────────────"
curl -sS -o /tmp/relay_models.json -w "HTTP %{http_code}  time=%{time_total}s\n" \
  "$BASE/models" -H "Authorization: Bearer $KEY"
grep -oE '"id"[: ]*"[^"]*"' /tmp/relay_models.json 2>/dev/null | grep -iE 'image|dall' | head
echo

echo "── 2/3  POST /chat/completions  ($CHAT_MODEL) ────"
curl -sS -o /tmp/relay_chat.json -w "HTTP %{http_code}  time=%{time_total}s\n" \
  -X POST "$BASE/chat/completions" \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d "{\"model\":\"$CHAT_MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}],\"max_tokens\":5}"
head -c 300 /tmp/relay_chat.json; echo; echo

echo "── 3/3  POST /images/generations  ($IMG_MODEL, 可能 1-3 分钟) ──"
curl -sS --max-time 240 -o /tmp/relay_img.json -w "HTTP %{http_code}  time=%{time_total}s\n" \
  -X POST "$BASE/images/generations" \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d "{\"model\":\"$IMG_MODEL\",\"prompt\":\"a red apple on a table\",\"n\":1,\"size\":\"1024x1024\"}"
python3 - <<'PY' 2>/dev/null || head -c 300 /tmp/relay_img.json
import json
d = json.load(open('/tmp/relay_img.json'))
if d.get('data', [{}])[0].get('b64_json'):
    print("OK  出图正常，返回 b64_json 长度", len(d['data'][0]['b64_json']))
else:
    print("异常响应：", json.dumps(d, ensure_ascii=False)[:300])
PY
echo
echo "判读：三项都 HTTP 200 = 中转正常。images/chat 出现 4xx/5xx 且 body 是 'Upstream ...' = 中转的上游挂了，联系服务提供商。"
