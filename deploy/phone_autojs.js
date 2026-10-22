/**
 * 铃洛 · 手机桥接脚本（AutoX.js）
 *
 * 干什么：监听手机 QQ 的通知，把消息上报给服务器上的 phone.py；
 *        服务器算好回复后推回来，本脚本代她发出去。
 *
 * 为什么这么绕：云服务器上跑 NapCat 这类非官方协议端会被腾讯天天风控。
 * 这台手机上跑的是官方 QQ 客户端、真实设备、家里宽带，腾讯侧看不出异常。
 *
 * 能力边界（通知能给什么就只能拿什么）：
 *   文字：能收能发       图片：只能拿到「［图片］」四个字
 *   语音：只能拿到「［语音］」，也发不出去
 *
 * 使用前提：
 *   1. 手机装 AutoX.js（或 AutoX.js Pro），把本脚本放进脚本目录
 *   2. 系统设置里给 AutoX.js 开「无障碍服务」和「通知使用权」
 *   3. 关掉 QQ 的电池优化、给 AutoX.js 也关掉，允许后台常驻
 *   4. 屏幕设置成常亮 + 不设锁屏（回复要靠无障碍点界面，锁屏下点不了）
 *   5. 改下面「配置」里的地址和 token，跟服务器 .env 里的一致
 */

// ============================ 配置 ============================
var CONFIG = {
    // 服务器地址。手机连家里/公司网络时写服务器内网 IP 或 Tailscale 分配的 IP
    serverUrl: "ws://100.x.x.x:8765",
    // 跟服务器 .env 的 CATGIRL_PHONE_TOKEN 一致；留空表示不校验（只在本地调试用）
    token: "改成一个随机串",
    // 群名列表：标题（通知标题）出现在这里的，才当作群消息。
    // 注意：通知里拿不到群号，只能用群名称匹配，改群名前记得同步
    groupNames: ["猫窝", "测试群"],
    // 通知文本里出现「昵称：内容」这种格式时，也按群消息处理（兜住漏配的群）
    sniffGroupByColon: true,
    // 只把能识别的会话上报；false = 所有 QQ 通知都上报（调试用）
    strictGroups: true
};

// ============================ 依赖 ============================
auto.waitFor();
console.show();
console.setSize(0.9, 0.45);

var PACKAGE_QQ = "com.tencent.mobileqq";
var running = true;
var ws = null;
var reconnectDelay = 2000;
var lastClickAt = 0;

function log(msg) {
    console.log(msg);
}

// ============================ 通知解析 ============================
function normalize(text) {
    return (text == null ? "" : String(text)).replace(/\s+/g, " ").trim();
}

/**
 * 从通知里拆出 chat / sender / text。
 * 私聊：title = 对方昵称，text = 消息正文
 * 群聊：title = 群名，text = "昵称: 消息正文"（QQ 的写法并不总是带冒号）
 */
function parseNotification(n) {
    var title = normalize(n.getTitle());
    var body = normalize(n.getText());
    if (!title && !body) return null;

    // 连续多条会折叠成「3 条新消息」，这种没有内容，跳过
    if (/^\d+\s*条新消息$/.test(body) || /条新消息/.test(body)) {
        log("跳过折叠通知：" + title);
        return null;
    }

    var isGroup = CONFIG.groupNames.indexOf(title) >= 0;
    var sender = "";
    var content = body;

    if (!isGroup && CONFIG.sniffGroupByColon) {
        var m = body.match(/^(.{1,20})[：:]\s*([\s\S]+)$/);
        if (m) {
            isGroup = true;
            sender = m[1];
            content = m[2];
        }
    } else if (isGroup) {
        var m2 = body.match(/^(.{1,20})[：:]\s*([\s\S]+)$/);
        if (m2) {
            sender = m2[1];
            content = m2[2];
        }
    }

    if (!isGroup && CONFIG.strictGroups && CONFIG.groupNames.length > 0 && sender === "") {
        // 私聊：正文原样上交
        content = body;
        sender = title;
    }
    if (!isGroup) sender = title;

    if (!content) return null;

    // 通知里没有 QQ 号，只能用昵称当标识。改昵称会导致记忆换库，注意
    var chat = (isGroup ? "group:" : "private:") + title;
    return {
        chat: chat,
        chatName: title,
        sender: sender || title,
        senderId: "",
        text: content,
        hasImage: /\[图片\]|\[Image\]/.test(content)
    };
}

