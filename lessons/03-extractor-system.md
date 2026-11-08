# 03 · 提取器体系：1700+ 个站点适配器如何共存

> 提取器（InfoExtractor，简称 IE）是 yt-dlp 数量最大、变更最频繁的部分。
> 本篇讲清楚：基类给了什么"福利"、注册表如何组织、懒加载如何提速、
> 任意 URL 如何兜底、插件如何注入，最后放大看 YouTube 这个最复杂的案例。

## 1. 模板方法：`extract()` 与 `_real_extract()`

`InfoExtractor`（`extractor/common.py:105`）的核心是**模板方法模式**。
公共骨架 `extract(url)`（common.py:756）固定执行：

```
extract(url)
 ├─ initialize()               懒初始化（只跑一次）
 │   ├─ _initialize_pre_login()
 │   ├─ _perform_login()       ← 子类按需覆写（登录逻辑）
 │   └─ _real_initialize()
 ├─ _real_extract(url)         ← 唯一必须实现的"变化点"（基类直接 NotImplementedError）
 ├─ GeoRestrictedError? → 伪造 X-Forwarded-For 重试一次（geo 绕过）
 └─ 异常统一封装（KeyError/StopIteration → "extractor error"，防实现 bug 裸抛）
```

于是**每个站点子类只需要关心"怎么从这个网站抠出 info_dict"**，
登录、重试、地理绕过、错误包装、日志前缀全部由模板层承包。

子类用类属性声明元信息（而不是实现接口方法）：

| 类属性 | 用途 |
|---|---|
| `_VALID_URL` | 匹配正则，**必须含命名组 `(?P<id>...)`**（`_match_id` 提取视频 id） |
| `_NETRC_MACHINE` | 声明支持 netrc 登录（见 [06 认证](06-authentication.md)） |
| `_GEO_COUNTRIES` / `_GEO_IP_BLOCKS` | 地理限制绕过配置 |
| `_EMBED_REGEX` | 在第三方网页中认领"嵌入的自己"（供 Generic 兜底用） |
| `_WORKING = False` | 标记提取器当前已损坏 |
| `IE_DESC` / `SEARCH_KEY` | 描述 / 搜索前缀（如 `ytsearch:`） |

## 2. 基类给的"福利"：网络与解析工具箱

### 2.1 分层请求管道

```
_create_request()        str → Request 对象（headers/data/impersonate 都挂在请求上）
   ↓
_request_webpage()       ★ 节流（--sleep-requests）· X-Forwarded-For 假 IP ·
                         浏览器伪装协商 · expected_status 白名单 · 异常分级
   ↓
_download_webpage_handle()   → (内容str, 响应句柄)；编码猜测见下
   ↓
__create_download_methods()  ★ 元工厂：批量生成 _download_json/_download_xml/...
                             及各自 _handle 变体（common.py:1092）
   ↓
_download_webpage()      最常用入口（带简单重试）
```

两个值得抄的设计：

- **工厂方法 + 晚绑定**：`_download_json = 网页下载 + _parse_json`。parser 按名字
  `getattr` 查找——子类覆写 `_parse_json`（比如站点 JSON 有奇怪前缀），所有生成方法自动生效；
- **编码猜测链**（`_guess_encoding_from_content`）：Content-Type 的 charset →
  页头 1024 字节内 `<meta charset>` → BOM 判 UTF-16 → 默认 UTF-8；
  解码统一 `decode(encoding, 'replace')` 容错。

`expected_status` 机制很实用：显式接受 403/404 并**返回错误页的响应体**——
很多站的"错误页面"里其实带着数据。

### 2.2 解析工具（网页 → 结构化数据）

