#!/usr/bin/env python3
"""
实践 06：ffmpeg 后处理全链路——分离流合并 → 提取音频 → remux 换容器

验证文档：
  - lessons/07-audio-video.md §5 「合并：两条流怎么变成一个文件」
  - lessons/07-audio-video.md §6 「后处理器家族」
      · FFmpegMergerPP：requested_formats → -map i:v:0 / i:a:0 -c copy
      · FFmpegExtractAudioPP：能 copy 就 copy（aac→m4a 秒完成）
      · FFmpegVideoRemuxerPP：仅换容器，流内编码原样搬运
  - lessons/07-audio-video.md §7 「临时文件命名」

需要 PATH 里有 ffmpeg/ffprobe；纯离线运行（素材本地生成）。
"""
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from yt_dlp import YoutubeDL
from yt_dlp.postprocessor import (
    FFmpegExtractAudioPP,
    FFmpegMergerPP,
    FFmpegVideoRemuxerPP,
)
from yt_dlp.utils import prepend_extension

WORKDIR = Path('/home/peter/.tmp/opencode/lesson06')


def sh(*cmd):
    subprocess.run(cmd, check=True, capture_output=True)


def streams(path):
    """ffprobe 列出文件的所有流（眼见为实）。"""
    out = subprocess.check_output([
        'ffprobe', '-v', 'quiet', '-print_format', 'json',
        '-show_streams', str(path)])
    return [
        f"{s['codec_type']}:{s.get('codec_name')}"
        for s in json.loads(out)['streams']]


def main():
    WORKDIR.mkdir(parents=True, exist_ok=True)
    video = WORKDIR / 'video_only.mp4'
    audio = WORKDIR / 'audio_only.m4a'

    # ── 1. 用 ffmpeg 生成素材：纯视频流 + 纯音频流（模拟 DASH 分离流）──
    print('== 1. 生成素材（模拟 bv+ba 下载产物：两条独立流文件）')
    sh('ffmpeg', '-y', '-loglevel', 'error',
       '-f', 'lavfi', '-i', 'testsrc2=duration=3:size=320x240:rate=10',
       '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(video))
    sh('ffmpeg', '-y', '-loglevel', 'error',
       '-f', 'lavfi', '-i', 'sine=frequency=440:duration=3',
       '-c:a', 'aac', '-b:a', '128k', str(audio))
    print(f'   {video.name}: {streams(video)}')
    print(f'   {audio.name}: {streams(audio)}')

    ydl = YoutubeDL(params={'quiet': True, 'no_warnings': True})

    # ── 2. FFmpegMergerPP：bv+ba 的核心后处理器 ─────────────────────────
    print('\n== 2. FFmpegMergerPP（process_info 动态追加的合并器，同一条 PP 流水线）')
    merged = WORKDIR / 'merged.mp4'
    pp = FFmpegMergerPP(ydl)
    # run(info) 契约：读 filepath / requested_formats / __files_to_merge
    info = {
        'filepath': str(merged),
        'requested_formats': [
            {'acodec': 'none', 'protocol': 'https', 'filepath': str(video)},
            {'vcodec': 'none', 'protocol': 'https', 'filepath': str(audio)},
        ],
        '__files_to_merge': [str(video), str(audio)],
        'ext': 'mp4',
    }
    files_to_delete, info = pp.run(info)
    print(f'   合并产物: {merged.name}: {streams(merged)}')
    print(f'   返回的待删文件列表: {[Path(p).name for p in files_to_delete]}')
    print('   ↳ 构造的 ffmpeg 命令等价于: ffmpeg -i video -i audio '
          '-map 0:v:0 -map 1:a:0 -c copy merged.mp4')

    # ── 3. FFmpegExtractAudioPP：能 copy 就 copy ───────────────────────
    print('\n== 3. FFmpegExtractAudioPP -x（源是 aac → 直接转封装 m4a，不重编码）')
    pp = FFmpegExtractAudioPP(ydl, preferredcodec='m4a')
    info = {'filepath': str(merged), 'ext': 'mp4', 'title': 'merged'}
    files_to_delete, info = pp.run(info)
    extracted = Path(info['filepath'])
    print(f'   提取产物: {extracted.name}: {streams(extracted)}')
    print(f'   info 更新: ext {info.get("ext")!r}  待删: {[Path(p).name for p in files_to_delete]}')
    print('   ↳ 若指定 mp3 等不同编码才会真正调用编码器（--audio-quality）')

    # ── 4. FFmpegVideoRemuxerPP：仅换容器（-c copy）────────────────────
    print('\n== 4. FFmpegVideoRemuxerPP --remux-video mkv（对比 --recode-video：后者无 -c copy，要重编码）')
    pp = FFmpegVideoRemuxerPP(ydl, preferedformat='mkv')   # 历史拼写就是少个 r
    info = {'filepath': str(merged), 'ext': 'mp4'}
    _, info = pp.run(info)
    remuxed = Path(info['filepath'])
    print(f'   remux 产物: {remuxed.name}: {streams(remuxed)}')
    print('   ↳ 流编码 h264/aac 原样搬运（无损、秒级）；recode 则编码全变')

    print('\n== 5. 验证临时文件命名（lessons/07 §7 命名法典）')
    for p in sorted(WORKDIR.iterdir()):
        print(f'   {p.name}')

    print('\n结论：合并/提取/remux 全部是 ffmpeg 参数构造问题；')
    print('PostProcessor.run(info) 契约 = (files_to_delete, updated_info)。')


if __name__ == '__main__':
    main()
