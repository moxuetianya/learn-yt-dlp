#!/usr/bin/env python3
"""
实践 02：-f 格式选择 DSL 的编译与执行、bv+ba 虚拟合并格式

验证文档：
  - lessons/02-core-pipeline.md §7 「格式选择」
    - build_format_selector 把字符串编译成选择器函数（构造期）
    - _select_formats 运行期执行
    - _merge 合成"虚拟合并格式"（requested_formats / format_id 137+140 / 容器选择）
    - FormatSorter 排序决定 best 语义（列表尾部 = best）

纯离线运行。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from yt_dlp import YoutubeDL
from yt_dlp.utils import FormatSorter


def make_formats():
    """模拟 YouTube 风格的 formats 列表（字段已归一化）。"""
    return [
        {'format_id': '139', 'ext': 'm4a', 'protocol': 'https', 'vcodec': 'none',
         'acodec': 'mp4a.40.5', 'abr': 48, 'tbr': 48, 'filesize': 3_000_000},
        {'format_id': '140', 'ext': 'm4a', 'protocol': 'https', 'vcodec': 'none',
         'acodec': 'mp4a.40.2', 'abr': 128, 'tbr': 128, 'filesize': 8_000_000},
        {'format_id': '134', 'ext': 'mp4', 'protocol': 'https', 'vcodec': 'avc1.4d401e',
         'acodec': 'none', 'height': 360, 'fps': 30, 'tbr': 600},
        {'format_id': '137', 'ext': 'mp4', 'protocol': 'https', 'vcodec': 'avc1.640028',
         'acodec': 'none', 'height': 1080, 'fps': 30, 'tbr': 4000, 'vbr': 3900},
        {'format_id': '248', 'ext': 'webm', 'protocol': 'https', 'vcodec': 'vp9',
         'acodec': 'none', 'height': 1080, 'fps': 30, 'tbr': 2600, 'vbr': 2500},
        {'format_id': '18', 'ext': 'mp4', 'protocol': 'https', 'vcodec': 'avc1.42001E',
         'acodec': 'mp4a.40.2', 'height': 360, 'fps': 30, 'tbr': 500},
    ]


def main():
    ydl = YoutubeDL(params={'quiet': True, 'no_warnings': True})
    formats = make_formats()

    # ── 1. FormatSorter：best/worst 的排序语义 ──────────────────────────
    # 先全序排序（升序），之后选择器"反转取第 N 个"——列表尾部即 best
    sorter = FormatSorter(ydl, [])   # 第二个参数 = 用户 --format-sort 字段（此处为空）
    ranked = sorted(formats, key=sorter.calculate_preference)
    print('== 1. FormatSorter 排序（升序，尾部 = best）')
    for f in ranked:
        print(f"   {f['format_id']:>3}  {f['ext']:<5} vcodec={f['vcodec']:<13} "
              f"acodec={f['acodec']:<12} height={f.get('height', '-')} tbr={f.get('tbr')}")

    # ── 2. 各种 -f 表达式的选择结果 ────────────────────────────────────
    print('\n== 2. 格式选择 DSL（编译 → 执行）')
    for spec in [
        'best', 'worst',                          # 预合并流最好/最差
        'bestvideo', 'bestaudio',                 # 纯视频/纯音频最好
        'bestvideo+bestaudio',                    # 虚拟合并（重点）
        'bestvideo[height>=1080]+bestaudio/best', # 过滤器 + fallback
        'b*',                                     # 任意（含分离流）最好
        'mp4',                                    # 扩展名选择器
        '18',                                     # format_id 字面量
        'all',                                    # 全部
    ]:
        selector = ydl.build_format_selector(spec)
        selected = ydl._select_formats(formats, selector)
        ids = [f['format_id'] for f in selected]
        print(f'   -f {spec!r:42} → {ids}')

    # ── 3. 放大看虚拟合并格式 ──────────────────────────────────────────
    print('\n== 3. bv+ba 产出的"虚拟合并格式"（不是一个真实 URL，而是两条格式的捆绑包）')
    selector = ydl.build_format_selector('bestvideo+bestaudio')
    (merged,) = ydl._select_formats(formats, selector)
    for key in ['format_id', 'ext', 'protocol', 'vcodec', 'acodec', 'height', 'tbr',
                'requested_formats']:
        val = merged.get(key)
        if key == 'requested_formats':
            val = [f['format_id'] for f in val]
        print(f'   {key:>18}: {val}')
    print('   ↓ 解读')
    print('   · format_id 拼接为 248+140；requested_formats 保留原始两条原始格式')
    print('   · 容器选择：vp9(视频)+aac(音频) 进不了 mp4 也进不了 webm → 兜底 mkv')
    print('     （若选中 137(h264)+140(aac) 则会得到 mp4；这就是"默认常下出 mkv"的由来）')
    print('   · tbr = 两流码率之和(2600+128)；下载期才真正分两路，')
    print('     FFmpegMergerPP 是动态追加的后处理器')

    # ── 4. fallback 语义：a/b 从左到右第一个非空整体生效 ────────────────
    print('\n== 4. PICKFIRST 语义：bv[height>2000]+ba/b 时合并失败则退回 b')
    selector = ydl.build_format_selector('bv[height>2000]+ba/b')
    selected = ydl._select_formats(formats, selector)
    print('   -f "bv[height>2000]+ba/b" →',
          [f['format_id'] for f in selected], '（1080p 不满足 >2000，退回 best）')

    # ── 5. 语法错误在编译期暴露 ────────────────────────────────────────
    print('\n== 5. -f 语法错误在构造/编译期报错（不会等到下载一半才失败）')
    try:
        ydl.build_format_selector('best+++bad(')
    except Exception as e:
        print(f'   build_format_selector("best+++bad(") → {type(e).__name__}: {e}')


if __name__ == '__main__':
    main()
