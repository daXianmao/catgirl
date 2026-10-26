#!/usr/bin/env bash
# 猫娘云端版 · 06 把协议端从 Docker 迁到宿主机（NapCat Shell + systemd）
#
# 为什么换：
#   Docker 版 NapCat 是「一个 Electron 版 QQ 塞进容器」，在 2G 内存的小机器上
#   很容易被 OOM 杀掉，容器退出后还得手动重启，登录态和日志都不好查。
#   改成宿主机原生进程 + systemd 托管后：
#     - 崩溃由 systemd 自动拉起（Restart=on-failure），不用人盯着
#     - 日志在 journalctl 里，崩溃原因能直接翻出来
#     - 少一层 Docker，省内存
#     - 猫娘那边一行代码都不用改（还是 OneBot v11 正向 WebSocket）
#
# 迁移后架构：
#   宿主机  : NapCat Shell（QQ + 协议端）  ← systemd 托管，端口 3001 / 6099
#   宿主机  : catgirl + catgirl-qq（仍在 Docker）→ 用 host.docker.internal 连宿主协议端
#
# 用法：
#   cd ~/catgirl && bash 06_napcat_native.sh
#   （脚本可重复执行，已装的步骤会跳过）
set -uo pipefail

# 脚本自己所在目录：一定要在脚本里先算成绝对路径。
# 下面安装 Qt 时会 cd 到别的目录，那时再算 dirname "$0" 就跑到新目录去了，
# 会导致 napcat_watchdog.sh 找不到、看门狗被静默跳过。
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# docker 前缀：Ubuntu 上普通用户默认不在 docker 组里，必须 sudo，
# 否则下面诊断段全是空的、容器也停不掉（两个客户端登同一个 QQ 会互踢）。
DOCKER="docker"
if command -v docker >/dev/null 2>&1 && ! docker info >/dev/null 2>&1; then
    DOCKER="sudo docker"
fi

SUDO=""
[ "$(id -u)" -eq 0 ] || SUDO="sudo"

INSTALL_DIR="${INSTALL_DIR:-$HOME/napcat}"
APP_DIR="${APP_DIR:-$HOME/catgirl}"
ONE_BOT_PORT="${ONE_BOT_PORT:-3001}"
WEBUI_PORT="${WEBUI_PORT:-6099}"

log() { echo "[$(date +%H:%M:%S)] $*"; }
die() { echo "[$(date +%H:%M:%S)] !! $*"; exit 1; }

# ================================================================ 0. 诊断
log "=== 0. 先看 Docker 版 NapCat 到底为什么挂 ==="
if command -v docker >/dev/null 2>&1; then
  $DOCKER ps -a --filter name=napcat --format '容器: {{.Names}}  状态: {{.Status}}' 2>/dev/null || true
  $DOCKER inspect napcat --format '  退出码={{.State.ExitCode}}  OOM={{.State.OOMKilled}}  重启次数={{.RestartCount}}  镜像={{.Config.Image}}' 2>/dev/null || true
  log "--- 最近 30 行日志（最后几行常见原因）---"
  $DOCKER logs --tail 30 napcat 2>&1 | tail -30 || true
else
  log "（这台机器没有 docker，跳过诊断）"
fi
log "诊断信息只看不改：上面几行请主人自己判断一下，是 OOM 还是登录失效。"
log ""

# ================================================================ 1. 安装 NapCat Shell
log "=== 1. 安装 NapCat Shell（宿主机原生，保留 QQ 原入口）==="
if [ -f "$INSTALL_DIR/launcher.sh" ]; then
  log "已存在 $INSTALL_DIR/launcher.sh，跳过安装"
else
  mkdir -p "$INSTALL_DIR" || die "建不了 $INSTALL_DIR"
  cd "$INSTALL_DIR" || die "进不去 $INSTALL_DIR"
  INSTALLERS=(
    "https://raw.githubusercontent.com/NapNeko/napcat-linux-installer/main/install.sh"
    "https://ghfast.top/https://raw.githubusercontent.com/NapNeko/napcat-linux-installer/main/install.sh"
    "https://cdn.jsdelivr.net/gh/NapNeko/napcat-linux-installer@main/install.sh"
  )
  GOT=""
  for u in "${INSTALLERS[@]}"; do
    printf '  试 %s ... ' "$u"
    if curl -fsSL --connect-timeout 8 --max-time 60 "$u" -o "$INSTALL_DIR/install.sh" 2>/dev/null && [ -s "$INSTALL_DIR/install.sh" ]; then
      echo "OK"
      GOT="$u"
      break
    fi
    echo "失败"
  done
  [ -n "$GOT" ] || die "三个源都下不到安装脚本，请检查服务器能否访问 GitHub"
  log "开始安装（会先用 apt 装依赖，再装 QQ 客户端本体，大概几分钟）..."
  log "安装器必须 root：下面可能会要一次 sudo 密码"
  # --github-proxy auto：让安装器自己探测可用线路（直连 / ghfast / ghproxy / gh-proxy / dpik）。
  # 原来写死 0 是"直连 github.com"，这台机器连 raw 都超时，必然失败。
  $SUDO bash "$INSTALL_DIR/install.sh" --github-proxy auto \
    || die "安装器失败。若是提示「没有可用的 GitHub 下载线路」，把报错贴给铃铛，改用手动放包的方式"
  # 安装器以 root 跑，产物归 root；NapCat 服务是以普通用户启动的，配置会写不进去
  $SUDO chown -R "$(id -u):$(id -g)" "$INSTALL_DIR" 2>/dev/null || true
  log "安装产物归属已改回 $(id -un)"
