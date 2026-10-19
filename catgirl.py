"""命令行入口：本地终端里和猫娘聊天。

    python catgirl.py
    python catgirl.py --provider deepseek --model deepseek-chat
    python catgirl.py --provider dashscope --model qwen-plus

配置优先级：命令行参数 > 环境变量 > config.json > provider 预设。
线上部署请用 server.py，这里只服务本地调试。
"""
import argparse
import sys

from config import PROVIDERS, describe, load_config
from core import CatgirlCore


def setup_console():
    """Windows 控制台默认不是 UTF-8，中文和符号会乱码。"""
    for stream in (sys.stdout, sys.stderr, sys.stdin):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def build_parser():
    parser = argparse.ArgumentParser(description="本地 AI 猫娘（支持本地模型和在线大模型）")
    parser.add_argument("--provider", choices=sorted(PROVIDERS), help="服务商预设")
    parser.add_argument("--model", help="对话模型名")
    parser.add_argument("--base-url", dest="baseUrl", help="OpenAI 兼容接口地址")
    parser.add_argument("--embed-model", dest="embedModel", help="向量模型名")
    parser.add_argument("--api-key", dest="apiKey", help="接口密钥（也可用环境变量）")
    parser.add_argument("--db", help="记忆库路径，默认 data/memory.db")
    parser.add_argument("--name", help="猫娘名字")
    parser.add_argument("--no-stream", dest="noStream", action="store_true", help="关闭流式输出")
    return parser


def print_banner(core, cfg):
    info = core.profile()
    print(f"—— {info['name']} 已上线 ——")
    print(
        f"{describe(cfg)['provider']} / {info['model']} ｜ 记忆 {info['memories']} 条 "
        f"｜ 好感度 {info['affinity']}/100"
    )
    if not info["embedding"]:
        print("[提示] 没配置向量模型，记忆只能按重要度和新鲜度排序（建议配 bge-m3 一类）")
    print(f"{info['gap']}。输入 /help 看命令，/quit 退出。\n")


def main():
    setup_console()
    args = build_parser().parse_args()

    overrides = {
        "provider": args.provider,
        "model": args.model,
        "baseUrl": args.baseUrl,
        "embedModel": args.embedModel,
        "apiKey": args.apiKey,
        "name": args.name,
    }
    cfg = load_config({k: v for k, v in overrides.items() if v})

    core = CatgirlCore(cfg, dbPath=args.db)
    core.bootstrap()
    print_banner(core, cfg)

    while True:
        try:
            raw = input("\n你 > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not raw:
            continue

        if raw.startswith("/"):
            head, _, arg = raw[1:].partition(" ")
            head = head.lower()
            arg = arg.strip()

            if head in ("quit", "exit", "q"):
                break
            elif head == "help":
                print(
                    "\n可用命令：\n"
                    "  /memories [条数]   查看长期记忆\n"
                    "  /remember <内容>   手动记一条\n"
                    "  /forget <编号>     删除某条记忆\n"
                    "  /stats             记忆库统计\n"
                    "  /new               开始新会话（长期记忆保留）\n"
                    "  /quit              退出\n"
                )
            elif head == "memories":
                rows = core.list_memories(int(arg) if arg.isdigit() else 20)
                if not rows:
                    print("（记忆库还是空的）")
                for row in rows:
                    print(f"  #{row['id']:<4} [{row['kind']}｜{row['importance']:.0f}] {row['content']}")
            elif head == "remember":
                if not arg:
                    print("用法：/remember 主人下周三要交稿")
                else:
                    result = core.add_memory(arg)
                    label = "并入" if result["merged"] else "新增"
                    print(f"  {label} #{result['id']}：{arg}")
            elif head == "forget":
                if not arg.isdigit():
                    print("用法：/forget 12（编号看 /memories）")
                else:
                    print("  已删除" if core.forget(int(arg)) else "  没找到这条记忆")
            elif head == "stats":
                print(f"  {core.stats()}")
            elif head == "new":
                print(f"  新会话已开始（{core.new_session()}），长期记忆保留")
            else:
                print("  没有这个命令，/help 看看")
            continue

        sys.stdout.write(f"{core.name} > ")
        sys.stdout.flush()
        try:
            for kind, payload in core.respond_iter(raw):
                if kind == "delta":
                    sys.stdout.write(payload)
                    sys.stdout.flush()
                elif payload.get("error"):
                    print(f"\n[出错了] {payload['error']}")
                else:
                    print()
                    for notice in payload.get("notices", []):
                        print(f"  {notice}")
        except KeyboardInterrupt:
            print("\n[打断] 这句不算。")

    print("\n[收尾] 正在把记忆写完……")
    core.shutdown()
    stats = core.stats()
    print(f"[收尾] 记忆 {stats['memories']} 条，对话 {stats['turns']} 句")
    print(f"[收尾] 晚安，{core.name} 睡下了。")


if __name__ == "__main__":
    main()
