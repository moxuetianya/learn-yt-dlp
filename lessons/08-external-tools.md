# 08 · 外部工具与依赖生态：yt-dlp 站在谁的肩膀上

> 一个"视频下载器"的源码树里有 JS 解释器、AES 实现、SOCKS 协议、TLS 指纹——
> 因为它把能不依赖的都自己写了，把值得依赖的都接得很好。
> 本篇盘点 yt-dlp 借力的外部工具/库，以及各自的接入方式与降级策略。

## 1. 外部可执行文件

### 1.1 ffmpeg / ffprobe —— 唯一的"准必需"依赖

**用途分布**（按重要性）：

| 功能 | 用哪个 |
|---|---|
| 合并分离流（bv+ba） | ffmpeg |
| 提取音频/转码/remux | ffmpeg |
| 嵌字幕/封面/metadata/章节 | ffmpeg |
| HLS/RTMP 直播下载、部分边界格式 | ffmpeg |
| 探测编码/流信息 | ffprobe |
| SponsorBlock 切片、关键帧重打 | ffmpeg |

接入方式：`FFmpegPostProcessor._determine_executables`（`postprocessor/ffmpeg.py`）——
`--ffmpeg-location`（目录或单个可执行文件）否则搜 PATH；版本与特性探测结果
**全局缓存**（进程内只探测一次）。命令构造全是列表拼接（无 shell），
输出文件加 `file:` 前缀防文件名里的 `:` 被误认为协议。

**没有 ffmpeg 会怎样**：合并/转码类功能全灭，但程序不崩——默认格式选择
自动退回单体 `best`（`_default_format_spec` 检查 `can_merge()`），只下载预合并流。
这就是"优雅降级"的最高优先级案例。

### 1.2 外部下载器（`downloader/external.py`，`--downloader XXX`）

| 工具 | 特色参数 | 适用场景 |
|---|---|---|
| **aria2c** | 固定 `-x16 -j16 -s16 --min-split-size 1M` | 多连接抢带宽；不稳定网络的利器 |
| **curl** | `--continue-at -`、cookie 经 stdin 传入 | 简单可靠 |
| **wget** | `--compression=auto`、`--tries` | 传统环境 |
| axel / httpie | 各自的风格 | 小众偏好 |

接入方式统一为 `ExternalFD` 子类的 `_make_cmd()`（参数→命令行翻译）+
`_call_downloader()`。两个聪明设计：

- cookie **不落盘**：curl≥7.59 直接 `--cookie -` 从 stdin 读，避免明文 cookie 文件；
- **分片委托**：HLS/DASH 时把解析好的分片 URL 清单交给外部下载器批量抓
  （写到 `tmpfilename.frag.urls`），Python 侧再逐片读入、解密、按序拼接——
  兼得 aria2c 的多连接与 yt-dlp 的解密/状态管理。

### 1.3 JS 运行时：deno / node / bun / qjs

YouTube 的 nsig/sig 挑战需要执行真实 player JS（见 [03](03-extractor-system.md) §6.2）。
jsc 框架按优先级探测外部 JS 运行时，在沙箱中执行解题脚本；
`--extractor-args youtubejsc-<name>:...` 可配置。没有 JS 运行时 →
部分 YouTube 高端格式不可用（明确警告而不是静默失败）。

### 1.4 其他可选可执行文件

- **AtomicParsley**：m4a/mp4 嵌封面的三级 fallback 之一（mutagen → AtomicParsley → ffmpeg）；
- **rtmpdump**：RTMP 协议兜底（有原生 RtmpFD，但复杂场景仍可委派）；
- **phantomjs**：历史上的 JS 引擎选项，现已淘汰。

## 2. Python 可选依赖

`pyproject.toml` 的 `[project.optional-dependencies]` 定义分组；运行时全部 try-import，
缺失时按功能降级（`networking/__init__.py` 的注册循环就是活例子）：

| 库 | 用途 | 缺失时的行为 |
|---|---|---|
| **requests**（+urllib3） | 首选 HTTP 后端（连接池） | 回退 urllib 后端（永远可用） |
| **curl_cffi** | 浏览器 TLS 指纹模拟（`--impersonate`） | 伪装功能不可用；需要伪装的站点报错 |
| **websockets** | ws/wss 协议（弹幕、live chat） | 相关功能不可用 |
| **pycryptodomex** | AES 原生实现（浏览器 cookie 解密、HLS 解密） | **纯 Python AES 回退**（`aes.py`，慢但正确） |
| **mutagen** | 音频元数据/封面原子写入 | 嵌封面能力降级 |
| **secretstorage** | Linux GNOME Keyring 读取（cookie 解密） | 该 keyring 路径不可用 |
| brotli / zstandard | 响应解压 | 相应编码不解压 |
| certifi | CA 证书包 | 用系统证书 |

