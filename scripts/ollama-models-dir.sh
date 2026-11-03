#!/bin/sh
# llmchat 模型目录切换（由 sudoers 白名单放行，llmchat 服务端调用）
# 用法: ollama-models-dir.sh set <绝对路径> [migrate]
#   set <path>          写入 systemd drop-in（OLLAMA_MODELS）并重启 ollama
#   set <path> migrate  迁移前先把旧目录内容 rsync 过去
#   get                 查看当前 drop-in 内容
set -eu

DROPIN=/etc/systemd/system/ollama.service.d/models-dir.conf
# 目标目录只允许落在这些前缀下，防写坏系统盘
ALLOW_PREFIXES="/home/kiki /mnt /srv /opt"
OLD_DEFAULT=/usr/share/ollama/.ollama/models

get_dir() {
  systemctl show ollama -p Environment 2>/dev/null | tr ' ' '\n' |
    sed -n 's/^OLLAMA_MODELS=//p' | tr -d '"' | head -1
}

case "${1:-}" in
  get)
    cat "$DROPIN" 2>/dev/null || echo "OLLAMA_MODELS 未设置（默认 $OLD_DEFAULT）"
    exit 0
    ;;
  set) ;;
  *) echo "用法: $0 set <path> [migrate] | get"; exit 2 ;;
esac

path="${2:-}"
migrate="${3:-}"

# ---- 校验 ----
case "$path" in
  /*) ;;
  *) echo "错误：必须是绝对路径"; exit 2 ;;
esac
case "$path" in
  /|/bin|/boot|/dev|/etc|/home|/lib|/lib32|/lib64|/libx32|/media|/proc|/root|/run|/sbin|/snap|/srv|/sys|/tmp|/usr|/var)
    echo "错误：禁止的系统目录"; exit 2 ;;
esac
case "$path" in
  *..*) echo "错误：路径含 .."; exit 2 ;;
esac
ok=0
for p in $ALLOW_PREFIXES; do
  case "$path" in
    "$p"/*) ok=1 ;;
  esac
done
if [ "$ok" != 1 ]; then
  echo "错误：只允许这些前缀下的目录: $ALLOW_PREFIXES"
  exit 2
fi

# ---- 迁移 ----
if [ "$migrate" = "migrate" ]; then
  old="$(get_dir || true)"
  [ -n "$old" ] || old="$OLD_DEFAULT"
  if [ -d "$old" ] && [ "$old" != "$path" ]; then
    echo "迁移: $old -> $path"
    mkdir -p "$path"
    chown ollama:ollama "$path"
    rsync -a "$old/" "$path/"
    echo "迁移完成（旧目录保留，确认新目录正常后可手工清理）"
  fi
fi

# ---- 落盘 drop-in 并重启 ----
mkdir -p "$(dirname "$DROPIN")"
printf '[Service]\nEnvironment="OLLAMA_MODELS=%s"\n' "$path" > "$DROPIN"
chmod 644 "$DROPIN"
chown ollama:ollama "$path" 2>/dev/null || true
systemctl daemon-reload
systemctl restart ollama
echo "OK $path"
