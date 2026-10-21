#!/usr/bin/env bash
# NapCat 看门狗：systemd 定时器每 2 分钟调一次。
#
# systemd 的 Restart=on-failure 只能救「进程死了」；
# 但 NapCat 有时会进程还活着、协议端却不监听（假死），
# 这时外面看起来就是「猫娘突然没反应了」。这个脚本负责把这种情况揪出来重启。
set -uo pipefail

ONE_BOT_PORT="${ONE_BOT_PORT:-3001}"
SERVICE="${SERVICE:-napcat}"

# 1. 端口在监听 = 协议端活着
if ss -ltn 2>/dev/null | grep -q ":${ONE_BOT_PORT}\b"; then
    exit 0
fi

# 2. 端口没了但进程还在 → 假死，重启
if systemctl is-active --quiet "$SERVICE"; then
    echo "[$(date +%F %T)] 协议端端口 $ONE_BOT_PORT 没监听，判定为假死，重启 $SERVICE" >&2
    systemctl restart "$SERVICE"
    exit 0
fi

# 3. 服务已经不在了（systemd 会自己拉），这里不用管
exit 0