**值得抄的检测模式**：

```python
# networking/__init__.py 的懒加载注册：缺依赖只 warn 不崩溃
for module, attr in (...):
    try:
        register_handler(getattr(importlib.import_module(module), attr))
    except ImportError:
        logger.debug(f'{module} not found, skipping')
```

## 3. 自研替代品：为什么这些轮子要自己造

| 自研模块 | 替代的依赖 | 为什么 |
|---|---|---|
| `socks.py` | PySocks | SOCKS4/4a/5 协议不大（~300 行），省一个硬依赖；还能按需桥接进各后端 |
| `aes.py` 纯 Python 部分 | pycryptodomex | cookie 解密/HLS 解密是核心路径，不能因缺加密库而全灭 |
| `jsinterp.py` | JS 引擎 | 历史：外部引擎不可控；现状：退居小站点 + 教学样本 |
| `minicurses.py` | curses | 跨平台（含 Windows）的进度条渲染 |
| `webvtt.py` | webvtt 库 | 字幕解析/打包要嵌进下载流水线（边下边打包） |

**统一的取舍逻辑：核心路径上的依赖要么没有、要么必须有纯 Python 回退。**

## 4. 外部服务

| 服务 | 用途 | 接口 |
|---|---|---|
| **SponsorBlock** | 赞助片段社区标注 | REST API（`--sponsorblock-mark/remove`） |
| YouTube innertube API | 元数据/流地址 | 内部 API 逆向（`youtube/_base.py`） |
| 各站点私有 API | 1700+ 提取器 | 逆向成果，`extractor/` 全部 |

## 5. 插件生态（用户侧扩展）

不用改源码的四种扩展点（`yt_dlp/plugins.py` 统一加载）：

| 后缀 | 扩展什么 | 示例 |
|---|---|---|
| `*IE` | 新站点提取器 | 社区站点包 |
| `*PP` | 后处理器 | `--use-postprocessor` 调用 |
| `*PTP`/`*PCP`/`*PCSP` | YouTube PO Token provider | bgutil-ytdlp-pot-provider |
| `*JCP` | jsc JS 挑战 provider | 自定义 JS 运行时接入 |

插件目录：配置目录下 `plugins/`、可执行文件旁/用户/系统 `yt-dlp-plugins/`
（支持 zip/egg/whl），详见 [03](03-extractor-system.md) §5。

## 6. 开发工具链（devscripts/，读代码可跳过）

- `make_lazy_extractors.py`：发布构建时生成提取器桩类（懒加载，见 [03](03-extractor-system.md) §3）；
- `make_readme.py` / `prepare_manpage.py`：文档从 `--help` 生成的单向流水线；
- `make_changelog.py`：从 git log 生成 Changelog。

## 7. 生态全景一图流

```
                    yt-dlp（纯标准库即可运行）
                           │
      ┌──────────┬─────────┼──────────┬─────────────┐
      ▼          ▼         ▼          ▼             ▼
   ffmpeg     aria2c    JS运行时   Python库       插件
   ffprobe    curl/wget deno/node  requests      (*IE/*PP/
   (合并/转码/  (多连接    bun/qjs   curl_cffi     *PTP...)
    探测)       下载)     (解挑战)   pycryptodomex
                                   mutagen ...
                           │
                           ▼
                    自研回退（socks.py / aes.py 纯Py /
                     jsinterp / minicurses / webvtt.py）
                           │
                           ▼
                    每个可选依赖缺失 = 对应功能降级，程序不死
```

**这就是 yt-dlp 依赖管理的核心哲学：**
> 对"能力增强型"依赖（多连接、伪装、原生加密）：有则更好，无则降级；
> 对"核心路径"（HTTP、AES、SOCKS）：要么标准库、要么自带纯 Python 实现——
> 保证 `pip install` 都不需要的极端环境下依然能跑。

## 8. 动手验证

```bash
# 查看当前环境的 handler 注册情况（curl_cffi 缺失 → 伪装不可用的活例子）
python3 lessons/code/04_network_stack.py

# 无 pycryptodomex 环境跑纯 Python AES
python3 lessons/code/05_jsinterp_and_aes.py
```

---

至此八篇正文完结。回到 [README](README.md) 检查学习路线是否走完，
或者直接去读你最喜欢的那个模块的源码——现在你应该有地图了。
