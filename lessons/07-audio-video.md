# 07 · 音视频处理：从分离流到可播放的文件

> 本篇回答：**"下载视频"为什么变成了一场音频工程 + 视频工程 + 容器工程？**
> 覆盖：formats 模型 → 按协议下载（HLS/DASH）→ 合并 → ffmpeg 后处理家族。
> 配套实践：`lessons/code/06_ffmpeg_postprocess.py`（本地生成素材，全链路可复现）。

## 1. 问题的根源：现代流媒体没有"一个文件"

| 时代 | 形态 | 下载难度 |
|---|---|---|
| Flash 时代 | 一个 `.flv` 直链 | GET 保存完事 |
| HLS/DASH 时代 | m3u8/MPD **清单** + 几百个 TS/MP4 分片 | 要解析清单、并发下载、可能解密 |
| 分离流时代 | YouTube 1080p+ **只有** `video-only` + `audio-only` 两条流 | 必须下载两路再**合并** |
| 反爬时代 | 流地址带签名/令牌，还会节流 | 要解挑战、断点重试、换格式 |

所以 yt-dlp 的 info_dict 里，一个"视频"是一组 formats，每个 format 声明：

```python
{'format_id': '137', 'ext': 'mp4', 'protocol': 'https',
 'vcodec': 'avc1.640028',      # 'none' = 纯音频流
 'acodec': 'none',             # 'none' = 纯视频流
 'height': 1080, 'fps': 30, 'tbr': 4000, 'filesize': ...}
```

`vcodec`/`acodec` 是否为 `none` 决定了它是"预合并流 / 纯视频 / 纯音频"——
这也是 `-f b/bv/ba/bv*` 选择语法的过滤基础（见 [02](02-core-pipeline.md) §7）。

## 2. 下载器按协议路由

`dl()` 门面按 `info['protocol']` 查 `PROTOCOL_MAP`（`downloader/__init__.py`）：

| protocol | 下载器 | 场景 |
|---|---|---|
| `http/https` | `HttpFD` | 直链（Range 续传，见 [05](05-network-stability.md) §3） |
| `m3u8_native` | `HlsFD` | HLS（yt-dlp 自己解析下载） |
| `m3u8` | `FFmpegFD` | HLS（委派 ffmpeg，含多数直播） |
| `http_dash_segments` | `DashSegmentsFD` | DASH 清单+分片 |
| `rtmp/rtsp/mms...` | `RtmpFD`/`FFmpegFD` | 传统流媒体 |
| `f4m` / `ism` | `F4mFD` / `IsmFD` | Adobe HDS / Smooth Streaming（遗产协议） |
| `websocket` | `WebSocketFD` | 弹幕/live chat 等 |

**分工原则**：extractor 负责"清单 → formats/fragments 结构"（如
`_parse_m3u8_formats_and_subtitles` 在基类），downloader 负责"fragments → 文件"。

## 3. HLS：m3u8 的完整处理（`downloader/hls.py`）

`HlsFD.real_download` 把 media playlist 解析成 fragment 列表，交给分片框架：

| m3u8 标签 | 处理 |
|---|---|
| `#EXT-X-KEY:METHOD=AES-128` | key URL + IV；**IV 缺省时用 media_sequence 大端 8 字节**；key 按 URL 缓存；解密走 `aes.py`（装 pycryptodomex 走原生，否则纯 Python——后者会警告"极慢"） |
| `#EXT-X-BYTERANGE` | `<len>[@offset]` → Range 头；无 offset 时**延续上一片的 end** |
| `#EXT-X-MAP` | 初始化段作为第 1 片 |
| `#EXT-X-DISCONTINUITY` | 配合 `--hls-split-discontinuity` 拆分 |
| 私有标签（ANVATO/UPLYNK） | 识别广告分片并跳过 |
| FairPlay/PlayReady/FAXS | `_has_drm` 正则识别 → 不选/报错（DRM 不碰） |

原生搞不定时（DRM 之外的边界情况）自动**委派 FFmpegFD**。

**WebVTT 字幕的特殊处理**：HLS 字幕每片独立时间轴，还受 MPEG PES 33bit 时间戳
溢出影响。HlsFD 内置打包器：逐片解析 cue、跨片时间轴对齐、时间戳溢出补偿、
去重窗口——状态就存在 `.ytdl` 的 `extra_state` 里。

## 4. DASH 与双流并行（`downloader/dash.py` + `fragment.py`）

MPD 清单在 extractor 侧展开成 `fmt['fragments']`（列表或**生成器**——
`--live-from-start` 用生成器懒求值）。`requested_formats`（视频+音频两条 track）时
`download_and_append_fragments_multiple` 各起一个线程池**同时下载两路分片**，
全部追加完成后才进入合并。

