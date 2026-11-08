#!/usr/bin/env python3
"""
实践 05：内置 JS 解释器 + 纯 Python AES（无 pycryptodomex 的回退）

验证文档：
  - lessons/06-authentication.md §5 「jsinterp：够用的解释器长什么样」
  - lessons/06-authentication.md §3.2 「Chromium cookie 解密依赖 aes.py，
    无 pycryptodomex 时走纯 Python 回退」
  - lessons/08-external-tools.md §3 「自研替代品：为什么这些轮子要自己造」

纯离线运行。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from yt_dlp import aes
from yt_dlp.jsinterp import JSInterpreter


def demo_jsinterp():
    print('== 1. JSInterpreter：从 JS 源码中定位函数并执行')
    js_code = r'''
    function deobfuscate(id) {
        var parts = id.split('.');
        var out = [];
        for (var i = parts.length - 1; i >= 0; i--) {
            out.push(parts[i]);
        }
        return out.join('-');
    }
    '''
    interp = JSInterpreter(js_code)
    func = interp.extract_function('deobfuscate')
    result = func(['com.example.video.12345'])
    print(f'   deobfuscate("com.example.video.12345") → {result!r}')
    assert result == '12345-video-example-com'


def demo_aes():
    print('\n== 2. aes.py：检测到/未检测到 pycryptodomex 时的双轨实现')
    import importlib.util
    has_native = importlib.util.find_spec('Cryptodome') is not None
    print(f'   pycryptodomex 安装: {has_native} → '
          + ('原生实现' if has_native else '纯 Python 实现（慢但正确，本环境的活教材）'))

    key = b'This is a key123'          # 16B AES-128
    iv = b'This is an IV456'
    data = b'hello yt-dlp aes fallback!' * 4

    # 加密恒为纯 Python 实现；解密在无 Cryptodome 时也走纯 Python
    ciphertext = aes.aes_cbc_encrypt_bytes(data, key, iv)
    plaintext = aes.aes_cbc_decrypt_bytes(ciphertext, key, iv)
    plaintext = aes.unpad_pkcs7(plaintext)
    assert plaintext == data
    print(f'   AES-CBC 加解密回环: OK ({len(data)}B 明文 → {len(ciphertext)}B 密文 → 还原一致)')

    # GCM（Windows Chromium cookie 解密用的模式：12B nonce + 16B tag）
    nonce = b'0123456789ab'
    ct = data[:24]                                       # 拿一段明文当 GCM 输入演示 tag 校验
    try:
        aes.aes_gcm_decrypt_and_verify_bytes(ct, key, b'\x00' * 16, nonce)
        print('   GCM 错误 tag 校验: 未抛异常（异常）')
    except ValueError:
        print('   GCM 错误 tag 校验: 正确抛出 ValueError（Mismatching authentication tag）')

    print('\n== 3. 这两个模块存在的意义')
    print('   · jsinterp：无外部 JS 运行时时也能解部分站点的 JS 挑战（youtube 已改用 jsc/外部运行时）')
    print('   · aes.py：浏览器 cookie 解密（Windows GCM / macOS、Linux CBC）与 HLS AES-128')
    print('     都是核心路径 → 不能因用户没装加密库而全灭 → 必须有纯 Python 回退')


if __name__ == '__main__':
    demo_jsinterp()
    demo_aes()
