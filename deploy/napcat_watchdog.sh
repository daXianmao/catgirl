#!/usr/bin/env bash
# NapCat 看门狗：systemd 定时器每 2 分钟调一次（以 root 运行）。
#
# 解决的问题：
#   systemd 的 Restart=on-failure 只能救「进程死了」；
#   但 NapCat 有时进程还活着、协议端却不监听（假死），
#   外面看起来就是「猫娘突然没反应」。
#
# 失败熔断（重要）：
#   如果登录态被吊销，端口永远不会起来。无脑重启会变成「每 2 分钟登录一次」，
#   在腾讯眼里就是频繁无效登录，越试风控越重，最后直接冻结。
#   所以 30 分钟内重启 3 次仍未恢复 → 停止自动重启，落一个熔断标记，等人处理。
#   恢复方式：人工扫码登录成功后再删标记（或重启后端口自己起来会自动清）。
#
# 调参（环境变量）：ONE_BOT_PORT / SERVICE / MAX_RESTARTS / WINDOW
set -uo pipefail

ONE_BOT_PORT="${ONE_BOT_PORT:-3001}"
SERVICE="${SERVICE:-napcat}"
MAX_RESTARTS="${MAX_RESTARTS:-3}"
WINDOW="${WINDOW:-1800}"          # 统计窗口，秒
STATE="${STATE:-/var/tmp/napcat_watchdog.state}"
HALT="${HALT:-/var/tmp/napcat_watchdog.halt}"

log() { echo "[$(date '+%F %T')] $*" >&2; }

port_up() { ss -ltn 2>/dev/null | grep -q ":${ONE_BOT_PORT}\b"; }

# 1. 端口在监听 = 协议端活着。顺手清掉历史失败记录和熔断标记
if port_up; then
    rm -f "$STATE" "$HALT" 2>/dev/null
    exit 0
fi

# 2. 已经熔断过，就别再动它，等人工
if [ -f "$HALT" ]; then
    log "已熔断（$HALT 存在），跳过自动重启。恢复：sudo rm -f $HALT"
    exit 0
fi

# 3. 服务本身已经退出 → 交给 systemd 的 Restart=on-failure，不重复插手
if ! systemctl is-active --quiet "$SERVICE"; then
    exit 0
fi

# 4. 进程活着但端口不通 = 假死，统计窗口内的重启次数
now=$(date +%s)
touch "$STATE" 2>/dev/null || { log "写不了 $STATE，跳过"; exit 0; }
# 只保留 WINDOW 秒内的记录
awk -v now="$now" -v w="$WINDOW" '($1 + 0) > (now - w)' "$STATE" > "$STATE.tmp" 2>/dev/null \
    && mv -f "$STATE.tmp" "$STATE" 2>/dev/null
count=$(wc -l < "$STATE" 2>/dev/null || echo 0)

if [ "$count" -ge "$MAX_RESTARTS" ]; then
    log "$((WINDOW / 60)) 分钟内已重启 ${count} 次仍未恢复，停止自动重启。"
    log "大概率是登录态失效 —— 继续重试只会加重风控。请在电脑上开隧道扫码："
    log "  ssh -L 6099:127.0.0.1:6099 ubuntu@<服务器IP>  → http://127.0.0.1:6099/webui"
    printf '%s\n' "$(date '+%F %T') 熔断：${count} 次重启未恢复" > "$HALT" 2>/dev/null
    exit 0
fi

log "协议端端口 $ONE_BOT_PORT 没监听，判定为假死，重启 $SERVICE（窗口内第 $((count + 1)) 次）"
echo "$now" >> "$STATE"
systemctl restart "$SERVICE"
sleep 25

if port_up; then
    log "重启后已恢复，清理失败记录"
    rm -f "$STATE" 2>/dev/null
else
    log "重启后仍未监听；再失败 $((MAX_RESTARTS - count - 1)) 次就会熔断停止"
fi
exit 0