## 5. 合并：两条流怎么变成一个文件

两条路径（见 [02](02-core-pipeline.md) §7.2）：

```
路径A（首选）: FFmpegFD can_merge_formats → 单个 ffmpeg 进程
              -i <视频URL> -i <音频URL> -map 0:v:0 -map 1:a:0 -c copy <输出>
              边下边合，磁盘上永远只有一个文件

路径B（默认）: 两路分别下载成 title.f137.mp4 / title.f251.webm
              → 动态追加 FFmpegMergerPP 到 __postprocessors
              → post_process 阶段执行合并，随后删除两个源文件
```

`FFmpegMergerPP`（`postprocessor/ffmpeg.py:822`）的命令构造逻辑：
对每条输入流按 codec 决定 `-map i:a:0` / `-map i:v:0`；
m3u8 来源的 aac 音频自动补 `-bsf:a aac_adtstoasc`（TS 容器的 ADTS 头要剥掉才能进 MP4）。

**容器怎么选**（`get_compatible_ext`，`utils/_utils.py:3091`）：

```
用户 --merge-output-format 优先 → 否则按 codec 兼容表试 mp4 → webm
  （h264+aac → mp4；vp9/av1+opus → webm）
多条视频流或多条音频流（如 bv+ba+ba）→ 直接 mkv
全部不匹配 → 兜底 mkv（几乎容纳任意 codec 组合）
```

另有一处强制 mkv：结果是 webm 且要 `--embed-thumbnail`（webm 不支持封面）。

## 6. 后处理器家族（`postprocessor/`）

### 6.1 run 协议与 8 个时机

```python
class PostProcessor:
    def run(self, info):        # 读 info['filepath']，返回 (待删文件列表, 更新后的info)
```

- 元类自动包装 `run`：执行前后发 `started/finished` 进度事件
  （对接 `--postprocessor-hooks`）；
- `_restrict_to(images=False)` 之类的装饰器自动按 vcodec/acodec 判断媒体类型，
  跳过不适用的 PP（如对纯音频文件不跑视频相关 PP）；
- `POSTPROCESS_WHEN = ('pre_process', 'after_filter', 'video', 'before_dl',
  'post_process', 'after_move', 'after_video', 'playlist')`
  ——同一个流水线在不同生命周期点触发。合并发生在 `post_process`，
  用户 PP 在合并**之后**（ExtractAudio 要的是合并产物）。

### 6.2 FFmpegPostProcessor 基础设施

- 定位 ffmpeg：`ffmpeg_location` 参数（目录或可执行文件）否则 PATH；
  同时定位 ffprobe；
- 版本探测跑一次 `-bsfs`，除版本号外还探测特性（`fdk`/`setts` bsf 等）并全局缓存；
- `real_run_ffmpeg`：输入/输出 `(路径, 参数)` 列表拼接；输出自动加
  `-movflags +faststart`（网络播放优化）；文件名一律加 `file:` 前缀
  （防文件名里的 `:` 被当成协议）；输出 mtime 对齐最旧输入；
- `get_audio_codec`/`get_metadata_object`：ffprobe 探测（JSON 输出）。

### 6.3 主要 PP 一览

| PP | 选项 | 干什么 |
|---|---|---|
| `FFmpegMergerPP` | 自动（bv+ba） | `-c copy` 合并，见 §5 |
| `FFmpegExtractAudioPP` | `-x` | 提取音频。**能 copy 就 copy**（源是 aac 直接转封装成 m4a，秒完成）；否则编码；`--audio-quality` >10 视为 kbps（`-b:a`），0-10 映射编码器质量档 |
| `FFmpegVideoRemuxerPP` | `--remux-video` | **仅换容器**：`-c copy` 流内编码原样搬。无损、秒级 |
| `FFmpegVideoConvertorPP` | `--recode-video` | **重编码**：不带 `-c copy`。需要编码器、损画质、慢 |
| `FFmpegEmbedSubtitlePP` | `--embed-subs` | mp4/mov/webm/mkv 嵌字幕，每字幕一个输入 + 语言 metadata；webm 仅收 vtt |
| `FFmpegMetadataPP` | `--add-metadata/--add-chapters` | 手写 `;FFMETADATA1` 章节文件；mkv 还能 `-attach` 挂 info.json |
| `EmbedThumbnailPP` | `--embed-thumbnail` | 按容器四路：mp3→ffmpeg 封面流；mkv→attach；m4a/mp4→mutagen `covr` 原子→AtomicParsley→ffmpeg 三级 fallback；ogg/opus/flac→mutagen |
| `FFmpegSplitChaptersPP` | `--split-chapters` | 每章一次 `-ss/-t` + copy 切片（可选先重打关键帧） |
| `FFmpegSubtitlesConvertorPP` | `--convert-subs` | dfxp/ttml 先经纯 Python 的 `dfxp2srt`，再 ffmpeg 转 srt/vtt/ass |
| `SponsorBlockPP` + `ModifyChaptersPP` | `--sponsorblock-*` | SponsorBlock API 拿章节 → 切割+concat 删除赞助片段（边界无关键帧时强制重打） |
| `FFmpegConcatPP` | `--concat-playlist` | ffprobe 校验各文件 codec 一致后 concat demuxer 拼接 |
| Fixup 家族 | 自动 | 按 extractor 留下的提示字段动态追加：`FixupStretched`（拉伸像素比）、`FixupM3u8`（TS 内容配 MP4 名）、`FixupTimestamp`（setts bsf 免重编码）等——**"我知道这个站的文件有毛病"的机器可读表达** |
| `MoveFilesAfterDownloadPP` | 自动（永远最后） | 把所有产物移到最终目录 |

