#!/usr/bin/env bash
# 强制同步远端代码并重启 wqt-hub，最后打印最近 40 行日志。
#
# 每次执行都会丢弃服务器上的本地改动，用远端分支完整覆盖：
#   git fetch --prune  +  git reset --hard <remote>/<branch>
#
# 用法：
#   sudo ./update-hub.sh
#   REMOTE=origin BRANCH=master LOG_LINES=80 sudo ./update-hub.sh
#   CLEAN=1 sudo ./update-hub.sh          # 额外删掉未跟踪的非忽略文件
#
# 说明：
#   · 默认仓库 /opt/wqt-hub，服务名 wqt-hub，日志 40 行，可用环境变量覆盖
#   · 非 root 运行时自动给 git / systemctl / journalctl 加 sudo
#   · 自动登记 safe.directory，避免 root 与仓库 owner 不同导致的 dubious ownership
#   · 数据目录（accounts/usage/logs）在 .gitignore 里，reset/clean 都不会动它们
#   · fetch 或 reset 失败直接退出，不会带着旧代码重启

set -euo pipefail

REPO_DIR="${REPO_DIR:-/opt/wqt-hub}"
SERVICE="${SERVICE:-wqt-hub}"
LOG_LINES="${LOG_LINES:-40}"
REMOTE="${REMOTE:-origin}"
BRANCH="${BRANCH:-master}"

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

if ! $SUDO git remote get-url "$REMOTE" >/dev/null 2>&1; then
  echo "!! 远端 '$REMOTE' 不存在，现有远端：" >&2
  $SUDO git remote -v >&2 || true
  exit 1
fi

BEFORE=$($SUDO git rev-parse --short HEAD 2>/dev/null || echo none)

echo "==> [1/3] 强制同步 $REMOTE/$BRANCH（本地改动会被丢弃）"
if ! $SUDO git fetch --prune "$REMOTE" "$BRANCH"; then
  echo "!! git fetch 失败：检查网络/权限，或确认分支 '$BRANCH' 存在" >&2
  exit 1
fi
$SUDO git reset --hard "$REMOTE/$BRANCH"

# 远端已删除的旧构建 chunk 由 reset 处理；未跟踪文件默认保留，
# 需要彻底干净时显式 CLEAN=1（-fd 不碰 .gitignore 里的数据目录）
if [ "${CLEAN:-0}" = "1" ]; then
  echo "==> 清理未跟踪文件（保留 .gitignore 中的数据）"
  $SUDO git clean -fd
fi

AFTER=$($SUDO git rev-parse --short HEAD)
echo "==> 代码已同步：$BEFORE -> $AFTER  ($REMOTE/$BRANCH)"
$SUDO git log --oneline -1

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