| 工具 | 作用 |
|---|---|
| `_search_regex(pattern, str, name)` | 事实标准。pattern 可以是列表（逐个试）；`group=None` 时返回**第一个非 None 捕获组**；失败语义三档：默认值 / 致命抛 `RegexNotFoundError` / 警告返回 None |
| `_search_json(start, str, name)` | 从 `start_pattern` 后抠出**平衡花括号**的 JSON 并解析——提取 `ytInitialData`、`__NEXT_DATA__` 这类嵌入 JSON 的关键工具，还容错截断 JSON |
| `_html_search_regex` | 上者 + `clean_html`（去标签/反转义实体） |
| `_og_search_property` / `_html_search_meta` | OpenGraph / `<meta>` 元数据 |
| `_search_json_ld` | schema.org JSON-LD → info_dict |
| `_search_nextjs_data` / `_search_nuxt_data` | React Next.js / Nuxt 框架 SSR 数据 |
| `_hidden_inputs` | 提取隐藏表单（登录 POST 用） |
| `traverse_obj`（utils） | 声明式 JSON 路径提取，容错缺字段 |

### 2.3 结果构造器与"走私"

```python
self.url_result(url, ie_key='Youtube', video_id=...)     # {'_type': 'url', ...} 交给下家
self.playlist_result(entries, playlist_id, playlist_title)
playlist_from_matches(matches, ...)                       # 正则命中 → 去重 url_result 生成器
```

`smuggle_url(url, data)` / `unsmuggle_url(url)`：把 dict 编码进 URL 片段
（`#ytdl+...`），跨提取器传递上下文（referer、强制单流等）。
dict 出不了本提取器，URL 本身当信封。

### 2.4 错误体系（`utils/_utils.py`）

```
YoutubeDLError
└── ExtractorError            预期标志 expected：区分"站点限制"还是"yt-dlp 的 bug"
    ├── UnsupportedError      所有提取器都处理不了（几乎只由 GenericIE 抛）
    ├── RegexNotFoundError
    ├── GeoRestrictedError    携带 countries 白名单 → 模板层联动假 IP 重试
    └── UserNotLive           频主未开播
```

便捷抛出器：`raise_login_required()` / `raise_geo_restricted()` / `raise_no_formats()` /
`report_drm()`——统一决定"抛错还是按 --ignore-no-formats-error 降级为警告"。

## 3. 注册与查找：顺序即优先级

```
_extractors.py      2477 行纯静态导入清单（按站点字母序，devscripts 自动维护）
      ↓ import
extractors.py       运行时装配：dir() 反射所有 *IE 类 → 手工排序
                    （① YouTube 最前 ② 其余按序 ③ GenericIE 永远垫底）
      ↓ 写入
globals.extractors  contextvar 包装的 Indirect 字典（协程安全）
      ↓
YoutubeDL._ies      有序 dict；extract_info() 遍历它，第一个 suitable() 命中者胜出
```

`suitable(url)` 本质是 `_VALID_URL` 正则的 `re.match`（正则编译结果缓存在类 `__dict__`）。
**顺序敏感**：具体站点在前（如 `YoutubeIE`），宽松模式在后，
`GenericIE` 的 `_VALID_URL = r'.*'` 兜住一切。

### 懒加载：lazy_extractors

`devscripts/make_lazy_extractors.py`（构建期）为每个 IE 生成**桩类**：只含静态属性快照
（`_VALID_URL`、`_WORKING`、`IE_DESC`、`_NETRC_MACHINE` 等）+ 方法代理。
桩类访问未快照属性时（`LazyLoadMetaClass.__getattr__`）才 `importlib` 加载真实模块。

效果：URL 匹配只需要正则，**无需 import 上千个模块**——冷启动从数秒降到毫秒级。
配套纪律：`suitable()` 的 docstring 明确要求**函数内不得 import 其他提取器**。

## 4. GenericIE：最后的兜底（`extractor/generic.py`）

`_VALID_URL = r'.*'`，注册表永远排最后，只在所有专用提取器都不匹配时执行。
`_real_extract()` 是一条"少下载、早返回"的检测管道：

