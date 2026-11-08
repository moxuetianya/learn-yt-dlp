#!/usr/bin/env python3
"""
实践 01：YoutubeDL 作为 Python 库 + info_dict 结构 + 输出模板引擎

验证文档：
  - lessons/01-overview.md §3.2  「一个数据结构贯穿始终：info_dict」
  - lessons/02-core-pipeline.md §5  「info_dict 的数据流演变」
  - lessons/02-core-pipeline.md §8  「prepare_filename / 输出模板」

纯离线运行，不发任何网络请求。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # 仓库根目录

from yt_dlp import YoutubeDL


def main():
    # ── 1. 引擎即库：YoutubeDL(params)，与 CLI 殊途同归 ──────────────────
    # CLI 层 parse_options() 最终产出的就是这样一个字典（lessons/02 §1）
    ydl = YoutubeDL(params={
        'quiet': True,
        'no_warnings': True,
        'outtmpl': '%(title)s [%(id)s].%(ext)s',   # 输出模板语言
        'format': 'bestvideo+bestaudio/best',       # 默认格式规格（会被预编译）
    })

    print('== 1. YoutubeDL 引擎构造成功（CLI 与 API 的唯一接口是 params 字典）')
    print('   格式选择器已在构造期预编译（-f 语法错误会在任何网络请求之前暴露）:')
    print('  ', ydl.format_selector)

    # ── 2. 手工构造 info_dict（正常流程中它由 extractor 产出）───────────
    # 选中格式后，format 级字段会 update() 上浮到顶层（lessons/02 §5 表 ⑥）
    info = {
        'id': 'BaW_jenozKc',
        'title': 'yt-dlp test video',
        'extractor': 'youtube',
        'formats': [
            {'format_id': '140', 'ext': 'm4a', 'protocol': 'https',
             'vcodec': 'none', 'acodec': 'mp4a.40.2', 'abr': 128, 'tbr': 128},
            {'format_id': '137', 'ext': 'mp4', 'protocol': 'https',
             'vcodec': 'avc1.640028', 'acodec': 'none', 'height': 1080,
             'fps': 30, 'tbr': 4000},
            {'format_id': '18', 'ext': 'mp4', 'protocol': 'https',
             'vcodec': 'avc1.42001E', 'acodec': 'mp4a.40.2',
             'height': 360, 'tbr': 500},
        ],
    }

    print('\n== 2. extractor 产出的最小 info_dict（3 条格式：纯音频/纯视频/预合并）')
    for f in info['formats']:
        kind = '纯音频流' if f['vcodec'] == 'none' else ('纯视频流' if f['acodec'] == 'none' else '预合并流')
        print(f"   {f['format_id']:>3}  {kind}  {f}")

    # ── 3. 输出模板引擎：prepare_filename ───────────────────────────────
    # 这是 yt-dlp 实际上的"模板语言"：字段引用、日期格式、替代值、算术……
    chosen = dict(info['formats'][1])           # 模拟选中 137（纯视频）
    merged = dict(info); merged.update(chosen)  # ← 字段上浮就发生在这一步
    merged.update({'resolution': '1920x1080'})

    print('\n== 3. 输出模板展开（prepare_filename）')
    for tmpl in [
        '%(title)s [%(id)s].%(ext)s',
        '%(extractor)s/%(height)sp.%(ext)s',
        '分辨率=%(resolution)s 码率=%(tbr)dk',
        '空字段带默认值: %(uploader|unknown)s',
        '字段嵌套与格式化: %(id).5s...',           # 类 printf 截断
    ]:
        # outtmpl 支持 dict 形式按类型分别指定（default/thumbnail/screenshot...）
        ydl.params['outtmpl'] = {'default': tmpl}
        print(f'   模板: {tmpl!r:50} → {ydl.prepare_filename(merged)!r}')

    # ── 4. simulate 模式：--skip-download/--dump-json 的实现基础 ─────────
    # process_info 在 prepare_filename 之后、dl() 之前检查 simulate 即返回
    print('\n== 4. 结论')
    print('   · info_dict 是唯一贯穿 提取→选择→下载→后处理 的数据结构')
    print('   · 选中的 format 字段上浮顶层后，输出模板才能用 %(height)s 等字段')
    print('   · YoutubeDL 引擎不实现任何协议，只做调度（下载/提取/后处理各自注册表）')


if __name__ == '__main__':
    main()
