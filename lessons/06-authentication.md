# 06 · 认证体系：从 Cookie 文件到 PO Token

> yt-dlp 的"认证"其实是**三个不同层面的问题**，混在一起谈就会觉得乱：
> ① 凭据如何随请求**传输**（cookiejar / HTTP auth）；
> ② 凭据从**哪里来**（cookie 文件 / 浏览器数据库 / netrc / 账号密码 / OAuth）；
> ③ 站点**专有**的令牌协议（YouTube PO Token、JS 挑战、签名）。

## 1. 传输层：cookiejar 一以贯之

所有请求共用 `YoutubeDL.cookiejar`（一个 `YoutubeDLCookieJar`，Netscape 格式的
`MozillaCookieJar` 子类）。三个网络后端都接受 cookiejar 扩展
（`InstanceStoreMixin` 按 cookiejar 实例缓存 session），
外部下载器（aria2c/curl）则把 cookie **导出成临时文件**传给它们。

统一入口在 CLI 层（`__init__.py`）：

```
--cookies FILE            从 Netscape 文件加载；退出时写回（会话 cookie 也持久化）
--cookies-from-browser B  直接提取浏览器数据库（见 §3）★
--username/--password     交给提取器的 _perform_login
--netrc                   从 ~/.netrc 读取（站点机器名 = _NETRC_MACHINE）
```

`YoutubeDLCookieJar` 的兼容细节：`#HttpOnly_` 前缀读写（Netscape 格式没有
HttpOnly 字段的民间扩展）、`expires=0` 视为会话 cookie、宽松版 `Set-Cookie` 解析
（`LenientSimpleCookie`，容错现实网站的畸形头）。

## 2. 账号密码：模板方法式登录

提取器基类提供钩子：

```python
def _perform_login(self, username, password):   # 子类覆写：模拟登录表单/API
    ...
```

`extract()` 模板保证它**懒执行且只执行一次**（`initialize()` 内）。
`_get_login_info()` 统一取参数，优先级：显式参数 > netrc > None。
`raise_login_required()` 根据 `--ignore-no-formats-error` 决定抛错还是警告降级。

HTTP 基础认证走另一条小路：URL 里带 `user:pass@host` 时，
`YoutubeDL.urlopen` 会剥出来转成 `Authorization: Basic` 头。

## 3. 从浏览器提取 Cookie（`yt_dlp/cookies.py`）——最硬核的部分

`--cookies-from-browser chrome+Profile_1:KEYRING` 支持
Chromium 系（chrome/chromium/edge/brave/opera/vivaldi/whale）+ firefox + safari。

### 3.1 通用技巧：复制数据库再打开

浏览器的 SQLite cookie 库被运行中的进程锁着。yt-dlp 的做法是
**把数据库文件复制到临时目录再打开**；多 profile 时取 mtime 最新的
（`_newest`）。这解释了为什么浏览器开着也能提取。

### 3.2 Chromium cookie 的加密与解密（难点）

Chromium 把 cookie 的 `encrypted_value` 列加密，**密钥来源和解密方式按操作系统各不相同**：

| 平台 | 加密 | 密钥来源 |
|---|---|---|
| **Windows** | AES-**GCM**（12B nonce + 16B tag） | `Local State` JSON 里 `os_crypt.encrypted_key`：base64 解码、剥 `DPAPI` 前缀、**DPAPI `CryptUnprotectData`**（ctypes 调 crypt32.dll）解出主密钥 |
| **macOS** | AES-**CBC**，PBKDF2-SHA1（salt=`saltysalt`，**1003 轮**） | **Keychain**：`security find-generic-password -w -a chrome -s 'Chrome Safe Storage'` |
| **Linux v10** | AES-CBC，PBKDF2 仅 **1 轮** | **固定口令 `peanuts`**（Chromium 源码里的硬编码，无 keyring 时的兜底） |
| **Linux v11** | 同上 | OS keyring：KWallet（kwallet-query / dbus）/ GNOME Keyring（secretstorage 库）；桌面环境自动检测逻辑复刻 Chromium 的实现 |

几个工程细节：

- v11 的 keyring 密码可能被用户改过 → **多个候选 key 依次尝试 + 空口令兜底**；
- 新版 Chromium（meta 表 version≥24）明文 cookie 带 32 字节 hash 前缀，需裁掉；
- 解密失败的 cookie 跳过并计数，不让一个坏 cookie 炸掉全部；
- **AES 解密用的是 `yt_dlp/aes.py`**：装了 pycryptodomex 走原生实现，
  没装走**纯 Python 实现**（CBC/GCM 都有）——用户环境常常没有加密库，所以必须有回退。

Firefox（明文 SQLite）与 Safari（二进制 `Cookies.binarycookies` 格式，
手写解析器）相对简单，此处从略。

## 4. YouTube 的三层专有协议

普通"带上 Cookie"对 YouTube 的高端格式远远不够，这是反爬与对抗的主战场：

