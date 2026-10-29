"""假 OneBot v11 协议端（仅用于本地验证 qq.py）。

用法：
    python deploy/fake_onebot.py events.json [保持秒数]

events.json 形如：
    [[1.5, {"post_type": "message", "message_type": "private", ...}]]
收到的 action 会以 ACTION 行打印到 stdout。
"""
import asyncio
import json
import sys

from websockets.asyncio.server import serve

EVENTS = json.load(open(sys.argv[1], encoding="utf-8")) if len(sys.argv) > 1 else []
HOLD = float(sys.argv[2]) if len(sys.argv) > 2 else 30.0
CONNS = []


async def handler(ws):
    CONNS.append(ws)
    try:
        async for raw in ws:
            data = json.loads(raw)
            action, params, echo = data.get("action"), data.get("params"), data.get("echo")
            if action == "get_login_info":
                reply = {"status": "ok", "retcode": 0, "echo": echo,
                         "data": {"user_id": 10001, "nickname": "洛天依"}}
                await ws.send(json.dumps(reply))
                continue

            text = ""
            for seg in (params or {}).get("message", []):
                if seg.get("type") == "text":
                    text += seg["data"].get("text", "")
                else:
                    text += f"[{seg.get('type')}]"
            print(f"ACTION {action} payload={json.dumps(params, ensure_ascii=False)[:120]} "
                  f"text={json.dumps(text, ensure_ascii=False)}", flush=True)
            await ws.send(json.dumps({"status": "ok", "retcode": 0, "echo": echo,
                                      "data": {"message_id": 900 + len(CONNS)}}))
    except Exception as exc:
        print("HANDLER_END", repr(exc), flush=True)
    finally:
        if ws in CONNS:
            CONNS.remove(ws)


async def main():
    async with serve(handler, "127.0.0.1", 3099):
        print("FAKE_READY", flush=True)
        await asyncio.sleep(1.0)
        for delay, event in EVENTS:
            await asyncio.sleep(delay)
            print("PUSH", json.dumps(event, ensure_ascii=False)[:160], flush=True)
            for ws in list(CONNS):
                await ws.send(json.dumps(event, ensure_ascii=False))
        await asyncio.sleep(HOLD)
        print("DONE", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
