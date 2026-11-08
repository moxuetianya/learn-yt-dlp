# yt-dlp 源码学习文档

> 基于本仓库源码（版本 **2026.10.26**，commit `412e7f0a`）整理。
> 文中引用的行号均以该版本为准，后续版本可能偏移，请以"文件 + 类/函数名"为锚点检索。

## 这套文档讲什么

yt-dlp 是目前最强的开源命令行视频下载器（youtube-dl 的活跃分支），支持 1800+ 站点。
它的真正价值不止是"能下视频"，而在于**用一套干净的架构驯服了极度混乱的现实世界**：

- 上千个网站，每个网站的页面结构、API、反爬手段都不同，而且随时在变；
- 现代流媒体把"一个视频"拆成了独立的音频流 + 视频流（DASH/HLS），还要解密、合并；
- 网络本身不可靠：断线、限速、封禁、地域限制；
- 站点要求身份：Cookie、登录、令牌、甚至要求你"证明自己是浏览器"。

这套文档按"**它要解决什么问题 → 它怎么解决 → 源码在哪 → 动手验证**"的思路展开。

## 目录

| 文档 | 主题 | 对应你关心的问题 |
|---|---|---|
| [01-overview.md](01-overview.md) | 项目概览、要解决的问题、总体架构 | **主要解决了什么问题**、原理总纲 |
| [02-core-pipeline.md](02-core-pipeline.md) | 从命令行到文件落盘的完整流程、info_dict 数据流、格式选择 | 原理：核心调度引擎 |
| [03-extractor-system.md](03-extractor-system.md) | 提取器体系：模板方法、注册表、懒加载、Generic 兜底、插件、YouTube 案例 | 原理：如何适配上千站点 |
| [04-networking.md](04-networking.md) | 网络层：RequestDirector/Handler、异常即重试策略、代理、TLS 指纹伪装 | 原理：网络抽象 |
| [05-network-stability.md](05-network-stability.md) | 网络不稳定处理：四层重试、断点续传、分片并发、限速与节流 | **如何处理的网络不稳定** |
| [06-authentication.md](06-authentication.md) | 认证：Cookie 文件、浏览器 Cookie 解密、netrc、账号密码、PO Token、JS 挑战 | **如何处理的认证** |
| [07-audio-video.md](07-audio-video.md) | 音视频处理：HLS/DASH、分离流下载、合并、后处理家族 | **如何处理的音频、视频** |
| [08-external-tools.md](08-external-tools.md) | 外部工具与依赖生态：ffmpeg、aria2c、curl_cffi、deno/node…… | **使用了哪些其他工具** |

## 配套实践代码（`code/` 目录）

所有脚本**不依赖外网**（用本地 HTTP 服务器 / 本地生成的媒体文件），可直接运行验证文档结论：

| 脚本 | 验证内容 |
|---|---|
| [code/01_info_dict_and_formats.py](code/01_info_dict_and_formats.py) | API 用法、info_dict 结构、输出模板引擎 |
| [code/02_format_selection.py](code/02_format_selection.py) | `-f` 格式选择 DSL 的编译与执行、`bv+ba` 虚拟合并格式 |
| [code/03_retry_and_resume.py](code/03_retry_and_resume.py) | **本地服务器模拟断线**：验证重试 + Range 断点续传 + `.part` 文件 |
| [code/04_network_stack.py](code/04_network_stack.py) | 网络 handler 注册表、按请求路由、异常体系分类 |
| [code/05_jsinterp_and_aes.py](code/05_jsinterp_and_aes.py) | 内置 JS 解释器、纯 Python AES（无 pycryptodomex 时的回退） |
| [code/06_ffmpeg_postprocess.py](code/06_ffmpeg_postprocess.py) | ffmpeg 生成分离流 → 合并 → 提取音频 → remux，全链路后处理 |

运行方式（在仓库根目录）：

```bash
python3 lessons/code/01_info_dict_and_formats.py   # 逐个运行即可
```

环境要求：Python 3.11+，`ffmpeg`/`ffprobe` 在 PATH 中（脚本 06 需要）。
可选依赖（`curl_cffi`、`pycryptodomex`）不装也能跑——这本身就是文档要讲的"优雅降级"设计的活例子。

> **验证状态**：全部 6 个脚本已在当前环境实测通过（Python 3.12.3 / ffmpeg 6.1.1 /
> 未安装 curl_cffi 与 pycryptodomex / requests 版本过旧被拒——恰好完整演示了降级链）。
> 脚本输出中的"意外"正是活教材：如本机 requests 因版本 < 2.32.2 被网络层拒绝注册，
> 回退到 urllib 后端，下载流程照样完整跑通。

## 建议学习路线

```
01 概览（半小时建立全局感）
   │
02 核心流程 ──────────► 最重要的一篇，其他都是它的展开
   │
   ├─► 03 提取器（信息从哪来）
   ├─► 04+05 网络（请求怎么发、坏了怎么办）
   ├─► 06 认证（身份怎么带）
   └─► 07 音视频（字节怎么变成能播放的文件）
        │
08 工具生态（收尾：它站在哪些巨人肩膀上）
```

每篇文档末尾有"动手验证"指向对应脚本；强烈建议边读边跑。

## 全局速查：一次 `yt-dlp URL` 背后发生了什么

```
命令行参数 (options.py 解析，四级配置文件分层合并)
        │
        ▼
YoutubeDL 引擎 (YoutubeDL.py, ~4500 行的调度中枢)
        │  extract_info: 遍历提取器注册表，找到能处理该 URL 的 InfoExtractor
        ▼
InfoExtractor (extractor/, 1700+ 站点适配器)
        │  下载网页/API → 解析出 info_dict {id, title, formats:[...]}
        │  （网络请求经 RequestDirector 路由到 urllib/requests/curl_cffi）
        ▼
process_video_result: 格式清洗 → FormatSorter 排序 → 格式选择器挑出 bv+ba
        │
        ▼
process_info: 展开输出模板得到文件名 → FileDownloader (downloader/)
        │  按 protocol 路由: HttpFD / HlsFD / DashSegmentsFD / FFmpegFD...
        │  .part 断点续传 + .ytdl 分片状态 + RetryManager 重试
        ▼
PostProcessor 链 (postprocessor/)
        │  FFmpegMergerPP 合并音视频 → 提取音频 → 嵌字幕/封面 → 移动文件
        ▼
最终文件落盘 + 写 download archive
```

后面每一篇，就是把这张图的某一块放大讲透。
