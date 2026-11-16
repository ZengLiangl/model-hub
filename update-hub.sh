#!/usr/bin/env bash
# 一键更新并重启 wqt-hub，最后打印最近 40 行日志。
#
# 用法：
#   sudo ./update-hub.sh
#   REPO_DIR=/opt/wqt-hub SERVICE=wqt-hub LOG_LINES=80 ./update-hub.sh
#
# 说明：
#   · 默认仓库 /opt/wqt-hub，服务名 wqt-hub，日志 40 行，可用环境变量覆盖
#   · 非 root 运行时自动给 git / systemctl / journalctl 加 sudo
#   · 自动登记 safe.directory，避免 root 与仓库 owner 不同导致的 dubious ownership
#   · pull 失败（有本地改动/冲突）会直接退出，不会带着旧代码重启

set -euo pipefail

REPO_DIR="${REPO_DIR:-/opt/wqt-hub}"
SERVICE="${SERVICE:-wqt-hub}"
LOG_LINES="${LOG_LINES:-40}"

if [ "$(id -u)" -ne 0 ]; then
  SUDO="sudo"
else
  SUDO=""
fi

if [ ! -d "$REPO_DIR/.git" ]; then
  echo "!! $REPO_DIR 不是 git 仓库，请用 REPO_DIR=... 指定正确路径" >&2
  exit 1
fi

cd "$REPO_DIR"

# root 与仓库 owner 不同时，git 会拒绝操作
$SUDO git config --global --add safe.directory "$REPO_DIR" 2>/dev/null || true

echo "==> [1/3] 拉取代码"
if ! $SUDO git pull --ff-only; then
  echo "!! git pull 失败：本地可能有过改动或分支分叉" >&2
  echo "   查看：git status" >&2
  echo "   丢弃本地改动后重试：sudo git reset --hard HEAD && sudo git pull --ff-only" >&2
  exit 1
fi

echo "==> [2/3] 重启 $SERVICE"
$SUDO systemctl restart "$SERVICE"

# 给进程一点启动时间，再判断状态并取日志
sleep 2

if ! $SUDO systemctl is-active --quiet "$SERVICE"; then
  echo "!! $SERVICE 启动失败，最近 ${LOG_LINES} 行日志：" >&2
  $SUDO journalctl -u "$SERVICE" -n "$LOG_LINES" --no-pager >&2 || true
  exit 1
fi

echo "==> [3/3] 服务状态"
$SUDO systemctl status "$SERVICE" --no-pager -l | head -15 || true

echo
echo "==> 最近 ${LOG_LINES} 行日志"
$SUDO journalctl -u "$SERVICE" -n "$LOG_LINES" --no-pager
