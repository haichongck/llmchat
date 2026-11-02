#!/usr/bin/env bash
# 冒烟测试：起一个临时实例（管理面板关闭），打几个关键接口再退出。
# 本地与 CI 共用：bash scripts/smoke.sh [端口]
set -euo pipefail
PORT="${1:-17101}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

LOG="$(mktemp)"
LLMCHAT_PORT="$PORT" LLMCHAT_ADMIN=0 LLMCHAT_PROXY_ALLOW=127.0.0.1 \
  python3 server.py >"$LOG" 2>&1 &
SVPID=$!
cleanup() { kill "$SVPID" 2>/dev/null || true; wait "$SVPID" 2>/dev/null || true; rm -f "$LOG"; }
trap cleanup EXIT

for _ in $(seq 1 40); do
  curl -fsS "http://127.0.0.1:$PORT/" >/dev/null 2>&1 && break
  sleep 0.25
done

fail=0
check() { # name expected actual
  if [ "$2" = "$3" ]; then printf '  ok   %-22s %s\n' "$1" "$3"
  else printf '  FAIL %-22s want=%s got=%s\n' "$1" "$2" "$3"; fail=1; fi
}

check "首页"            200 "$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/")"
check "KaTeX 静态资源"  200 "$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/vendor/katex/katex.min.css")"
check "管理关闭时 404"  404 "$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/__admin/state")"
check "配置接口"        200 "$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/__config")"

body="$(curl -s -X POST "http://127.0.0.1:$PORT/__proxy" \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://example.com/","method":"GET"}')"
if grep -q '白名单' <<<"$body"; then echo "  ok   反代白名单拦截"; else echo "  FAIL 反代未拦截外站: $body"; fail=1; fi

[ "$fail" = "0" ] && echo "冒烟测试通过" || { echo "冒烟测试失败"; exit 1; }
