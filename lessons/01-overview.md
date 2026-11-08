# 01 · 项目概览：yt-dlp 解决什么问题，架构长什么样

> 阅读本文后你应该能回答：yt-dlp 的核心难题是什么？它把代码切成哪几块，为什么这样切？

## 1. 它主要解决了什么问题

表面看 yt-dlp 是"视频下载器"，但它实际同时解决了四类问题：

### 问题一：站点的多样性（Extract 问题）

每个视频网站都有自己的页面结构、私有 API、播放器逻辑。YouTube 改一次播放器代码，
全世界的下载工具就得跟着改。yt-dlp 的答案是把"站点差异"全部隔离进 `yt_dlp/extractor/`——
**一个站点一个文件、一个提取器类**，本版本实测 **1754 个提取器类、940 个模块文件**
（YouTube 已拆成 31 个文件的包），
官方宣称支持 1800+ 站点（见仓库根目录 `supportedsites.md`）。

新站点适配 = 新增一个继承 `InfoExtractor` 的类，**不碰任何核心代码**（详见 [03 提取器体系](03-extractor-system.md)）。

### 问题二：流媒体的复杂性（音视频分离、加密、协议）

现代视频网站早已不给你"一个 mp4 链接"：

- **DASH**：视频流和音频流完全分离，各自按清单（manifest）切片；
- **HLS**：m3u8 清单 + TS 分片，常见 AES-128 加密；
- YouTube 的最高画质（如 1080p60+）**只有分离流**，不提供合并流——想看就必须自己下载两条流再合并；
- URL 往往带签名/令牌（YouTube 的 `n` 挑战、PO Token），需要执行站点的 JS 代码才能拿到可用地址。

yt-dlp 的答案：`downloader/`（按协议下载）+ `postprocessor/`（ffmpeg 合并/转码）+
内置 JS 解释器 + 内置 AES 实现（详见 [07 音视频处理](07-audio-video.md)、[06 认证](06-authentication.md)）。

### 问题三：网络的不可靠（Stability 问题）

断线、限速、服务器 5xx、地域封锁、TLS 指纹检测……yt-dlp 的答案是一整套
**分层重试 + 断点续传 + 分片并发 + 多后端回退**机制（详见 [04 网络层](04-networking.md)、
[05 网络稳定性](05-network-stability.md)）：

- 传输层：`urllib` / `requests` / `curl_cffi` 三个后端，按请求能力动态路由；
- 应用层：四层 `RetryManager` 重试（整文件/分片/文件访问/提取器）；
- 磁盘层：`.part`（断点续传）+ `.ytdl`（分片状态记账）。

### 问题四：身份与反爬（Auth 问题）

- Cookie：支持 Netscape 格式文件，也支持**直接从各浏览器数据库提取并解密**（Windows DPAPI、macOS Keychain、Linux keyring）；
- 账号：`--username/--password`、netrc、OAuth 设备码；
- 反爬：`curl_cffi` 模拟浏览器 TLS 指纹；针对 YouTube 的 nsig 挑战用**外部 JS 运行时**（deno/node）执行真实 player 代码。

详见 [06 认证体系](06-authentication.md)。

> **一句话总结**：yt-dlp = 「站点适配层」×「协议下载层」×「媒体处理层」粘在一个统一调度引擎上，
> 用同一个数据结构（info_dict）从 URL 一路传递到最终文件。

## 2. 源码目录地图

```
yt_dlp/
├── __init__.py        CLI 编排层：解析参数 → 构造 YoutubeDL → download()
├── options.py         全部命令行选项定义（optparse）
├── YoutubeDL.py       ★ 核心引擎（~4500 行）：提取调度/格式选择/下载调度/后处理调度
├── extractor/         ★ 站点适配层（1700+ 提取器）
│   ├── common.py        InfoExtractor 基类（所有提取器的模板）
│   ├── generic.py       兜底提取器（任何 URL 都能试）
│   ├── youtube/         YouTube 提取器（已拆成包：_base/_video/_tab/...）
│   ├── _extractors.py   静态导入清单（注册表数据源）
│   └── extractors.py    运行时装配（排序：YouTube 优先，Generic 垫底）
├── networking/        ★ 网络抽象层
│   ├── common.py        RequestDirector（调度）/ RequestHandler（后端抽象）
│   ├── _urllib.py       后端1：标准库 urllib（零依赖兜底）
│   ├── _requests.py     后端2：requests/urllib3（连接池）
│   ├── _curlcffi.py     后端3：curl_cffi（TLS 指纹模拟）
│   ├── _helper.py       SSL 上下文/SOCKS 隧道/连接建立
│   ├── impersonate.py   浏览器伪装抽象
│   └── exceptions.py    异常体系（= 重试策略的分类依据）
├── downloader/        ★ 下载层（按协议路由）
│   ├── common.py        FileDownloader 基类（进度/限速/临时文件规则）
│   ├── http.py          HttpFD：直链下载 + Range 断点续传
│   ├── fragment.py      FragmentFD：通用分片框架（.ytdl 状态/并发/解密）
│   ├── hls.py / dash.py / f4m.py / ism.py   各流媒体协议
│   └── external.py      外部下载器委托（aria2c/curl/wget/ffmpeg...）
├── postprocessor/     ★ 后处理层（几乎全是 ffmpeg 的封装）
│   ├── common.py        PostProcessor 基类（run 协议 + 8 个执行时机）
│   └── ffmpeg.py        合并/提取音频/换容器/嵌字幕/嵌封面/修容器...
├── utils/             工具集（_utils.py 单文件 ~5000 行，FormatSorter/RetryManager 等）
├── cookies.py         浏览器 Cookie 提取与解密
├── jsinterp.py        内置 JS 解释器（树遍历执行器）
├── aes.py             AES 纯 Python 实现（pycryptodomex 缺失时的回退）
├── socks.py           自实现的 SOCKS4/4a/5 协议（不依赖 PySocks）
├── cache.py           磁盘缓存（nsig 函数/签名时间戳等）
└── plugins.py         插件系统（extractor/PP 插件的加载）
```

