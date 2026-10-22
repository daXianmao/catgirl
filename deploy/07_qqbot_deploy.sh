#!/usr/bin/env bash
# ============================================================
#  猫娘 → QQ 官方机器人  一键部署
#
#  前置：已经在 q.qq.com 建好机器人，拿到 AppID / AppSecret，
#        并把这台服务器的公网 IP 填进了「服务器 IP 白名单」。
#
#  用法： cd ~/catgirl && bash 07_qqbot_deploy.sh
#
#  做的事：拉代码 → 写 .env → 构建 → 自检 → 停 NapCat → 起官方机器人
#  自检不通过就停在这里，不会动现有服务。
# ============================================================
set -uo pipefail

APP_DIR="${APP_DIR:-$HOME/catgirl}"
BRANCH="${BRANCH:-cloud}"
REPO="daXianmao/catgirl"

cd "$APP_DIR" 2>/dev/null || { echo "找不到 $APP_DIR，先确认部署目录"; exit 1; }

# ubuntu 用户跑 docker 要 sudo；已经是 root 就别套了
if [ "$(id -u)" -eq 0 ]; then SUDO=""; else SUDO="sudo"; fi

echo "=============================================================="
echo "  猫娘 → QQ 官方机器人   一键部署"
echo "  目录：$APP_DIR   分支：$BRANCH"
echo "=============================================================="
echo

# ---------------------------------------------------------- 1. 凭证
read -rp "AppID（纯数字）: " APP_ID
read -rsp "AppSecret（输入不回显）: " APP_SECRET
echo
APP_ID="$(printf '%s' "$APP_ID" | tr -d '[:space:]')"
APP_SECRET="$(printf '%s' "$APP_SECRET" | tr -d '[:space:]')"

if [ -z "$APP_ID" ] || [ -z "$APP_SECRET" ]; then
    echo "!! AppID / AppSecret 不能为空"
    exit 1
fi

# ---------------------------------------------------------- 2. 白名单
PUB_IP="$(curl -fsS --connect-timeout 5 --max-time 10 https://ip.sb 2>/dev/null \
       || curl -fsS --connect-timeout 5 --max-time 10 https://api.ipify.org 2>/dev/null \
       || true)"
echo
echo "本机公网 IP：${PUB_IP:-（取不到，去腾讯云控制台看）}"
echo "这一条必须先做：开放平台 → 你的机器人 → 开发设置 → 服务器 IP 白名单，"
echo "把这个 IP 加进去。没加的话后面自检会报「源 IP 不在白名单」。"
read -rp "已经填好白名单了吗？(y/N) " READY
case "$READY" in
    [yY]*) ;;
    *) echo "先去填，填完再跑本脚本。"; exit 1 ;;
esac

# ---------------------------------------------------------- 3. 拉代码
echo
echo "[1/6] 拉取代码"
SRC="https://raw.githubusercontent.com/$REPO/$BRANCH"
FILES="qqbot.py config.py qq.py core.py brain.py voice.py tts.py persona.md docker-compose.yml .env.example"
FAIL=""
for f in $FILES; do
    printf '  %-22s ' "$f"
    if curl -fsSL --connect-timeout 8 --max-time 30 "$SRC/$f" -o "$f.tmp" 2>/dev/null \
       && [ -s "$f.tmp" ]; then
        mv "$f.tmp" "$f"; echo "OK"
    elif curl -fsSL --connect-timeout 8 --max-time 30 "https://ghfast.top/$SRC/$f" -o "$f.tmp" 2>/dev/null \
       && [ -s "$f.tmp" ]; then
        mv "$f.tmp" "$f"; echo "OK（镜像）"
    else
        rm -f "$f.tmp"; echo "失败"; FAIL=1
    fi
done
if [ -n "$FAIL" ]; then
    echo "!! 有文件没拉到，先解决网络再重跑（可 export BRANCH=cloud 换分支）"
    exit 1
fi

# ---------------------------------------------------------- 4. 写 .env
echo
echo "[2/6] 写入 .env"
[ -f .env ] || : > .env
BAK=".env.bak.$(date +%Y%m%d%H%M%S)"
cp .env "$BAK"
echo "  旧配置备份：$BAK"