fi

[ -f "$INSTALL_DIR/launcher.sh" ] || die "装完没找到 launcher.sh，检查 $INSTALL_DIR 里的实际文件名"
log "安装目录：$INSTALL_DIR"
ls -1 "$INSTALL_DIR" | head -20 | sed 's/^/    /'

# ================================================================ 2. systemd 托管
log ""
log "=== 2. 注册 systemd 服务（崩溃自动重启）==="
cat > /tmp/napcat.service <<EOF
[Unit]
Description=NapCat Shell (OneBot v11 协议端)
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=$(id -un)
WorkingDirectory=$INSTALL_DIR
ExecStart=/usr/bin/bash $INSTALL_DIR/launcher.sh
Restart=on-failure
RestartSec=5
TimeoutStopSec=20
LimitCORE=0
Environment=HOME=$HOME

[Install]
WantedBy=multi-user.target
EOF
$SUDO cp /tmp/napcat.service /etc/systemd/system/napcat.service || die "写 systemd 服务失败"
$SUDO systemctl daemon-reload
$SUDO systemctl enable napcat >/dev/null 2>&1
$SUDO systemctl restart napcat
log "已启动，等协议端起来..."

for i in $(seq 1 30); do
  if $SUDO ss -ltn 2>/dev/null | grep -q ":$WEBUI_PORT\b"; then
    log "WebUI 端口 $WEBUI_PORT 已监听"
    break
  fi
  sleep 2
done

# ================================================================ 3. 看门狗
log ""
log "=== 3. 装看门狗（进程活着但协议端没监听时自动重启）==="
WATCH_SRC="$SCRIPT_DIR/napcat_watchdog.sh"
if [ -f "$WATCH_SRC" ]; then
  install -m 755 "$WATCH_SRC" "$HOME/napcat_watchdog.sh"
  cat > /tmp/napcat-watchdog.service <<EOF
[Unit]
Description=NapCat 看门狗
[Service]
Type=oneshot
ExecStart=$HOME/napcat_watchdog.sh
EOF
  cat > /tmp/napcat-watchdog.timer <<EOF
[Unit]
Description=每 2 分钟检查一次 NapCat
[Timer]
OnBootSec=90s
OnUnitActiveSec=120s
[Install]
WantedBy=timers.target
EOF
  $SUDO cp /tmp/napcat-watchdog.service /etc/systemd/system/napcat-watchdog.service
  $SUDO cp /tmp/napcat-watchdog.timer /etc/systemd/system/napcat-watchdog.timer
  $SUDO systemctl daemon-reload
  $SUDO systemctl enable --now napcat-watchdog.timer >/dev/null 2>&1
  log "看门狗已启用（systemctl list-timers | grep napcat 可查看）"
else
  log "（没找到 napcat_watchdog.sh，跳过看门狗；可以从本次更新的目录里手动拷过去）"
fi

# ================================================================ 4. 停掉 Docker 版
log ""
log "=== 4. 停掉 Docker 版 NapCat（容器和数据卷都保留，随时能回退）==="
if command -v docker >/dev/null 2>&1; then
  $DOCKER stop napcat 2>/dev/null && log "Docker 版 napcat 已停止（没删除，回退用 sudo docker start napcat）" || log "（没有正在跑的 Docker 版 napcat，跳过）"
  if [ -n "$($DOCKER ps -q --filter name=napcat 2>/dev/null)" ]; then
    log "!! Docker 版 napcat 仍在运行：同一 QQ 号两个客户端会互踢，请手动 sudo docker stop napcat"
  else
    log "已确认 Docker 版不再运行（容器保留，随时可回退）"
  fi
fi

# ================================================================ 5. 登录 + OneBot 配置
log ""
log "=== 5. 剩下两步需要主人手动做 ==="
cat <<'EOF'

【第一步：登录 QQ】

  在你自己的电脑上开一个终端，做 SSH 隧道（这样 WebUI 只在你本机可见，公网不暴露）：

    ssh -L 6099:127.0.0.1:6099 <你的服务器用户>@<服务器IP>

  然后浏览器打开：

    http://127.0.0.1:6099/webui

  用里面的「快速登录 / 扫码登录」把 QQ 号登上去。
  扫码时记得勾选「下次登录无需确认」，否则每次重启都要重新验证。

  不想开隧道也可以看日志里的二维码：
    sudo journalctl -u napcat -n 50 --no-pager

【第二步：开 OneBot 11 正向 WebSocket 服务端】

  仍然在 WebUI 里：网络配置 → 添加 OneBot 11 网络配置 →
    类型：WebSocket 服务端
    地址：0.0.0.0
    端口：3001
    消息上报格式：array
    上报自己的消息：关
    token：留空（猫娘这边也没配 token）

  配完保存，协议端会自动重载。

【第三步：确认端口通了】

    ss -ltn | grep 3001

【第四步：猫娘侧改连接地址并重建】

    cd ~/catgirl
    # 改 compose 后重建（qq 服务要能访问宿主）
    sudo docker compose --profile qq up -d --build catgirl qq
    # 验证
    sudo docker compose logs -f qq | head -40

EOF

log "脚本跑完了。上面四步做完，猫娘就能连上宿主机的协议端了。"
log "如果想回退 Docker 版：docker start napcat，并把 compose 里 CATGIRL_QQ_WS 改回 ws://napcat:3001"
