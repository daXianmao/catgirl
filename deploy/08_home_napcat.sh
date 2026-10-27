#!/usr/bin/env bash
# 猫娘云端版 · 08 把 QQ 协议端搬到家里的 Windows 笔记本（SSH 反向隧道）
#
# 为什么要搬：
#   云服务器的 IP 属于腾讯云机房网段，QQ 对机房 IP 登录极度敏感，小号挂在这上面
#   就是天天被要求验证、被踢下线。家里的宽带是住宅 IP，IP 信誉正常，风控基本不找上门。
#
# 搬完之后的架构（catgirl 的代码、.env 一行都不用改）：
#
#   家里笔记本                                    云服务器
#   ┌────────────────────┐            ┌─────────────────────────────┐
#   │ NapCat 127.0.0.1   │            │ sshd                         │
#   │   :3001            │◀── 反向 ──▶│   └ 在本机开出 0.0.0.0:3001  │
#   │ ssh -R（主动外连） │   隧道     │ catgirl-qq ──▶ 3001         │
#   └────────────────────┘            └─────────────────────────────┘
#
#   为什么不用 frp：frpc.exe 会被 Windows Defender 直接判成病毒（frp 长期被误杀），
#   解压就被删。Windows 10/11 自带 OpenSSH 客户端，ssh -R 零安装、原生加密，
#   和 frp 一个效果。
#
# 用法：
#   cd ~/catgirl && bash deploy/08_home_napcat.sh
#   （可重复执行，已经配好的步骤会跳过）
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

DOCKER="docker"
if command -v docker >/dev/null 2>&1 && ! docker info >/dev/null 2>&1; then
    DOCKER="sudo docker"
fi

SUDO=""
[ "$(id -u)" -eq 0 ] || SUDO="sudo"

REMOTE_PORT="${REMOTE_PORT:-3001}"   # 映射到服务器的端口，必须和容器里配的一致
DROPIN="/etc/ssh/sshd_config.d/50-napcat-tunnel.conf"

log() { echo "[$(date +%H:%M:%S)] $*"; }
die() { echo "[$(date +%H:%M:%S)] !! $*"; exit 1; }

# ================================================================ 0. 现状
log "=== 0. 现状 ==="
$SUDO ss -ltn 2>/dev/null | grep -E ":(${REMOTE_PORT})\b" || log "  ${REMOTE_PORT} 目前没人监听"
echo -n "  服务器上的 napcat: "; $SUDO systemctl is-active napcat 2>/dev/null || echo unknown
echo -n "  看门狗: "; $SUDO systemctl is-active napcat-watchdog.timer 2>/dev/null || echo unknown
echo -n "  sshd GatewayPorts: "; $SUDO sshd -T 2>/dev/null | grep -i '^gatewayports' || echo "未显式配置（默认 no）"

# ================================================================ 1. 停掉服务器上的 NapCat
# 必须停：它占着 3001，隧道的远程转发会绑不上；而且就是要让它别在机房 IP 上登录。
log ""
log "=== 1. 停掉服务器上的协议端（腾出 ${REMOTE_PORT}，也终止机房 IP 登录）==="
$SUDO systemctl disable --now napcat 2>/dev/null && log "  napcat 已停止并取消自启" || log "  napcat 未安装或已停"
$SUDO systemctl disable --now napcat-watchdog.timer 2>/dev/null && log "  看门狗已停" || true
if command -v docker >/dev/null 2>&1 && $DOCKER ps -a --format '{{.Names}}' 2>/dev/null | grep -qx napcat; then
    $DOCKER rm -f napcat >/dev/null 2>&1 && log "  备用 Docker 版 NapCat 容器已删除"
fi
if $SUDO ss -ltn 2>/dev/null | grep -q ":${REMOTE_PORT}\b"; then
    $SUDO ss -ltnp 2>/dev/null | grep ":${REMOTE_PORT}\b"
    die "${REMOTE_PORT} 还被占用，先停掉上面那个进程"
fi
log "  ${REMOTE_PORT} 已腾空"

