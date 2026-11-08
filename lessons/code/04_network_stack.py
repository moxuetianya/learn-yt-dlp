#!/usr/bin/env python3
"""
实践 04：网络栈——handler 注册表、按请求路由、异常体系分类

验证文档：
  - lessons/04-networking.md §2 「RequestDirector / preference 打分 / 能力协商」
  - lessons/04-networking.md §4 「异常体系 = 重试策略的声明」
  - lessons/08-external-tools.md §2 「Python 可选依赖的优雅降级」

演示（按当前环境实际状态输出，本学习环境没有 curl_cffi，正好是活教材）：
  1. 当前环境注册了哪些 handler、偏好打分
  2. 一次真实的本地请求如何路由（含响应头读取）
  3. 连接拒绝 → TransportError（可重试族）
  4. 404 → HTTPError 4xx（不可重试族）
  5. file:// 协议默认被拒 → NoSupportingHandlers + 人性化提示
"""
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from yt_dlp import YoutubeDL
from yt_dlp.networking.exceptions import HTTPError, TransportError


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == '/missing':
            self.send_response(404)
            self.send_header('Content-Length', '0')
            self.end_headers()
        else:
            body = b'hello from local server'
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)


def main():
    server = ThreadingHTTPServer(('127.0.0.1', 0), H)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f'http://127.0.0.1:{port}'

    ydl = YoutubeDL(params={'quiet': True})

    # ── 1. handler 注册表 ──────────────────────────────────────────────
    print('== 1. 当前环境的网络后端注册表（依赖可选、缺失自动跳过）')
    director = ydl._request_director
    for key, rh in director.handlers.items():
        print(f'   {key:<10} {rh.__class__.__name__:<12} '
              f'URL schemes={getattr(rh, "_SUPPORTED_URL_SCHEMES", "?")}')
    print(f'   偏好函数: {len(director.preferences)} 个 '
          f'(requests: +100, curl_cffi: -100, impersonate: +1000)')
    import importlib.util
    if importlib.util.find_spec('requests') and 'Requests' not in director.handlers:
        print('   ↳ 本环境装了 requests 却没注册 RequestsRH：版本 < 2.32.2 被'
              ' networking/__init__.py 的导入检查拒绝 → 回退 urllib 后端（优雅降级）')
    if 'CurlCFFI' not in str(director.handlers):
        print('   ↳ 未安装 curl_cffi → --impersonate 不可用，普通请求不受影响')

    # ── 2. 一次真实请求 ────────────────────────────────────────────────
    print('\n== 2. ydl.urlopen 发起请求（Director 按偏好选择 handler）')
    resp = ydl.urlopen(f'{base}/hello')
    print(f'   状态: {resp.status}  Content-Type: {resp.headers.get("Content-Type")}')
    print(f'   响应体: {resp.read().decode()}')

    # ── 3. 异常体系：两种失败，两种命运 ────────────────────────────────
    print('\n== 3. 异常分类（= 重试策略声明）')
    try:
        ydl.urlopen('http://127.0.0.1:1/nope')   # 端口 1，必然连接拒绝
    except TransportError as e:
        print(f'   连接拒绝 → {type(e).__name__}（TransportError 族 → 【可重试】）')
        print(f'     {e}')
    try:
        ydl.urlopen(f'{base}/missing')
    except HTTPError as e:
        print(f'   404      → HTTPError(status={e.status})（4xx → 【不可重试】）')

    # ── 4. 能力协商：不认识的协议直接出局 ─────────────────────────────
    print('\n== 4. 能力协商：file:// 默认所有 handler 都拒绝')
    try:
        ydl.urlopen('file:///etc/hostname')
    except Exception as e:
        print(f'   {type(e).__name__}: {e}')
        print('   ↳ 提示里直接给出解决方案：--enable-file-urls')

    server.shutdown()


if __name__ == '__main__':
    main()
