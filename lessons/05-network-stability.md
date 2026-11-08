# 05 · 网络不稳定怎么办：重试、断点续传与分片并发

> 本篇回答一个实际问题：**wifi 断了、服务器 500 了、下载到 95% 掉线了，yt-dlp 怎么活下来？**
> 答案分四层：异常分类（04 篇）→ RetryManager 统一重试 → `.part` 断点续传 → `.ytdl` 分片记账。
> 所有结论可用 `lessons/code/03_retry_and_resume.py` 本地复现（模拟服务器半途断线）。

## 1. 总原则：重试集中在应用层

三个反直觉但重要的决策：

1. **网络栈不重试**：`YoutubeDL.urlopen()` 直通 Director，一次失败就抛；
2. **requests 后端故意禁用 urllib3 内建重试**（`max_retries=Retry(False)`）；
3. **重试全部上移**到 downloader / extractor 层，用同一个 `RetryManager`。

为什么？因为只有应用层知道**什么该重试、重试前要不要退避、失败了要不要换格式**。
库层盲目重试反而会破坏统一日志与断点语义。

## 2. RetryManager：迭代器式的重试引擎（`utils/_utils.py:5259`）

```python
for retry in RetryManager(retries, error_callback):   # retries 可为 float('inf')
    try:
        do_sthing_flaky()
    except network_exceptions as e:
        retry.error = e        # 标记本轮失败 → continue 进入下一轮
        continue
```

实现只有 ~50 行，但把"重试次数、错误报告、退避睡眠"全部收敛到一个迭代协议里：

- 每轮 `__iter__` 重置错误标记、`attempt += 1`，循环体结束后若有错误则回调；
- 回调 `report_retry` 决定：次数用尽 → "Giving up after N retries"（re-raise）；
  否则 warn + 按 `sleep_func(n)` 睡眠；
- `--retry-sleep [TYPE:]EXPR` 支持常数 / `linear=START:END:STEP` / `exp=START:END:BASE`，
  TYPE 四类独立配置（`http|fragment|file_access|extractor`）。

### 四层重试矩阵（谁在用 RetryManager）

| 层级 | CLI 参数 | 默认次数 | 位置 |
|---|---|---|---|
| 整文件 HTTP 下载 | `--retries` | 10 | `downloader/http.py` |
| 分片下载 | `--fragment-retries` | 10 | `downloader/fragment.py` |
| 文件访问（磁盘 EACCES 等） | `--file-access-retries` | 3 | `downloader/common.py`（`wrap_file_access` 装饰器） |
| 提取器 API 调用 | `--extractor-retries` | 3 | `extractor/common.py`（`self.RetryManager`） |

## 3. HttpFD：直链下载的断点续传（`downloader/http.py`）

### 3.1 `.part` 文件与 Range 协议

```
目标: video.mp4
临时: video.mp4.part          下载中写这里，成功后 rename 去掉 .part
```

流程（`real_download`）：

```
ctx.resume_len = os.path.getsize(.part)        # 上次下到多少
请求头 Range: bytes=<resume_len>-              # 从断点继续
响应 Content-Range 起点必须 == resume_len       # 服务器不认账？
    → report_unable_to_resume()，resume_len 清零，'wb' 重下
边读边写 .part（追加模式）
完成 → try_rename(.part, 正式名) → try_utime（用 Last-Modified 回写文件时间）
```

### 3.2 三处教科书级的特判

1. **HTTP 416（Range Not Satisfiable）**：不带 Range 重发请求，若远端
   `Content-Length` 与本地 `.part` 大小**相差 <100 字节**，直接认定已完成——
   YouTube 会微调文件尾部几个字节，死磕 Range 会永远差那么一点；
2. **读中途断线**：记录已写字节，从磁盘重新取 `.part` 实际大小作为新 `resume_len`
   再重试——**磁盘状态就是唯一事实来源**（内存计数器不可信）；
3. **`--http-chunk-size`**：把大下载切成多个 Range 请求（对抗按连接限速的服务器），
   且块大小随机化为 95%~100%——防止服务器按固定 chunk 边界缓存。

### 3.3 可重试 / 不可重试的判定表

| 情况 | 动作 |
|---|---|
| HTTP 5xx / TransportError / IncompleteRead（Content-Too-Short） | `raise RetryDownload` → 重试（带断点） |
| HTTP 416 | 特判（见上） |
| HTTP 其他 4xx / CertificateVerifyError | **不重试**，直接失败 |
| 速度低于 `--throttled-ratelimit` 持续 **3 秒** | `raise ThrottledDownload`（防瞬时波动误杀）→ 上层可换格式 |
| `NextFragment`（一个 chunk 下完） | 回循环头，**不消耗重试次数** |

## 4. FragmentFD：分片下载框架（`downloader/fragment.py`）

HLS/DASH/F4M/ISM/外部下载器共同基类。核心三件套：

