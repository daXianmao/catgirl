"""把专辑目录里的 FLAC 批量转成 MP3，顺手生成曲库 index 条目。

用法：
    python tools/flac2mp3.py --src "D:/音乐/流行歌" --out "D:/AI Tools/_tmp/songs_new"

只转参数里列出的 album 子目录，输出文件名统一成「专辑 - 歌名.mp3」，
歌名去掉「 - 洛天依」「feat. xxx」这些尾巴，模型点歌时好匹配。
"""

import argparse
import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

FFMPEG = os.environ.get("FFMPEG", r"D:/AI Tools/ffmpeg/bin/ffmpeg.exe")
FFPROBE = os.environ.get("FFPROBE", r"D:/AI Tools/ffmpeg/bin/ffprobe.exe")

ALBUMS = ["依如初见", "再生", "四重奏", "无尽蓝"]


def cleanTitle(stem):
    """从文件名里剥出干净的歌名。"""
    name = stem.strip()
    name = re.sub(r"\s*-\s*洛天依.*$", "", name)      # - 洛天依 / - 洛天依 & xxx
    name = re.sub(r"\s*feat\..*$", "", name, flags=re.I)  # feat. xxx
    name = re.sub(r"\s+", " ", name).strip(" -_")
    return name or stem.strip()


def aliasesOf(title):
    """歌名里有空格就拆出词当别名，「再声 Recall」也能被「再声」点到。"""
    parts = [p for p in re.split(r"[\s]+", title) if len(p) >= 2]
    return parts if len(parts) > 1 else []


def probe(path):
    proc = subprocess.run(
        [FFPROBE, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", path],
        capture_output=True, text=True,
    )
    try:
        return float(proc.stdout.strip().splitlines()[0])
    except (ValueError, IndexError):
        return 0.0


def convert(src, dst):
    args = [
        FFMPEG, "-v", "error", "-y", "-i", src,
        "-map_metadata", "0", "-id3v2_version", "3",
        "-codec:a", "libmp3lame", "-q:a", "0", dst,
    ]
    proc = subprocess.run(args, capture_output=True, text=True)
    if proc.returncode != 0 or not os.path.exists(dst) or os.path.getsize(dst) < 4096:
        raise RuntimeError((proc.stderr or "ffmpeg 失败").strip()[:300])
    return dst


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--src", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--albums", default=",".join(ALBUMS))
    parser.add_argument("--jobs", type=int, default=3)
    options = parser.parse_args()

    os.makedirs(options.out, exist_ok=True)
    jobs = []
    for album in [a.strip() for a in options.albums.split(",") if a.strip()]:
        folder = os.path.join(options.src, f"洛天依 {album}")
        if not os.path.isdir(folder):
            print(f"[跳过] 没有目录：{folder}")
            continue
        for name in sorted(os.listdir(folder)):
            if not name.lower().endswith((".flac", ".wav")):
                continue
            title = cleanTitle(os.path.splitext(name)[0])
            target = os.path.join(options.out, f"{album} - {title}.mp3")
            jobs.append((album, name, os.path.join(folder, name), target, title))

    print(f"共 {len(jobs)} 个文件待转换")
    index = {}

    def work(job):
        album, name, src, target, title = job
        try:
            convert(src, target)
            duration = probe(target)
            return job, None, duration
        except Exception as exc:  # noqa: BLE001 - 单个失败不影响整批
            return job, str(exc), 0.0

    with ThreadPoolExecutor(max_workers=options.jobs) as pool:
        for job, error, duration in pool.map(work, jobs):
            album, name, src, target, title = job
            if error:
                print(f"[失败] {name}: {error}")
                continue
            size = os.path.getsize(target) / 1048576
            print(f"[完成] {os.path.basename(target)}  {duration:.0f}s  {size:.1f}MB")
            index[os.path.basename(target)] = {
                "title": title,
                "aliases": aliasesOf(title),
                "album": album,
            }

    indexPath = os.path.join(options.out, "index_part.json")
    with open(indexPath, "w", encoding="utf-8") as handle:
        json.dump(index, handle, ensure_ascii=False, indent=1)
    print(f"索引条目 {len(index)} 条 -> {indexPath}")
    return 0 if len(index) == len(jobs) else 1


if __name__ == "__main__":
    sys.exit(main())
