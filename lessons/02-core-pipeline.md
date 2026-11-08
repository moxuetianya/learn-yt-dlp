# 02 · 核心执行流程：从命令行到文件落盘

> 这是全套文档最重要的一篇。读懂 `YoutubeDL.py` 的调度逻辑，其余模块都是它的展开。
> 阅读前建议先看 [01 概览](01-overview.md) 的目录地图。

## 1. 启动链：`python -m yt_dlp URL` 之后发生什么

```
yt_dlp/__main__.py          仅做 import yt_dlp + sys.path 修正
  └─ yt_dlp.main()                 顶层异常防火墙（__init__.py）
      └─ _real_main(argv)          真正的编排逻辑
          ├─ parse_options(argv)   参数解析 + 校验 + 派生（见 §2）
          ├─ 加载插件 / 处理 --list-extractors 等即退命令
          ├─ with YoutubeDL(ydl_opts) as ydl:   ★ 引擎构造（上下文管理器负责收尾）
          │     └─ ydl.download(all_urls)       ★ 主入口
          └─ 退出码转换
```

关键点：**CLI 层与引擎层的唯一接口是 `ydl_opts` 这个字典**。
`parse_options()` 最后一步就是把扁平的 `opts` 逐字段拷贝成 API 风格的字典
（`skip_download` 等键名与 CLI 参数基本一致）。所以 yt-dlp 既是 CLI 也是 Python 库，
两条路最终汇合到 `YoutubeDL(params)`。

## 2. 参数解析的几个巧妙设计（`yt_dlp/options.py`）

### 2.1 四级配置文件分层

`parseOpts()`（options.py:43）中，`Config` 类（实现在 `utils/_utils.py`）按顺序加载：

```
System 系统配置 → User 用户配置 → Home → Portable（可执行文件旁）→ 命令行
（优先级从低到高；实现上只是把各层参数列表拼接，靠 optparse "后到覆盖先到"的语义）
```

配置文件用 `shlex.split(comments=True)` 解析，天然支持引号与 `#` 注释。
`--verbose` 回显完整配置时会自动抹掉密码字段（`hide_login_info`）。

### 2.2 运行时别名

`--alias mv:-f bv+ba/b` 会注册一个新选项 `mv`；触发时把
`-f bv+ba/b` 经 shlex 拆分后**塞回待解析参数流头部**，等于宏展开。
内置预设 `-t mp3`/`-t mp4` 走同一机制。有递归上限（`ALIAS_TRIGGER_LIMIT=100`）防炸弹。

### 2.3 CLI 开关 → 后处理器声明列表

`get_postprocessors(opts)`（`__init__.py`）把 `--extract-audio`、`--embed-subs` 等开关
翻译成一个**有序的 PP 声明列表**，且顺序有讲究（如 ModifyChapters 必须先于 FFmpegMetadata）。
这解释了为什么后处理顺序不完全由用户命令行顺序决定。

### 2.4 一个澄清

README 中的选项文档**不是**手写的：`options.py` 里的帮助文案 → `--help` 输出 →
`devscripts/make_readme.py` 打补丁写回 README → man 手册再从 README 抽取。单向流水线。

## 3. 引擎构造：`YoutubeDL.__init__`

构造函数（YoutubeDL.py:635 起）做对了三件事：

1. **预编译格式选择器**（约 814 行）：
   ```python
   self.format_selector = self.params['format'] if callable(...) else self.build_format_selector(...)
   ```
   `-f` 的语法错误在任何网络请求之前就会暴露——"编译期检查 DSL"的经典应用。
2. **注册默认提取器**（`add_default_info_extractors`）：按 `--use-extractors` 过滤后
   全部注册，末尾追加哨兵 `UnsupportedURLIE`（保证"无匹配"也能统一报错）。
3. **构建网络栈**：`build_request_director()` 把 urllib/requests/curl_cffi 的 handler
   实例化并注册偏好函数（详见 [04 网络层](04-networking.md)）。

## 4. 提取主流程：`extract_info` → `process_ie_result`

### 4.1 调用链总览