# ================================================================ 2. 让 sshd 允许把远端端口绑到 0.0.0.0
# 默认 GatewayPorts no 只绑 127.0.0.1，Docker 容器（172.17.0.1）连不进去。
# 用 drop-in 而不是改主配置：sshd_config 里 Include 在最顶部，drop-in 优先生效，
# 也不会碰到主人手动改过的其它行。
log ""
log "=== 2. 配置 sshd（GatewayPorts clientspecified）==="
if $SUDO sshd -T 2>/dev/null | grep -qi '^gatewayports clientspecified'; then
    log "  已经是 clientspecified，跳过"
else
    $SUDO mkdir -p /etc/ssh/sshd_config.d
    $SUDO tee "$DROPIN" >/dev/null <<'EOF'
# 让反向隧道能把端口绑到 0.0.0.0（容器要能连进来）
# 由 catgirl deploy/08_home_napcat.sh 写入
GatewayPorts clientspecified
EOF
    log "  写入 $DROPIN"
    # 改 sshd 前先测语法，测不过就回滚，别把自己关在门外
    if ! $SUDO sshd -t 2>/tmp/sshd_t.err; then
        cat /tmp/sshd_t.err
        $SUDO rm -f "$DROPIN"
        die "sshd 配置校验失败，已回滚（你的 SSH 没被动过）"
    fi
    $SUDO systemctl reload ssh 2>/dev/null || $SUDO systemctl reload sshd 2>/dev/null || true
    sleep 1
    $SUDO sshd -T 2>/dev/null | grep -qi '^gatewayports clientspecified' \
        && log "  已生效" \
        || die "reload 后还是没生效，手动跑：sudo systemctl reload ssh && sudo sshd -T | grep -i gateway"
fi

# ================================================================ 3. 打印笔记本上要做的事
SRV_IP=$(curl -s --max-time 5 https://api.ipify.org 2>/dev/null || echo "你的服务器公网IP")
SSH_USER=$(whoami)

cat <<EOF

================================================================
 笔记本（Windows）上要做的事
================================================================
【第 1 步】生成一对专用密钥（不会碰你已有的 SSH key）

  ssh-keygen -t ed25519 -f "%USERPROFILE%\\.ssh\\lingdang_napcat" -N "" -C "napcat-tunnel"
  type "%USERPROFILE%\\.ssh\\lingdang_napcat.pub"

【第 2 步】把上面打印出来的那串公钥，粘到下面命令的 <公钥> 位置，
         在服务器（就是这个窗口）执行一次：

  mkdir -p ~/.ssh && chmod 700 ~/.ssh
  echo 'no-pty,no-agent-forwarding,no-X11-forwarding,no-user-rc <公钥>' >> ~/.ssh/authorized_keys
  chmod 600 ~/.ssh/authorized_keys

  —— 注意别加 restrict 或 command=，它们会把端口转发一起禁掉，隧道就废了。

【第 3 步】把 deploy/laptop/tunnel_start.bat 和 watchdog.ps1 放到 C:\\Lingdang\\ 下，
         把里面三行改成你的实际情况：

  set SERVER=${SRV_IP}
  set SSHUSER=${SSH_USER}
  set KEY=%USERPROFILE%\\.ssh\\lingdang_napcat

【第 4 步】双击 tunnel_start.bat 跑起来（先别关窗口看有没有报错）
================================================================

现在就在这个窗口验隧道通没通（笔记本那双击之后再看）：

  sudo ss -ltn | grep ${REMOTE_PORT}
  sudo docker logs --tail 8 catgirl-qq

第一条出现监听 = 隧道连上了；第二条出现「已登录：洛小猫（铃洛）」= 全链路通。

还要在腾讯云安全组确认 22 端口放行（笔记本要能 SSH 出来），
并且 ${REMOTE_PORT} 保持不放行公网 —— 隧道只在服务器内部用，不需要对外开口。

想退回服务器本地跑协议端（笔记本关机时应急）：
  sudo systemctl enable --now napcat
  （先让笔记本那边的隧道窗口停掉，否则端口会撞）
EOF
