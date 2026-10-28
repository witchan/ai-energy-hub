#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVER_HOST="${SERVER_HOST:-}"
SERVER_USER="${SERVER_USER:-}"
SERVER_PORT="${SERVER_PORT:-22}"
REMOTE_DIR="${REMOTE_DIR:-}"
SSH_KEY="${SSH_KEY:-}"
COMPOSE_FILES="-f docker-compose.yml"
if [ "${HTTPS:-0}" = "1" ]; then COMPOSE_FILES="$COMPOSE_FILES -f docker-compose.https.yml"; fi

if docker compose version >/dev/null 2>&1; then
    DC="docker compose"
elif docker-compose version >/dev/null 2>&1; then
    DC="docker-compose"
else
    echo "未找到 Docker Compose" >&2
    exit 1
fi

if [ -z "$SERVER_HOST" ]; then
    cd "$PROJECT_DIR"
    $DC $COMPOSE_FILES up -d --build
    echo "本地服务已启动：http://localhost:${PORT:-9006}"
    exit 0
fi

if [ -z "$SERVER_USER" ] || [ -z "$REMOTE_DIR" ]; then
    echo "远程部署必须设置 SERVER_USER 和 REMOTE_DIR" >&2
    exit 1
fi

SSH_OPTS=(-p "$SERVER_PORT" -o BatchMode=yes)
if [ -n "$SSH_KEY" ]; then SSH_OPTS+=(-i "$SSH_KEY"); fi
SSH_DEST="$SERVER_USER@$SERVER_HOST"
ssh "${SSH_OPTS[@]}" "$SSH_DEST" "mkdir -p '$REMOTE_DIR'"
rsync -az --delete \
    --exclude='artifacts' --exclude='.git' --exclude='.env' --exclude='data' --exclude='*.db*' \
    --exclude='.DS_Store' --exclude='__pycache__' --exclude='.workbuddy' \
    -e "ssh ${SSH_OPTS[*]}" "$PROJECT_DIR/" "$SSH_DEST:$REMOTE_DIR/"
ssh "${SSH_OPTS[@]}" "$SSH_DEST" "cd '$REMOTE_DIR' && $DC $COMPOSE_FILES up -d --build"
echo "远程服务已更新；服务器上的 .env 与 data/ 未被覆盖。"
