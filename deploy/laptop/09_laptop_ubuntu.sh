#!/usr/bin/env bash
# 猫娘云端版 · 09 笔记本（Ubuntu）当协议端 + 反向隧道
#
# 和 Windows 那套比，Ubuntu 省掉一堆麻烦：
#   - 系统自带 ssh，反向隧道直接交给 systemd 托管（Restart=always），不用 bat 循环
#   - 不用自动登录、不用任务计划程序、不用跟 Defender 打架
#   - NapCat 用和服务器上完全一样的安装器，配置、看门狗逻辑都能照搬
#
# 跑完之后的样子：
#   笔记本  NapCat(systemd) 127.0.0.1:3001  ← ssh -R ← 服务器 0.0.0.0:3001 ← catgirl-qq
#
# 用法（在笔记本上，用你自己的账号执行，不要 sudo）：
#   bash 09_laptop_ubuntu.sh <服务器公网IP> [ssh用户，默认 ubuntu] [ssh端口，默认 22]
#
# 例：bash 09_laptop_ubuntu.sh 1.2.3.4 ubuntu 22
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

SUDO=""
[ "$(id -u)" -eq 0 ] || SUDO="sudo"

SERVER_IP="${1:-}"
SSH_USER="${2:-ubuntu}"
SSH_PORT="${3:-22}"

KEY="$HOME/.ssh/lingdang_napcat"
NAPCAT_DIR="$HOME/napcat"
ONE_BOT_PORT="${ONE_BOT_PORT:-3001}"
WEBUI_PORT="${WEBUI_PORT:-6099}"
SVC="$HOME/napcat_tunnel.sh"

log() { echo "[$(date +%H:%M:%S)] $*"; }
die() { echo "[$(date +%H:%M:%S)] !! $*"; exit 1; }

[ -n "$SERVER_IP" ] || die "缺服务器地址。用法：bash 09_laptop_ubuntu.sh <服务器公网IP> [ssh用户] [ssh端口]"
[ "$(id -u)" -ne 0 ] || die "不要用 root 跑，用你自己的账号（脚本内部会自己 sudo）"

# ================================================================ 1. 电源：永不休眠、合盖不睡
# 笔记本合盖就睡眠是这套方案最大的隐形杀手：跑得好好的，一合盖就全网失联。
log "=== 1. 关掉睡眠 / 合盖休眠 ==="
$SUDO systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target >/dev/null 2>&1 \
    && log "  sleep/suspend/hibernate 已屏蔽" || log "  （屏蔽失败，可能已经屏蔽过）"

if [ -f /etc/systemd/logind.conf ]; then
    $SUDO sed -i 's/^#\?HandleLidSwitch=.*/HandleLidSwitch=ignore/' /etc/systemd/logind.conf
    $SUDO grep -q '^HandleLidSwitchExternalPower=' /etc/systemd/logind.conf \
        || echo 'HandleLidSwitchExternalPower=ignore' | $SUDO tee -a /etc/systemd/logind.conf >/dev/null
    log "  合盖已设为不动作（重启后生效）"
fi

# ================================================================ 2. 装 NapCat
log ""
log "=== 2. 安装 NapCat Shell ==="
if [ -f "$NAPCAT_DIR/launcher.sh" ]; then
    log "  已存在 $NAPCAT_DIR/launcher.sh，跳过安装"
else
    mkdir -p "$NAPCAT_DIR"
    if [ ! -s "$NAPCAT_DIR/install.sh" ]; then
        GOT=""
        for u in "https://raw.githubusercontent.com/NapNeko/napcat-linux-installer/main/install.sh" \
                 "https://ghfast.top/https://raw.githubusercontent.com/NapNeko/napcat-linux-installer/main/install.sh" \
                 "https://cdn.jsdelivr.net/gh/NapNeko/napcat-linux-installer@main/install.sh"; do
            printf '  下载安装器 %s ... ' "${u:0:48}"
            if curl -fsSL --connect-timeout 8 --max-time 90 "$u" -o "$NAPCAT_DIR/install.sh" 2>/dev/null \
               && [ -s "$NAPCAT_DIR/install.sh" ]; then
                echo "OK"
                GOT="$u"
                break
            fi
            echo "失败"
        done
        [ -n "$GOT" ] || die "三个源都下不到安装脚本，检查这台机器的网络"
    fi

    ARGS="--github-proxy auto"
    if [ -s "$NAPCAT_DIR/NapCat.Shell.zip" ]; then
        log "  发现本地 NapCat.Shell.zip，跳过 GitHub 下载"
        ARGS="--github-proxy 0"
    fi
    log "  开始安装（会 apt 装依赖 + 装 LinuxQQ 本体，几分钟）"
    # shellcheck disable=SC2086
    $SUDO bash "$NAPCAT_DIR/install.sh" $ARGS \
        || die "安装器失败。若卡在下载，把 NapCat.Shell.zip 放进 $NAPCAT_DIR 再重跑"
    # 安装器以 root 跑，产物归 root；服务是普通用户起的，配置会写不进去
    $SUDO chown -R "$(id -u):$(id -g)" "$NAPCAT_DIR" 2>/dev/null || true