```
ydl.download(urls)                                    YoutubeDL.py:3699
 └─ extract_info(url)                                 :1673  遍历 self._ies 找 suitable 的 IE
     └─ __extract_info                                :1859  调 ie.extract() 并补公共字段
         └─ ie.extract(url)                           extractor/common.py:756（模板方法）
             └─ ie._real_extract(url)                 ← 1700+ 个站点子类实现的"变化点"
         └─ process_ie_result                         :1906  按 info['_type'] 分发 ★
             ├─ 'video'      → process_video_result   :2833（元数据定稿+格式选择）
             │                   └─ process_info      :3332（下载调度）
             │                        └─ dl()         :3284 → FileDownloader.download()
             ├─ 'url'        → extract_info(递归)      一层间接（playlist 条目通常长这样）
             ├─ 'url_transparent' → 递归 + 元数据合并   "外层页面信息更准"的场景
             ├─ 'playlist'   → __process_playlist     :2078（逐条递归回本函数）
             └─ 'compat_list' → 逐项 process_ie_result（旧格式兼容）
```

### 4.2 `_type` 分发 = 一个递归状态机

这是理解 yt-dlp 的钥匙。提取器返回的 dict 用 `_type` 声明"我是什么"：

| `_type` | 含义 | 引擎行为 |
|---|---|---|
| （缺省）`video` | 终点：单视频 + formats | 进入 `process_video_result` |
| `url` | 间接："去提取这个 URL" | 递归 `extract_info(url, ie_key=...)` |
| `url_transparent` | 间接，但外层元数据优先 | 递归提取后，把外层字段**覆盖**进内层结果 |
| `playlist` / `multi_video` | 列表 | 逐条目递归（见 §6） |

于是"频道 → 播放列表 → 视频 → 分段"任意深度的嵌套，统一成一个递归算法。
两个"刹车"防止失控：

- **`extract_flat`**（`--flat-playlist`）：`url`/`url_transparent` 不再递归展开，
  只返回薄壳条目（快速列清单）；
- **`_playlist_urls` 集合查重**：防止 playlist 自引用死循环。

### 4.3 `url_transparent` 与"smuggle"机制

`url_result(url, ie_key='Xxx', url_transparent=True)` 是提取器之间的"接力棒"。
配套的 `smuggle_url()/unsmuggle_url()` 在 URL 后附加 `#ytdl+<编码数据>`，
用于跨提取器传递私有上下文（如 referer、强制单流标记）——dict 走不出的进程，
就用 URL 本身当信封。

## 5. info_dict 的数据流演变（必须掌握）

| 阶段 | 发生了什么 |
|---|---|
| ① 提取器返回 | 最小形态 `{id, title, formats:[...]}`（或 playlist/indirect 结构） |
| ② 包装 | 补 `webpage_url / extractor / extractor_key`（只在缺失时补） |
| ③ playlist 展开 | 条目经 `ChainMap` 叠加 `playlist_index / playlist_autonumber / playlist_count` 等上下文字段（不拷贝 dict） |
| ④ 格式归一化 | 每个 format 补 `protocol / video_ext / audio_ext / vbr / tbr / filesize_approx / http_headers` 等派生字段（`FormatSorter._fill_sorting_fields`） |
| ⑤ 排序 | `formats.sort(key=FormatSorter(...).calculate_preference)` —— **列表尾部 = best** |
| ⑥ 选择 | `new_info = copy(info_dict); new_info.update(选中格式)` —— 选中的 format 字段**上浮顶层**（这就是输出模板里 `%(height)s` 能用的原因） |
| ⑦ 下载 | 新增 `_filename / filepath / __files_to_move / __real_download` |
| ⑧ 后处理 | PP 读写 `filepath`；`requested_downloads` 记录最终信息供 `--print after_move:...` |

## 6. 播放列表处理：`__process_playlist`

三个值得学的细节：

1. **`PlaylistEntries` 惰性访问层**（`utils/_utils.py:2418`）：把 extractor 给的
   list/`LazyList`/`PagedList`/生成器统一封装；`get_requested_items()` 解析
   `--playlist-items 1:10:2`（起始:结束:步长）语法，且**边迭代边静默过滤**，
   让 `--break-on-existing` 能在不耗尽生成器的情况下提前终止。