**remux vs recode 的全部区别**就是有没有 `-c copy`——一个是搬运，一个是重做。

## 7. 临时文件命名法典（高频面试题）

| 模式 | 生成处 | 生命周期 |
|---|---|---|
| `<name>.part` | `FileDownloader.temp_name` | 下载中；成功后改名去掉；`--no-part` 禁用 |
| `<name>.ytdl` | `FileDownloader.ytdl_filename` | 分片状态 JSON；每片重写、完成删除、损坏清零 |
| `<name>.part-Frag<N>` | `FragmentFD._download_fragment` | 单个分片；追加进主文件后删除 |
| `<name>.f<format_id>.<ext>` | `process_info` | `bv+ba` 的两路独立文件（如 `title.f137.mp4`）；合并成功后删 |
| `<name>.temp.<ext>` | 各 PP | 后处理中间产物，`os.replace` 覆盖 |
| `<name>.orig.<ext>` | ExtractAudioPP | 转码前的原始文件备份（`--keep-video` 保留） |
| `<out>.concat` | FFmpeg PP | ffconcat 清单，用完即删 |

**共同语义：任何带后缀的临时文件都可以安全删除；没有后缀 = 最终产物。**

## 8. 为什么这些都必须懂一点多媒体基础

yt-dlp 源码里随处可见多媒体领域的"常识"被代码化：

- 容器（mp4/mkv/webm）与编码（h264/vp9/opus）是两回事——remux/recode 的区别、
  mkv 兜底、webm 不收封面，全建立在这个区分上；
- 关键帧决定切片边界——SponsorBlock 删片段、`--download-sections` 切段都绕不开；
- `-movflags +faststart` 把 moov atom 挪到文件头，网络播放才不用下完整个文件；
- aac 的 ADTS 头是 TS 容器专属，进 MP4 必须剥（`aac_adtstoasc`）。

## 9. 动手验证

```bash
python3 lessons/code/06_ffmpeg_postprocess.py
```

脚本流程：ffmpeg 生成 `video_only.mp4`（testsrc2）与 `audio_only.m4a`（正弦波）
→ 模拟 `requested_formats` 跑 **FFmpegMergerPP** 合并 →
**FFmpegExtractAudioPP** 提取音频 → **FFmpegVideoRemuxerPP** 换容器成 mkv →
每步用 ffprobe 打印流清单，眼见为实地验证"分离流→合并→提取→remux"全链路。

**本环境实测输出（节选）**：

```
== 1. 生成素材（模拟 bv+ba 下载产物：两条独立流文件）
   video_only.mp4: ['video:h264']          ← 纯视频流（acodec=none 的下载产物）
   audio_only.m4a: ['audio:aac']           ← 纯音频流

== 2. FFmpegMergerPP
   合并产物: merged.mp4: ['video:h264', 'audio:aac']
   返回的待删文件列表: ['video_only.mp4', 'audio_only.m4a']   ← run() 契约的前半

== 3. FFmpegExtractAudioPP -x（源是 aac → 直接转封装 m4a，不重编码）
   提取产物: merged.m4a: ['audio:aac']     ← copy 路径：没有编码器参与

== 4. FFmpegVideoRemuxerPP --remux-video mkv
   remux 产物: merged.mkv: ['video:h264', 'audio:aac']  ← 编码原样搬运（无损、秒级）
```

下一篇 → [08 外部工具生态](08-external-tools.md)：本篇反复出现的 ffmpeg 之外，
yt-dlp 还借了哪些力？