// ============================ 发送 ============================
/**
 * 打开通知对应的会话。收到消息就先打开，回复才能发到对的窗口里。
 * 点的是通知本身的 contentIntent，等于「用户点通知」，最可靠。
 */
function openChatByNotification(n) {
    try {
        n.click();
        sleep(1200);
        return true;
    } catch (e) {
        log("打开会话失败：" + e);
        return false;
    }
}

/** 在当前会话界面输入并发送。聊天页的输入框是 EditText，发送按钮文案是「发送」 */
function sendInCurrentChat(text) {
    var input = className("EditText").visibleToUser(true).findOne(3000);
    if (!input) {
        input = className("EditText").findOne(2000);
    }
    if (!input) {
        log("找不到输入框，放弃发送");
        return false;
    }
    input.setText(text);
    sleep(400);

    var btn = text("发送").findOne(2500);
    if (!btn) btn = desc("发送").findOne(1000);
    if (!btn) {
        // 部分版本按键没有文字，只能靠输入框右侧区域兜底
        var box = input.bounds();
        click(box.right + 60, box.centerY());
        sleep(300);
        return true;
    }
    btn.click();
    sleep(500);
    return true;
}

/** 回复一条消息：先确认当前页面对应的是哪个会话，再输入 */
function reply(payload) {
    var chunks = payload.chunks || [];
    if (chunks.length === 0) return;
    log("回复 " + payload.chat + "：" + chunks.join(" / "));
    for (var i = 0; i < chunks.length; i++) {
        if (!sendInCurrentChat(chunks[i])) break;
        sleep(800);
    }
}

// ============================ 网络 ============================
function connect() {
    log("连接 " + CONFIG.serverUrl);
    ws = web.newWebSocket(CONFIG.serverUrl, { timeout: 10000 });

    ws.on("open", function () {
        reconnectDelay = 2000;
        log("已连接，发送 hello");
        ws.send(JSON.stringify({
            type: "hello",
            token: CONFIG.token,
            device: device.model + " / Android " + device.release
        }));
    });

    ws.on("message", function (msg, second) {
        var raw = typeof msg === "string" ? msg : (second || "");
        if (!raw) return;
        var data;
        try { data = JSON.parse(raw); } catch (e) { return; }
        if (data.type === "welcome") {
            log("服务器已接受：" + data.name);
        } else if (data.type === "reply") {
            // 网络回调不是 UI 线程，切到主线程再操作界面
            ui.run(function () {
                try { reply(data); } catch (e) { log("发送出错：" + e); }
            });
        } else if (data.type === "pong") {
            // 心跳回执，忽略
        }
    });

    ws.on("closed", function () {
        log("连接断开");
        scheduleReconnect();
    });
    ws.on("failure", function (e) {
        log("连接失败：" + e);
        scheduleReconnect();
    });
}

function scheduleReconnect() {
    if (!running) return;
    // 指数退避，最多 30 秒一次，别把服务器打爆
    setTimeout(function () {
        reconnectDelay = Math.min(reconnectDelay * 2, 30000);
        connect();
    }, reconnectDelay);
}

// ============================ 主循环 ============================
events.observeNotification();
events.on("notification", function (n) {
    if (!n || n.getPackageName() !== PACKAGE_QQ) return;

    var msg = parseNotification(n);
    if (!msg) return;
    log("收到 " + msg.chat + " ｜ " + msg.sender + "：" + msg.text);

    // 先把这个会话打开，回复才不会发错窗口
    openChatByNotification(n);
    lastClickAt = Date.now();

    if (!ws) return;
    msg.id = Date.now() + "-" + Math.floor(Math.random() * 100000);
    msg.type = "message";
    try {
        ws.send(JSON.stringify(msg));
    } catch (e) {
        log("上报失败：" + e);
    }
});

// 心跳：手机在移动网络里容易静默掉线，靠它发现
setInterval(function () {
    if (ws) {
        try { ws.send(JSON.stringify({ type: "ping" })); } catch (e) { }
    }
}, 30000);

connect();
log("铃洛手机桥已启动");
setInterval(function () { }, 1000);  // 保活，别让脚本自行退出