2. **`ChainMap` 组装上下文**：`ChainMap(entry, {**common_info, 'playlist_index': i})`
   ——条目不用拷贝就能"看见" playlist 字段，输出模板 `%(playlist_index)s` 由此而来。
3. **失败控制**：`--skip-playlist-after-errors N` 达到上限即中止；每条 entry 的异常
   由 `__process_iterable_entry` 装饰器统一捕获，单条失败不炸整个列表。

## 7. 格式选择：`-f` 字符串如何变成下载列表

三层结构：

```
-f "bestvideo*+bestaudio/best"（字符串）
  → build_format_selector()   YoutubeDL.py:2339   编译期：tokenize + 递归下降 → 选择器函数
  → _select_formats()         YoutubeDL.py:2307   运行期：喂入 ctx → formats_to_download
  → FormatSorter              utils/_utils.py:5360 支撑层：决定 "best" 的排序语义
```

### 7.1 DSL 语法（递归下降解析产出四种节点）

| 表达式 | AST 节点 | 语义 |
|---|---|---|
| `a/b` | PICKFIRST | 从左到右试，**第一个非空结果整体生效**（fallback） |
| `a+b` | MERGE | 左右各选一个，合成"虚拟合并格式" |
| `a,b` | 列表 | 两个都要（`bv,ba` 会下载两个文件） |
| `(a)` | GROUP | 分组 |
| `sel[filter]` | 过滤器 | `height>=720`、`ext=mp4`、`acodec^=mp4a`、`format_id~=\d+`、`?` 修饰表示 None 也通过 |

原子 `best/worst/b/bv/ba` 的星号变体规则：

| 写法 | 过滤条件 |
|---|---|
| `b` / `w` | vcodec≠none **且** acodec≠none（仅预合并流） |
| `b*` / `w*` | 任意（含纯视频/纯音频） |
| `bv` | 纯视频（vcodec≠none 且 acodec=none） |
| `bv*` | 含视频（含预合并流）——**默认值用的就是它** |

冷知识：tokenizer 直接借用 Python 标准库 `tokenize`，为绕过 Python 3.12 把
`137_1` 这类 format_id 误判为数字字面量，先给所有数字加随机前缀再剥掉。

### 7.2 `bv+ba` 合并的实现：虚拟格式 + 动态后处理器

`_merge()`（YoutubeDL.py:2451）把两条真实格式合成一个**dict**：

```python
{'requested_formats': [视频流, 音频流],   # 保留原始两条，下载/合并按它执行
 'format_id': '137+251',                  # 拼接
 'protocol': 'https+https',
 'tbr': 两流码率之和,
 'ext': 由 get_compatible_ext() 决定,     # 见下
 # 唯一视频流的 height/vcodec 与唯一音频流的 acodec/abr 上浮顶层
 }
```

输出容器由 `get_compatible_ext()`（`utils/_utils.py:3091`）决定：
先试 `--merge-output-format`，再按 codec 兼容表试 mp4→webm（h264+aac→mp4；
vp9+opus→webm），**全部失败兜底 mkv**——这就是"为什么默认下载常是 .webm 或 .mkv"。

下载期（`process_info`，行 3487 起）：若选中的下载器是 FFmpegFD 且能合并，
**单个 ffmpeg 进程边下边合**；否则两条流各自下载成
`title.f137.mp4` / `title.f251.webm`，然后**把 `FFmpegMergerPP` 动态追加进
`info_dict['__postprocessors']`**——合并被建模为"运行期才注册的后处理器"，
与静态 PP 共用同一条 post_process 流水线。

### 7.3 FormatSorter：best/worst 的排序语义

`best` 不是遍历比较，而是**先全序排序、后"反转取第 N 个"**。
排序 key 是一个 tuple（多级排序），字段次序声明在注册表里
（默认：`hidden, aud_or_vid, hasvid, ie_pref, lang, quality, res, fps, hdr:12,
vcodec, channels, acodec, size, br, asr, proto, ext, hasaud, source, id`）：