# 用 awk 精确替换，避免 secret 里的 & / | 被 sed 当特殊字符解释
set_kv() {
    local key="$1" val="$2"
    if grep -q "^${key}=" .env 2>/dev/null; then
        awk -v k="$key" -v v="$val" '
            BEGIN { FS = OFS = "=" }
            $1 == k { print k "=" v; next }
            { print }
        ' .env > .env.tmp && mv .env.tmp .env
    else
        printf '%s=%s\n' "$key" "$val" >> .env
    fi
}

set_kv CATGIRL_QQBOT_ENABLED      "true"
set_kv CATGIRL_QQBOT_APP_ID       "$APP_ID"
set_kv CATGIRL_QQBOT_SECRET       "$APP_SECRET"
set_kv CATGIRL_QQBOT_SANDBOX      "false"
echo "  已写入 AppID / Secret，环境=正式（sandbox=false）"

# ---------------------------------------------------------- 5. 构建 + 自检
echo
echo "[3/6] 构建镜像（第一次要几分钟）"
$SUDO docker compose --profile official build qqbot || { echo "!! 构建失败"; exit 1; }

echo
echo "[4/6] 自检：AppID / Secret / IP 白名单 / 网关"
if ! $SUDO docker compose --profile official run --rm --no-deps qqbot python qqbot.py --check; then
    echo
    echo "!! 自检没通过，现有服务没有动。"
    echo "   按上面提示修好，然后重跑本脚本。"
    echo "   要回滚 .env： cp $BAK .env"
    exit 1
fi

# ---------------------------------------------------------- 6. 切链路
echo
echo "[5/6] 停掉 NapCat（两条链路同时开会把同一条消息回两遍）"
if $SUDO docker compose --profile qq stop qq 2>/dev/null; then
    echo "  Docker 版 qq 已停"
else
    echo "  Docker 版 qq 未在运行（正常）"
fi
if systemctl list-unit-files 2>/dev/null | grep -q '^napcat\.service'; then
    $SUDO systemctl stop napcat 2>/dev/null && echo "  宿主 NapCat 已停"
    $SUDO systemctl disable napcat 2>/dev/null && echo "  宿主 NapCat 已取消开机自启"
fi

# 看门狗必须一起停：它每 2 分钟检查 3001，发现不监听就 restart napcat，
# 停了服务却留着它，等于让那个号继续反复登录、继续被风控。
for unit in napcat-watchdog.timer napcat-watchdog.service; do
    if systemctl list-unit-files 2>/dev/null | grep -q "^${unit}"; then
        $SUDO systemctl stop "$unit" 2>/dev/null && echo "  已停 $unit"
        $SUDO systemctl disable "$unit" 2>/dev/null && echo "  已取消 $unit 开机自启"
    fi
done

echo
echo "[6/6] 启动猫娘 + 官方机器人"
$SUDO docker compose --profile official up -d catgirl qqbot
sleep 6
$SUDO docker compose --profile official ps

cat <<'TIP'

==============================================================
 启动完成。看日志：
   sudo docker compose logs -f qqbot

 日志里出现「身份确认：xxx」＝ 通了。
 出现「已连上官方网关（正式）」＝ 长连接建立成功。

 最后一步（认主）：
   1. 用你的 QQ 私聊这个机器人，随便说一句话
   2. 日志里会打印「发现一个 openid：xxxxx」
   3. 把它填进 .env：
        CATGIRL_QQBOT_OWNER_OPENID=xxxxx
   4. sudo docker compose --profile official restart qqbot

 排查顺序（连上但没反应时）：
   a. 开发设置 → 事件订阅与回调地址，必须是 WebSocket
      （配了 Webhook 回调地址，WebSocket 就失效，而且不好退回）
   b. 群里必须 @ 它；私聊要先加好友
   c. 个人主体只能拉进自己是群主/管理员的群
   d. 要给别人用：开发设置 → 内部体验号码（最多 20 个）
==============================================================
TIP