```
协议修补（//开头补scheme；无scheme按 --default-search 处理，甚至转 ytsearch:）
 → 请求探测（只读 512KB 判断类型；识别 Cloudflare 403 质询 → 提示 impersonate）
 → 重定向跟随（URL 变了 → url_result 递归）
 → 直接媒体文件？Content-Type / 魔数嗅探 → direct 标记返回
 → 文本清单？#EXTM3U→HLS；XML 根元素→ RSS/ISM/SMIL/XSPF/MPD(DASH)/F4M
 → HTML 网页：
     ★ _extract_embeds(): 遍历【所有】提取器调 extract_from_webpage()
       —— 任何声明了 _EMBED_REGEX 的提取器都能在此认领页面（如内嵌的 YouTube iframe）
       StopExtraction 异常表示"独占"
     再试常见播放器：JW Player / video.js / KVS / JSON-LD VideoObject / <video src>
 → 一无所获 → UnsupportedError（全链路最终失败信号）
```

注意分工：Generic 是**唯一允许"读响应体后再决策"**的提取器；
`_parse_m3u8_formats_and_subtitles`（master playlist → formats）等清单解析
也在 `extractor/common.py` 基类里，供所有提取器复用。

## 5. 插件机制（`yt_dlp/plugins.py`）

核心是**虚拟命名空间包 + import 钩子**：

- `PluginFinder`（`importlib.abc.MetaPathFinder`）被插到 `sys.meta_path[0]`，
  拦截 `yt_dlp_plugins.extractor.*` 的 import，在配置目录 `plugins/`、
  `yt-dlp-plugins/` 目录（支持 zip/egg/whl）里找模块；
- `load_plugins()` 枚举所有模块，用 inspect 找出**以 `IE` 结尾、非下划线开头**的类，
  `merge_dicts` **前置**合并进主注册表——插件提取器比官方的更早参与匹配；
- 同一套底座还支撑 `PP` 后缀的 postprocessor 插件；
- **覆写型插件**：子类化官方 IE 并 `class YoutubeIE(MyPlugin, plugin_name='xxx')`
  （`__init_subclass__` 钩子配合），自动改名 `youtube+xxx`、原类存入 `__wrapped__`。

## 6. 案例放大：YouTube 提取器（`extractor/youtube/` 包）

本版本 YouTube 已从单文件拆成包：`_base.py`（公共基类+客户端配置）、`_video.py`
（单视频，YoutubeIE）、`_tab.py`（频道/播放列表翻页）、`_search.py`、
以及两个微插件框架 `jsc/`（JS 挑战求解）和 `pot/`（PO Token）。

### 6.1 提取一条 YouTube 视频的步骤

```
① URL 解析：巨型 _VALID_URL（watch/shorts/embed/youtu.be/裸ID/invidious 镜像...）→ 11 位视频 ID
② 下载 watch 页面：
     ytcfg.set({...})           页面配置（API key 等）
     ytInitialData              页面渲染状态 JSON（标题/章节/点赞/评论翻页 token，与播放无关）
     ytInitialPlayerResponse    页面内嵌的首个播放器响应（元数据兜底用）
③ 多客户端 player response（核心）：
     INNERTUBE_CLIENTS 注册表定义几十个"伪装客户端"
     （web/web_safari/web_embedded/web_music/android/ios/tv_samsung...）
     按状态选客户端序列：付费订阅→premium组；已登录→authed组；无JS运行时→jsless组
     对每个客户端 POST https://www.youtube.com/youtubei/v1/player
     （带 signatureTimestamp/sts、visitorData、PO Token）拿到该视角的 streamingData
④ playabilityStatus 处理：LOGIN_REQUIRED / AGE_GATE → 换客户端重试
⑤ 格式提取：遍历各响应的 formats(合并流)+adaptiveFormats(分离流)+DASH/HLS manifest
     ★ 挑战解密（见下）
⑥ 元数据汇合：videoDetails 为主，microformat / ytInitialData / HTML meta 逐级兜底
   （traverse_obj 声明式取值）
⑦ 字幕/评论（评论抓取推迟到元数据写盘前）/ mark_watched / 返回 info_dict
```