- 前 4 个是强制项（不可被 `--format-sort` 覆盖）：保证"有画面的恒优于纯音频"、坏格式沉底；
- 有趣的 `ordered` 型字段：如 `vcodec` 注册了从优到劣的正则序
  `av01 > vp9.2 > vp9 > h265 > h264 > ... > none`，字符串 codec 映射成位次分值；
- `--format-sort` 的用户字段插在强制项与常规默认之间：**用户偏好优先，但不破坏硬约束**。

### 7.4 默认格式规格

不传 `-f` 时用 `_default_format_spec()`（行 2315）推导：

- 输出到 stdout、或直播 → `best`（单流）；
- 否则 → **`bestvideo*+bestaudio/best`**；
- 若 ffmpeg 不可用 → 退回 `best` 并警告（没有合并器，分离流没有意义）。

## 8. 下载调度：`process_info`

单次下载的完整清单（行 3332 起，顺序即执行顺序）：

1. 再次 `_match_entry`（此时文件名已知，可按最终路径过滤）；
2. `prepare_filename()`：展开输出模板（`prepare_outtmpl` 支持字段嵌套、替代值、算术、
   日期格式、`%%` 转义等，是 yt-dlp 实际上的"模板语言"）→ `full_filename` 和 `.part` 临时名；
3. `simulate` 模式到此为止（`--skip-download`/`--dump-json` 就是这么实现的）；
4. 写附属文件：description / 字幕 / 缩略图 / **info.json** / internet shortcut，
   记入 `files_to_move`；
5. **下载分支**：
   - `skip_download` → 只跑 MoveFilesAfterDownloadPP；
   - 多格式（`requested_formats` 存在）→ FFmpegFD 单进程合并，或分别下载 + 动态 MergerPP（见上节）；
   - 单格式 → `self.dl(temp_filename, info_dict)`；
6. `fixup()`：按 extractor 留下的提示字段动态追加"修容器"PP（详见 [07](07-audio-video.md) §6）；
7. `post_process()` → 跑 `post_process` 时机全部 PP（Merger 在用户 PP 之前）→
   MoveFilesAfterDownloadPP → `after_move` PP；
8. 写 download archive；`--max-downloads` 触发 `MaxDownloadsReached` 一路向外传播终止一切。

`dl()`（行 3284）是下载器门面：`get_suitable_downloader()`（`downloader/__init__.py`）
按 `info['protocol']` 查 `PROTOCOL_MAP` 选类（http→HttpFD、m3u8_native→HlsFD、
http_dash_segments→DashSegmentsFD、rtmp→RtmpFD、m3u8→FFmpegFD……）。
**引擎自己不实现任何传输协议。**

## 9. 过滤机制：`_match_entry` 的四个调用时机

同一个过滤器贯穿提取生命周期，粒度越来越细：

| 时机 | 位置 | 可用的信息 |
|---|---|---|
| playlist 整体 | `__process_playlist` 开头 | playlist 级字段 |
| playlist 每条目 | 主循环 + `PlaylistEntries` 静默预过滤 | 条目薄壳（incomplete） |
| 格式选择后 | `process_video_result` | 完整 formats |
| 即将下载前 | `process_info` | 最终文件名 |

`--match-filter` 表达式（如 `duration<600 & like_count>100`!注意是 `&`）由 CLI 层
`match_filter_func()` 编译成函数；`--break-on-existing`/`--break-matches-filter`
通过 `ExistingVideoReached` / `RejectedVideoReached` 异常族实现"停止但不算错误"。

## 10. 动手验证

```bash
# ① 虚拟合并格式长什么样 / 各种 -f 表达式的行为
python3 lessons/code/02_format_selection.py

# ② 输出模板引擎 + info_dict 字段上浮
python3 lessons/code/01_info_dict_and_formats.py
```

读完本篇，你可以带着两个问题去读下一篇：
**"1700+ 个提取器是怎么被组织、被找到、被懒加载的？"**
**"一个新站点需要写多少代码就能被支持？"** → [03 提取器体系](03-extractor-system.md)
