# 04 · 网络层：一套可插拔的请求栈

> 目录：`yt_dlp/networking/`。yt-dlp 把"发 HTTP 请求"抽象成
> **一个调度器（Director）+ 多个后端（Handler）**，后端按请求能力动态路由。
> 本篇讲架构与路由；重试/断点续传在 [05 篇](05-network-stability.md)。

## 1. 为什么不直接用 requests？

因为不同请求需要不同能力，而**没有任何单一后端全都擅长**：

| 能力 | urllib（标准库） | requests/urllib3 | curl_cffi |
|---|---|---|---|
| 零依赖可用 | ✅ 永远 | ❌ 需安装 | ❌ 需安装 |
| 连接池/keepalive | ❌（每请求新建 TCP） | ✅ | ✅ |
| SOCKS 代理 | 借助自研 socks.py | 需 PySocks（被 monkeypatch 掉了） | ✅ |
| HTTPS 代理 | ❌ | ✅ | ✅ |
| WebSocket | ❌ | ❌ | ❌（websockets 库另算） |
| **浏览器 TLS 指纹** | ❌ | ❌ | ✅ 独此一家 |

yt-dlp 的解法：全部装进统一的 `RequestHandler` 抽象，运行时按请求特征挑后端。
**依赖可选、可热插拔、一个后端崩了换下一个。**

## 2. 核心抽象（`networking/common.py`）

```
YoutubeDL.urlopen(Request)                统一入口（不做重试！只清洗+鉴权+直通）
  └─ RequestDirector.send(request)        common.py:94  调度器
       ├─ _get_handlers(): 按 preference 函数打分求和 → 降序排序
       ├─ 逐个 handler:
       │    validate(request) → 抛 UnsupportedRequest 则跳过试下一个
       │    send(request)     → RequestError 直接上抛（说明"支持但失败"，不换家）
       │                        其他异常 = handler 自身 bug → 记录并试下一个
       └─ 全部失败 → NoSupportingHandlers(汇总错误)
```

### 2.1 偏好打分：每请求动态路由的关键

`register_preference` 装饰器注册打分函数 `pref(handler, request) -> int`，求和排序：

| 内置偏好 | 分值 | 效果 |
|---|---|---|
| requests | **+100** | 有 requests 时默认优先于 urllib |
| curl_cffi | −100 | 平时不用它（开销大） |
| **impersonate** | **+1000** | 请求一旦要求浏览器伪装，curl_cffi **绝对优先** |

这就是"某站点强制要浏览器指纹"的路由开关：普通请求走 requests，
带 `impersonate` 扩展的请求瞬间切到 curl_cffi。

### 2.2 能力协商：用数据而不是接口

`RequestHandler` 子类声明式注册能力：

```python
_SUPPORTED_URL_SCHEMES = ('http', 'https', 'ftp', 'file', 'data')
_SUPPORTED_PROXY_SCHEMES = ('http', ...)         # socks 由 socks.py 桥接后加入
_SUPPORTED_FEATURES = (Features.ALL_PROXY, Features.NO_PROXY)
```

更妙的是**扩展机制**：`Request.extensions` 携带 `timeout`、`cookiejar`、
`impersonate`、`legacy_ssl` 等扩展。子类在 `_check_extensions` 中 `pop` 掉自己认识的；
**留在字典里的扩展 = 不支持 → `UnsupportedRequest`**——
不认识的扩展自动导致该 handler 出局，天然实现了前向兼容的能力协商。

### 2.3 重量级实例复用

`InstanceStoreMixin`（`_helper.py`）：按 (proxies, cookiejar, legacy_ssl) 等参数为键
缓存 opener/session 实例。urllib 后端复用的是 OpenerDirector
（含 ssl context/代理配置/cookiejar），requests 后端复用的是真正带连接池的 Session。

## 3. 三个后端的实现要点

### urllib 后端（`_urllib.py`，兜底者）

- 把 `urllib.error.HTTPError/URLError`、PySocks 异常、`OSError` 翻译成统一异常体系；
- 从 `'redirect error' in str(e)` 识别**重定向循环**；重定向时**剥离 Cookie 头防跨域泄漏**；
- SSL 上下文（`_helper.py` 的 `make_ssl_context`）：统一 cipher 列表 + TLSv1.2 下限——
  目的就是**让 Python 的 TLS 握手不要太显眼**（做不到浏览器级，但避免最差的默认指纹）；
- 自写 `create_connection`：`getaddrinfo` 返回的**多个地址逐个尝试连接**——
  单个 A 记录连不上自动试下一个。

### requests 后端（`_requests.py`，默认首选）

- 每请求栈显式加 `Connection: keep-alive`；按 (cookiejar, legacy_ssl) 维护 Session 池；
- **故意禁用 urllib3 内建重试**（`max_retries=Retry(False)`）——
  把重试决策权收归应用层，换取统一的退避/日志/断点语义（见 05 篇）；
- 读响应时把 `urllib3.ProtocolError` 里的 `http.client.IncompleteRead` 翻译成
  `IncompleteRead`（与其他后端语义对齐，供上层决定重试）。

### curl_cffi 后端（`_curlcffi.py`，浏览器伪装）