### 6.2 三道反爬挑战

YouTube 的流媒体 URL 不是"拿到就能用"的：

| 挑战 | 形式 | 后果 | yt-dlp 的应对 |
|---|---|---|---|
| **签名挑战 (s)** | URL 被拆成 `s=<密文>&sp=<参数名>&url=<真地址>`（signatureCipher） | 不解出则 URL 无效 | 从 player JS 中定位解密函数，执行后把结果拼回 `&signature=` |
| **n 挑战 (nsig)** | URL 查询串里的 `n=<密文>` | 解不出正确的 n，服务器返回 403/节流——**当前最主要的反爬手段** | 同上，且结果缓存（内存），player 版本绑定 |
| **PO Token** | player 请求与流媒体 URL 绑定的令牌 | 缺失时 web 客户端流 403/格式缺失 | `pot/` provider 插件框架；`--extractor-args youtube:po_token=...` 手动注入 |

挑战求解委托给 **jsc 框架**：把收集到的 n/s 挑战**批量**分发给 JS 运行时 provider
（内置支持 node/deno/bun 等外部运行时），在真实 JS 引擎中执行 player 代码解题；
结果按 `player_url + 挑战值` 缓存。旧的纯 Python `jsinterp.py` 退居二线
（仍服务 twitter 等小站点，见 [06 认证](06-authentication.md) §6）。

`sts`（signatureTimestamp）：从 player JS 抽取的 5 位时间戳，innertube 请求带上
可换取已签名的流——这解释了 YouTube 提取器为什么还要去下载并解析 player JS 文件。

### 6.3 翻页：continuation 模式

频道/播放列表条目藏在 `richGridRenderer` 等渲染器树里，翻页 token（`continuationCommand.token`）
就地收集，外层 while 循环用 token 调 `browse`/`next` API 追加——
`seen_continuations` 集合防环。这是"entries 为惰性生成器"的典型生产者。

## 7. 写一个最小提取器（伪代码示意）

```python
class ExampleIE(InfoExtractor):
    _VALID_URL = r'https?://example\.com/watch/(?P<id>\d+)'
    _TEST = {'url': '...', 'md5': '...', 'info_dict': {...}}   # 测试夹具

    def _real_extract(self, url):
        video_id = self._match_id(url)
        webpage = self._download_webpage(url, video_id)             # 福利①
        title = self._search_regex(r'<title>(.*?)</title>', webpage, 'title')  # 福利②
        return {                                                    # 福利③：返回 dict 即可
            'id': video_id, 'title': title,
            'formats': self._parse_m3u8_formats_and_subtitles(...)
        }
```

新站点进官方仓库：加 `extractor/example.py` + 在 `_extractors.py` 清单登记一行 +
`test/test_download.py` 加测试用例。核心代码零改动。

## 8. 动手验证

```bash
# 注册表规模与优先顺序（gen_extractor_classes = 注册顺序；YouTube 家族靠前、Generic 垫底）
python3 -c "
from yt_dlp.extractor import gen_extractor_classes
ies = list(gen_extractor_classes())
print(len(ies), [ie.IE_NAME for ie in ies[:3]], ies[-1].IE_NAME)"

# 看 YouTube IE 声明的部分类属性（懒加载桩类即可读取，无需真正加载模块）
python3 -c "
from yt_dlp.extractor import get_info_extractor
ie = get_info_extractor('Youtube')   # 键 = 类名去掉 IE 后缀（首字母大写）
print(ie._NETRC_MACHINE, bool(ie._VALID_URL))"
```

下一篇 → [04 网络层](04-networking.md)：上面反复出现的 `_download_webpage`，
底下到底压着怎样一套网络栈？