fi
[ -f "$NAPCAT_DIR/launcher.sh" ] || die "没找到 $NAPCAT_DIR/launcher.sh，看看实际解出来的文件名"

# ================================================================ 3. systemd 托管 NapCat
log ""
log "=== 3. 注册 napcat 服务（崩溃自动拉起）==="
cat > /tmp/napcat.service <<EOF
[Unit]
Description=NapCat Shell (OneBot v11)
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=$(id -un)
WorkingDirectory=$NAPCAT_DIR
ExecStart=/usr/bin/bash $NAPCAT_DIR/launcher.sh
Restart=on-failure
RestartSec=5
TimeoutStopSec=20
Environment=HOME=$HOME

[Install]
WantedBy=multi-user.target
EOF
$SUDO cp /tmp/napcat.service /etc/systemd/system/napcat.service || die "写服务失败"
$SUDO systemctl daemon-reload
$SUDO systemctl enable napcat >/dev/null 2>&1

# ================================================================ 4. 看门狗：进程活着但 3001 不通
# QQ 掉登录时进程还在，systemd 的 Restart 管不到这种情况，得自己探端口。
# 冷却 30 分钟：否则会把等你扫码的窗口反复杀掉
log ""
log "=== 4. 装看门狗（每 2 分钟探一次 3001）==="
cat > "$SVC" <<'EOF'
#!/usr/bin/env bash
PORT="${PORT:-3001}"
STAMP=/tmp/napcat_watchdog.last
if ss -ltn 2>/dev/null | grep -q ":${PORT}\b"; then
    exit 0
fi
# 30 分钟内已经重启过就不再动
if [ -f "$STAMP" ]; then
    last=$(cat "$STAMP" 2>/dev/null || echo 0)
    now=$(date +%s)
    [ $((now - last)) -lt 1800 ] && exit 0
fi
echo "[$(date '+%m-%d %H:%M:%S')] 3001 不通，重启 napcat"
sudo systemctl restart napcat
date +%s > "$STAMP"
EOF
chmod 755 "$SVC"

cat > /tmp/napcat-watchdog.service <<EOF
[Unit]
Description=NapCat 看门狗
[Service]
Type=oneshot
Environment=PORT=$ONE_BOT_PORT
ExecStart=/usr/bin/bash $SVC
EOF
cat > /tmp/napcat-watchdog.timer <<'EOF'
[Unit]
Description=每 2 分钟检查 NapCat 是否在监听
[Timer]
OnBootSec=120s
OnUnitActiveSec=120s
[Install]
WantedBy=timers.target
EOF
$SUDO cp /tmp/napcat-watchdog.service /etc/systemd/system/napcat-watchdog.service
$SUDO cp /tmp/napcat-watchdog.timer /etc/systemd/system/napcat-watchdog.timer
$SUDO systemctl daemon-reload
$SUDO systemctl enable --now napcat-watchdog.timer >/dev/null 2>&1
log "  看门狗已启用"

# ================================================================ 5. SSH 密钥
log ""
log "=== 5. 反向隧道用的密钥 ==="
mkdir -p "$HOME/.ssh" && chmod 700 "$HOME/.ssh"
if [ -f "$KEY" ]; then
    log "  已有 $KEY，复用"
else
    ssh-keygen -t ed25519 -f "$KEY" -N "" -C "napcat-tunnel" >/dev/null 2>&1 \
        || die "生成密钥失败"
    log "  已生成 $KEY"
fi
chmod 600 "$KEY" 2>/dev/null || true

if ssh -p "$SSH_PORT" -i "$KEY" -o BatchMode=yes -o StrictHostKeyChecking=accept-new \
       -o ConnectTimeout=8 "$SSH_USER@$SERVER_IP" true 2>/dev/null; then
    log "  免密登录服务器已通"