### 4.1 `.ytdl` 状态文件 = 断点记账本

```json
{"downloader": {"current_fragment": {"index": 42}, "fragment_count": 1000,
                "extra_state": {...}}}
```

- 每追加一片就重写一次；进程被杀后重启，`fragment_index` 之前的片直接跳过
  （这就是**分片级断点续传**）；
- 一致性自检：`.ytdl` 损坏（JSON 解析失败）或 `fragment_index>0` 但 `.part` 为空 →
  全部清零从头来——宁可重下也不产出错文件；
- 单片文件名 `video.mp4.part-Frag12`，本身也走 HttpFD 的 `.part` 续传逻辑；
  追加进主文件后删除（`--keep-fragments` 保留）。

### 4.2 并发下载、顺序写入

`--concurrent-fragments N` 用 `ThreadPoolExecutor` 并发下载分片，
但**用 `pool.map` 按清单顺序取回结果再追加**——下载乱序、写入有序，
最终文件的字节序永远正确。多格式（视频+音频两条分片流）时各起一个线程池，
`interrupt_trigger` 实现"一处 Ctrl-C、全体收敛"。

### 4.3 分片级重试

每片套独立的 `RetryManager(fragment_retries)`；**可重试异常 = HTTPError + IncompleteRead**。
首片永远 fatal（没有首片连容器头都拼不出来）；后续片按 `--skip-unavailable-fragments`
决定跳过还是终止。HLS 的 AES-128 解密 key 也经 `ydl.urlopen` 拉取并按 URL 缓存。

## 5. 分片进度估算的小聪明

分片下载前不知道总大小（清单只给片数）。`_start_frag_download` 用历史均值外推：

```
总大小估算 = (已完成字节 + 当前片字节) / 已完成片数 × 总片数
```

进度条显示的 `~` 后缀（`total_bytes_estimate`）就是这么来的；
速度/ETA 用 3 秒采样窗 + 指数平滑（`utils/progress.py`），避免数字乱跳。

## 6. 其他稳定性手段速览

| 手段 | 说明 |
|---|---|
| `--limit-rate` | `slow_down`：速度超标时 sleep 到目标值（精确压平均速度） |
| `--sleep-requests/--sleep-interval` | 请求级/下载级节流（防封） |
| `--retry-sleep exp=1:16:2` | 指数退避 1s→2s→…→16s |
| 外部下载器接管 | aria2c `-x16 -s16` 多连接；ffmpeg 处理直播流（RTMP/HLS live 由 FFmpegFD 承担） |
| 直播流的中断语义 | FFmpegFD 收到 Ctrl-C 时向 stdin 发 `q` 让 ffmpeg 正常收尾——**半场直播也是能播放的文件** |
| `--no-part` | 不要 .part（临时文件直接写最终名，风险自担） |

## 7. 一次"断线重试"的完整决策路径（总复习）

```
HttpFD 主循环: for retry in RetryManager(--retries=10)
  establish_connection()
    ├─ 5xx/TransportError → RetryDownload ─┐
    ├─ 416 → ±100B 特判（可能直接 SucceedDownload）│
    └─ 其他 4xx → 直接失败                    │
  download() 读 body                          │
    ├─ 中途 TransportError → 记录已写字节 ───┤
    └─ 读完但 < Content-Length → ContentTooShortError
                                              ▼
  retry.error = e → report_retry（warn + retry-sleep 睡眠）→ 下一轮
  下一轮: resume_len = 磁盘上 .part 的真实大小 → Range: bytes=resume_len- → 续传
```

## 8. 动手验证（重点推荐）

`lessons/code/03_retry_and_resume.py` 会：

1. 起一个本地 HTTP 服务器，**首次请求故意发一半就断开连接**（模拟断线）；
2. 用 yt-dlp API 下载（`retries=3`），观察第一次失败后服务器收到
   `Range: bytes=N-` 请求（**断点续传实锤**）；
3. 打印服务器日志与最终文件 md5，证明分片拼回后内容正确；
4. 失败中途还能看到磁盘上的 `.part` 文件。

```bash
python3 lessons/code/03_retry_and_resume.py
```

**本环境实测输出（节选）**——服务器日志就是断点续传的直接证据：

```
== 服务器视角的请求序列（断点续传实锤）
   REQUEST Range=None
   ! 断线：只发了 2000000/4000000 字节就 close()
   REQUEST Range='bytes=2000000-'        ← 重试自动带 Range，从断点继续
   "GET /video.mp4 HTTP/1.1" 206 -
     续传：从 2000000 字节处继续，发了 2000000 字节

== 校验
   md5 一致: True                          ← 分段拼回后内容完整
   .part 残留: 无（成功后已被改名）          ← .part 生命周期符合预期
```

下一篇 → [06 认证体系](06-authentication.md)：网络会重试了，但"请先登录"怎么办？