### 4.1 PO Token（Proof of Origin Token）

绑定访问上下文的防机器人令牌，缺失时 web 客户端的流媒体 URL 返回 403。
yt-dlp 的应对是一个**微插件框架**（`extractor/youtube/pot/`）：

```
PoTokenRequest（含完整网络参数快照：cookiejar/proxy/headers/visitor_data）
  → PoTokenDirector.get_po_token()      按偏好排序逐个尝试 provider
     ├─ PoTokenCacheProvider/SpecProvider   缓存约定（PCP/PCSP 后缀插件）
     └─ 外部 solver 插件（社区有 bgutil-ytdlp-pot-provider 等）
```

- 上下文分 `GVS`（流媒体 URL）/ `PLAYER`（player 请求）/ `SUBS`（字幕）；
- 手动注入：`--extractor-args "youtube:po_token=web.gvs+<base64url>"`；
- token 通过 `serviceIntegrityDimensions.poToken` 字段进 player 请求体。

### 4.2 JS 挑战（nsig / 签名）与 jsc 框架

流媒体 URL 里的 `n=<密文>`（nsig 挑战）需要执行 YouTube 的 player JS 中的
混淆函数才能得到正确值，否则服务器 403/节流。yt-dlp 的方案：
**把挑战批量交给真实 JS 运行时**（node/deno/bun，由 jsc provider 框架按优先级选择），
在沙箱里执行 player 代码解题，结果缓存。详见 [03 提取器](03-extractor-system.md) §6.2。

### 4.3 缓存（`yt_dlp/cache.py`）

91 行的小模块，`~/.cache/yt-dlp/<section>/<key>.json`，包裹版本号字段
（跨版本失效自动丢弃）。存放：`sts`（签名时间戳）、`sigfuncs`（签名函数规格）、
nsig 结果（默认仅内存，量太大且与 player 版本绑定）。
`--no-cache-dir` / `--rm-cache-dir` 控制。**缓存是"对抗"的弹药库**——
player JS 没变就不必重新解题。

## 5. 内置 JS 解释器（`yt_dlp/jsinterp.py`）

为"没有外部 JS 运行时的环境"准备的**纯 Python JS 子集解释器**（~1000 行），
树遍历执行、无 AST 中间层：

| 组件 | 设计 |
|---|---|
| 词法 | `_separate()`：括号/引号/正则字面量感知的分割器 |
| 作用域 | `LocalNameSpace` 继承 `ChainMap`：最内层写入、沿链查找赋值（闭包语义） |
| 运算符 | 每个运算符映射到模拟 JS 语义的 Python 函数：位运算 int32 截断、`0/0=NaN`、`0**0=1`、字符串比较规则…… |
| 控制流 | `JS_Break/JS_Continue/JS_Throw` 用**异常**实现跳转；支持 if/for/switch/try-catch |
| 函数抽取 | `extract_function(code, name)`：按 `function name(...)` / `name = function(...)` 等形态正则定位，内嵌匿名函数提升为命名对象 |

**不支持** ES6+（class/箭头函数/async/模板字符串）——这正是 YouTube player 代码
日趋复杂后转向外部 JS 运行时的根因。如今 jsinterp 仍服务 twitter（radix 转字符串）
等少数站点。教学价值极高：一份"够用的解释器"该长什么样。

## 6. 认证全景图

```
                        ┌──────────── 凭据来源 ────────────┐
 --cookies FILE          │ Netscape cookiejar 文件          │
 --cookies-from-browser  │ SQLite+解密（DPAPI/Keychain/     │──┐
 --username/--password   │ keyring/peanuts）                │  │
 --netrc                 │ 表单登录 _perform_login           │  │
 OAuth 设备码（插件）     │ TV/设备码流程                    │  │
                         └──────────────────────────────────┘  │
                                                               ▼
                   YoutubeDL.cookiejar / _perform_login
                                                               │
                        ┌──────────── 传输 ────────────────┐   │
                        │ Cookie 头随每个请求（三个后端共用）│◄─┘
                        │ HTTP Basic（URL 内嵌账密）        │
                        └──────────────────────────────────┘
                                                               │
                        ┌──────── 站点专有协议 ─────────────┐
                        │ YouTube PO Token（pot 框架）      │
                        │ nsig/sig 挑战（jsc 框架 + 缓存）  │
                        └──────────────────────────────────┘
```

## 7. 动手验证

```bash
python3 lessons/code/05_jsinterp_and_aes.py
```

脚本演示：jsinterp 执行一段含数组操作的 JS（模拟"解混淆"）；
`aes.py` 在**没有 pycryptodomex 的环境**（本学习环境正好如此）下走纯 Python 路径
完成 CBC 加解密回环 + GCM 认证解密——印证 §3.2 的回退设计。

下一篇 → [07 音视频处理](07-audio-video.md)：认证过了、流地址拿到了，字节怎么变成能播放的文件？