else
    echo
    echo "  ------------------------------------------------------------"
    echo "  这把公钥还没加到服务器上。把这行整个复制："
    echo
    cat "$KEY.pub"
    echo
    echo "  粘到服务器的这条命令里（把 <公钥> 换成上面那串）："
    echo "    mkdir -p ~/.ssh && chmod 700 ~/.ssh"
    echo "    echo 'no-pty,no-agent-forwarding,no-X11-forwarding,no-user-rc <公钥>' >> ~/.ssh/authorized_keys"
    echo "    chmod 600 ~/.ssh/authorized_keys"
    echo "  ------------------------------------------------------------"
    echo
    read -r -p "  加好之后按回车继续（Ctrl+C 退出也行，之后再跑本脚本）: " _
fi

# ================================================================ 6. 隧道服务
log ""
log "=== 6. 注册反向隧道服务 ==="
cat > /tmp/napcat-tunnel.service <<EOF
[Unit]
Description=反向隧道：本机 ${ONE_BOT_PORT} -> ${SERVER_IP}:${ONE_BOT_PORT}
After=network-online.target napcat.service
Wants=network-online.target

[Service]
User=$(id -un)
ExecStart=/usr/bin/ssh -NT -i $KEY -p $SSH_PORT \\
    -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \\
    -o ExitOnForwardFailure=yes -o StrictHostKeyChecking=accept-new -o TCPKeepAlive=yes \\
    -R 0.0.0.0:${ONE_BOT_PORT}:127.0.0.1:${ONE_BOT_PORT} \\
    ${SSH_USER}@${SERVER_IP}
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
$SUDO cp /tmp/napcat-tunnel.service /etc/systemd/system/napcat-tunnel.service
$SUDO systemctl daemon-reload
$SUDO systemctl enable napcat-tunnel >/dev/null 2>&1
$SUDO systemctl restart napcat-tunnel

# ================================================================ 7. 验证
log ""
log "=== 7. 验证 ==="
$SUDO systemctl restart napcat
for i in $(seq 1 30); do
    $SUDO ss -ltn 2>/dev/null | grep -q ":${WEBUI_PORT}\b" && break
    sleep 2
done

$SUDO ss -ltn 2>/dev/null | grep -q ":${WEBUI_PORT}\b" \
    && log "  WebUI ${WEBUI_PORT} 已监听" \
    || log "  !! WebUI 还没起来，看日志：sudo journalctl -u napcat -n 40 --no-pager"

$SUDO ss -ltn 2>/dev/null | grep -q ":${ONE_BOT_PORT}\b" \
    && log "  OneBot ${ONE_BOT_PORT} 已监听" \
    || log "  !! ${ONE_BOT_PORT} 没监听 —— 去 WebUI 里配 WebSocket 服务器（见下面第 2 条）"

sleep 2
if $SUDO systemctl is-active napcat-tunnel | grep -qx active; then
    log "  隧道服务 active"
else
    log "  !! 隧道没起来，看日志：sudo journalctl -u napcat-tunnel -n 20 --no-pager"
fi

cat <<EOF

================================================================
 还差最后一步：配置 NapCat 的 OneBot 服务
================================================================
1) 浏览器打开 http://127.0.0.1:${WEBUI_PORT}/webui（token 在 napcat 日志里）
   sudo journalctl -u napcat -n 40 --no-pager | grep -i webui

2) 左侧「网络配置」→ 新建 → WebSocket 服务器，填：
     主机 0.0.0.0   端口 ${ONE_BOT_PORT}
     token asfarhsdadaw2s      （和服务器 .env 的 CATGIRL_QQ_TOKEN 一致）
     消息格式 array
   保存。${ONE_BOT_PORT} 起来后，隧道和容器会自己在 30 秒内连上。

3) 扫码登录小号 3879101317，勾「下次登录无需确认」。

4) 回服务器上验收：
   sudo ss -ltn | grep ${ONE_BOT_PORT}
   sudo docker logs --tail 8 catgirl-qq     # 出现「已登录：洛小猫（铃洛）」就齐了

常用命令：
  sudo systemctl status napcat napcat-tunnel
  sudo journalctl -u napcat -n 50 --no-pager      # 协议端日志（含二维码链接）
  sudo journalctl -u napcat-tunnel -n 20 --no-pager
  sudo systemctl restart napcat
================================================================
EOF