（★ = 核心学习路径，建议按顺序读）

## 3. 核心架构思想

### 3.1 三个"可插拔注册表"

yt-dlp 最重要的结构决策：**引擎不实现具体能力，只做调度**。
三个能力维度各自是一个注册表 + 模板方法：

| 能力 | 基类（模板方法） | 注册表 | 引擎侧调用 |
|---|---|---|---|
| 从 URL 提取元数据 | `InfoExtractor._real_extract()` | `YoutubeDL._ies`（有序 dict） | `extract_info()` 遍历找 `suitable()` 的 |
| 按协议下载文件 | `FileDownloader.real_download()` | `downloader/__init__.py` 的 `PROTOCOL_MAP` | `get_suitable_downloader()` 按 `protocol` 查表 |
| 下载后加工文件 | `PostProcessor.run()` | `YoutubeDL._pps`（按 8 个时机分组） | `run_all_pps(when)` 顺序执行 |

### 3.2 一个数据结构贯穿始终：info_dict

从提取器产出到最终落盘，所有模块共享同一个字典（Python dict）：

```python
# 提取器返回的最小形态
{'id': 'dQw4w9WgXcQ', 'title': '...', 'formats': [
    {'format_id': '137', 'url': 'https://...', 'ext': 'mp4',
     'vcodec': 'avc1.640028', 'acodec': 'none', 'height': 1080, 'tbr': 4000},
    {'format_id': '140', 'url': 'https://...', 'ext': 'm4a',
     'vcodec': 'none', 'acodec': 'mp4a.40.2', 'abr': 128},
]}
# 选中格式后字段"上浮"到顶层；下载后新增 _filename/filepath；
# 后处理器读取/写回 filepath —— 同一个 dict 一路传递
```

这个设计让 1700+ 个提取器、若干下载器、几十个后处理器**无需互相 import**，
全部通过 dict 解耦。完整演变过程见 [02 核心流程](02-core-pipeline.md) §5。

### 3.3 依赖的"优雅降级"

yt-dlp 的硬依赖只有 Python 标准库。其余全部可选、缺失时降级而不是崩溃：

```
装了 requests      → 用它（连接池更快），没装 → 回退 urllib（永远可用）
装了 curl_cffi     → 能 --impersonate 模拟浏览器 TLS，没装 → 普通请求照发
装了 pycryptodomex → AES 走原生实现，没装 → 纯 Python AES（慢但正确）
装了 ffmpeg        → 能合并/转码/嵌字幕，没装 → 默认格式自动退回单体流
有外部 JS 运行时    → 能解 YouTube 的 nsig 挑战，没有 → 部分格式不可用
```

验证：本学习环境的 python 就没装 `curl_cffi`/`pycryptodomex`，
跑 `lessons/code/05_jsinterp_and_aes.py` 可以看到纯 Python AES 路径实际生效。

### 3.4 惰性求值无处不在

- **提取器懒加载**：发布版用 `lazy_extractors.py` 桩类（只含 `_VALID_URL` 等静态属性快照），
  URL 匹配时零 import 成本，真正提取某站点时才加载该模块——上千个提取器的冷启动从秒级降到毫秒级；
- **播放列表懒展开**：`entries` 可以是生成器/`LazyList`/`PagedList`，配合 `--flat-playlist`、
  `--break-on-existing` 可以"不下载完元数据就提前终止"；
- **格式检查懒执行**：`--check-formats` 按需探测格式可用性，而不是预先全量探测。

## 4. 工程侧亮点（顺带一读）

- **文档从代码生成**：`options.py` 手写 `--help` 文案 → `yt-dlp --help` 输出经
  `devscripts/make_readme.py` 打补丁生成 README 的选项段 → `prepare_manpage.py` 再生成 man 手册。
  三处文档永不失同步（方向是"代码 → 文档"，不是反过来）。
- **测试矩阵**：`test/` 下有提取器全量测试（`test/test_download.py`，需要网络）、
  网络层单测（`test/test_networking.py`）、JS 解释器测试（`test/test_jsinterp.py`）等。
- **Changelog.md** 记录了每个行为的变更原因，是排查"行为和以前不一样"的第一入口。

## 5. 动手验证

```bash
# 感受提取器注册表规模与优先顺序（YouTube 家族在前，Generic 垫底）
python3 -c "
from yt_dlp.extractor import gen_extractor_classes
ies = list(gen_extractor_classes())          # 注册顺序 = URL 匹配优先级
print('提取器总数:', len(ies)); print('前3个:', [ie.IE_NAME for ie in ies[:3]]); print('最后1个:', ies[-1].IE_NAME)
"
```

下一篇 [02-core-pipeline.md](02-core-pipeline.md)，我们放大图中那一块最大的：`YoutubeDL` 引擎。