- 原理：curl-impersonate 补丁版 curl，在 **TLS ClientHello**（cipher 顺序/扩展/ALPN/曲线）
  和 **HTTP/2 设置帧**层面复刻真实浏览器指纹——Python `ssl` 模块做不到（顺序固定），
  这正是 Cloudflare 类反爬的检测点；
- 读超时特殊处理：curl 的 timeout 是连接超时，于是设
  `LOW_SPEED_LIMIT=1B/s + LOW_SPEED_TIME=timeout` ——**速度低于 1B/s 持续 N 秒即断**，
  等效 read timeout；
- 伪装时**删掉与 yt-dlp 默认值相同的请求头**（UA 等），让 curl_cffi 注入与 TLS 指纹
  一致的浏览器头——避免"TLS 是 Chrome、UA 是 yt-dlp"的自相矛盾。

### `--impersonate` 的传递链

```
options.py 定义 --impersonate chrome-110:windows-10
 → __init__.py 转成 ImpersonateTarget(client, version, os, os_version)  # dataclass，支持通配
 → YoutubeDL.__init__ 启动即校验可用性（不可用直接报错，而不是运行到一半才失败）
 → build_request_director() 作为构造参数注入 curl_cffi handler
 → 提取器也可按请求覆盖：_download_webpage(..., impersonate=...)
 → ImpersonateTarget.__contains__ 通配匹配：请求 chrome 被 handler 支持的 chrome-131 包含即通过
```

## 4. 异常体系 = 重试策略的声明（`networking/exceptions.py`）

**这是全网络层最重要的一张表。**三个后端都被强制把底层异常翻译到同一套体系，
而"哪种异常怎么办"就写在异常的层级关系里：

```
YoutubeDLError
└── RequestError                        网络层错误基类（带 msg/cause/handler 引用）
    ├── UnsupportedRequest              → 换 handler（fallback），不算失败
    ├── NoSupportingHandlers            → 所有后端都不行（YoutubeDL.urlopen 会附人性化提示：
    │                                      "HTTPS 代理需装 requests/curl_cffi" 等）
    ├── TransportError                  ≈ 瞬时故障 → 【可重试】
    │   ├── IncompleteRead              读到的字节数不足（下载续传的关键信号）
    │   ├── SSLError
    │   │   └── CertificateVerifyError  证书校验失败 → 【不可重试】（确定性错误）
    │   └── ProxyError
    └── HTTPError                       带响应的状态码错误
          5xx                            → 【可重试】
          4xx（416 特判，见 05 篇）       → 【不可重试】
          redirect_loop=True            → 重定向循环标记
```

上层判断"是不是网络错误"统一用 `network_exceptions = (HTTPError, TransportError)` 元组
（extractor 和 YoutubeDL 都 catch 它）。`YoutubeDL.urlopen()` 本身**不做重试**——
重试统一上移到 downloader/extractor 层（05 篇详解）。

## 5. 代理支持：连 SOCKS 都是自己实现的

`yt_dlp/socks.py` 是一份**手写的 SOCKS4/4a/5 协议实现**（公共领域代码改写），
不依赖 PySocks——为了少一个硬依赖：

- SOCKS5 含 RFC1929 用户名/密码认证；域名走 `ATYP_DOMAINNAME` 交给代理解析（remote DNS，
  即 `socks5h` 与 `socks5` 的区别）；
- 桥接方式：urllib 后端动态生成 `SocksConnection` 类（HTTP 先隧道化，HTTPS 再在隧道上
  `wrap_socket`）；requests 后端则 monkeypatch 掉 `requests.adapters.SOCKSProxyManager`；
- `select_proxy()`（utils）统一处理 no_proxy 白名单与 `scheme → all` 回退。

代理配置来源：`--proxy`（支持 http/https/socks4/socks5/socks5h）；
不传则继承系统/环境变量；`--source-address` 绑定本地出口地址（IPv4/IPv6 由
自写连接逻辑按地址族过滤）。

## 6. YouTube 之外的一个冷门细节：`expected_status`

HTTP 语义里 4xx/5xx 应当失败，但现实网站常在 403/404 页面里嵌数据。
yt-dlp 允许提取器请求 `_download_webpage(..., expected_status=(403,))`——
**状态码在白名单内时返回错误页响应体**，异常体系与业务逻辑就此解耦。

## 7. 设计模式小结

| 模式 | 位置 |
|---|---|
| 策略 + 责任链 | Director 按偏好逐个尝试 handler |
| 声明式能力协商 | `_SUPPORTED_*` 类属性 + extensions 消费即支持 |
| 适配器 | 三个后端把各自异常翻译成统一体系 |
| 单例/享元 | `InstanceStoreMixin` 按配置键缓存实例 |
| 中间人（透明增强） | gzip/deflate/brotli 自动解压在响应适配器里完成 |

## 8. 动手验证

```bash
python3 lessons/code/04_network_stack.py
```

脚本会打印当前环境的 handler 注册表与偏好打分，演示：
普通请求路由到 requests；`file://` 请求被拒（`--enable-file-urls` 才放行）的
`NoSupportingHandlers` 提示；连接拒绝如何被翻译成 `TransportError`（可重试）。

下一篇 → [05 网络稳定性](05-network-stability.md)：请求会失败，然后呢？
