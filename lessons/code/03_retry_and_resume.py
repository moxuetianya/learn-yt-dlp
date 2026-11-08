#!/usr/bin/env python3
"""
实践 03：网络不稳定处理——重试 + Range 断点续传（本地模拟断线）

验证文档：
  - lessons/05-network-stability.md §3 「HttpFD 断点续传」
      · 服务器中途断线 → IncompleteRead → RetryManager 重试
      · 重试请求带 Range: bytes=N- → .part 文件续传（不从头下）
      · Content-Range 校验 / .part 临时文件生命周期
  - lessons/04-networking.md §4 「异常体系 = 重试策略」

原理：起一个本地 HTTP 服务器，首次请求【故意写到一半断开连接】，
之后正常服务 Range 请求。用 yt-dlp API 下载，观察重试与续传。
纯离线运行。
"""
import hashlib
import random
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from yt_dlp import YoutubeDL

DATA = random.Random(42).randbytes(4_000_000)   # 4MB 确定性伪随机数据
MD5 = hashlib.md5(DATA).hexdigest()
WORKDIR = Path('/home/peter/.tmp/opencode/lesson03')
LOG = []


class FlakyHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, fmt, *args):
        LOG.append(fmt % args)

    def do_GET(self):
        rng = self.headers.get('Range')
        LOG.append(f'REQUEST Range={rng!r}')
        if rng is None:
            # 首次请求：答应给全文件，但写到一半粗暴断开（模拟断线/服务器故障）
            self.send_response(200)
            self.send_header('Content-Type', 'video/mp4')
            self.send_header('Content-Length', str(len(DATA)))
            self.send_header('Accept-Ranges', 'bytes')
            self.end_headers()
            self.wfile.write(DATA[:len(DATA) // 2])
            self.wfile.flush()
            self.connection.close()          # ← 半途断线！
            LOG.append(f'! 断线：只发了 {len(DATA)//2}/{len(DATA)} 字节就 close()')
            return
        # Range 请求：正常服务（支持续传的服务器）
        start = int(rng.split('=')[1].split('-')[0])
        self.send_response(206)
        self.send_header('Content-Range', f'bytes {start}-{len(DATA)-1}/{len(DATA)}')
        self.send_header('Content-Length', str(len(DATA) - start))
        self.end_headers()
        self.wfile.write(DATA[start:])
        LOG.append(f'  续传：从 {start} 字节处继续，发了 {len(DATA)-start} 字节')


def run_server():
    server = ThreadingHTTPServer(('127.0.0.1', 0), FlakyHandler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, port


def main():
    WORKDIR.mkdir(parents=True, exist_ok=True)
    server, port = run_server()
    url = f'http://127.0.0.1:{port}/video.mp4'

    print(f'== 服务器就绪（127.0.0.1:{port}），文件 {len(DATA)} 字节，md5={MD5[:16]}...')
    print('== 首次请求会【中途断线】，之后支持 Range 续传\n')

    ydl = YoutubeDL(params={
        'quiet': False,
        'no_warnings': True,
        'retries': 3,                # --retries（downloader/http.py 的 RetryManager）
        'continuedl': True,          # 断点续传（默认开启）
        'outtmpl': str(WORKDIR / 'video.%(ext)s'),
        'noprogress': True,
        'logger': None,              # 用默认 stderr 输出，能看到重试警告
    })
    print('== 开始下载（yt_dlp API：ydl.download([url])）\n')
    ret = ydl.download([url])

    print('\n== 服务器视角的请求序列（断点续传实锤）')
    for line in LOG:
        print('  ', line)

    out = WORKDIR / 'video.mp4'
    ok = hashlib.md5(out.read_bytes()).hexdigest() == MD5
    print('\n== 校验')
    print(f'   退出码: {ret}  文件: {out}  大小: {out.stat().st_size}')
    print(f'   md5 一致: {ok}')
    print('   .part 残留:', list(WORKDIR.glob('*.part')) or '无（成功后已被改名）')

    if ok:
        print('\n结论：断线 → IncompleteRead → RetryManager 重试 → Range: bytes=N- 续传 → 文件完整。')
        print('（对应 lessons/05-network-stability.md §3.1 / §7 决策路径图）')
    server.shutdown()


if __name__ == '__main__':
    main()
