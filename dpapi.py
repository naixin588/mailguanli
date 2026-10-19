"""Windows DPAPI 加密工具 —— 用当前 Windows 用户凭据加密敏感数据。

无需管理密钥：加密后的数据只能由同一台机器、同一个 Windows 用户解密。
换用户 / 换机器 / 数据损坏都会解密失败（调用方需自行容错）。
"""

from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes


class DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


def _blob_to_bytes(blob: DATA_BLOB) -> bytes:
    return ctypes.string_at(blob.pbData, blob.cbData)


def _bytes_to_blob(data: bytes) -> DATA_BLOB:
    buffer = ctypes.create_string_buffer(data, len(data))
    return DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))


def protect(data: bytes) -> bytes:
    blob_in = _bytes_to_blob(data)
    blob_out = DATA_BLOB()
    ok = ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
    )
    if not ok:
        raise OSError("CryptProtectData 失败")
    result = _blob_to_bytes(blob_out)
    ctypes.windll.kernel32.LocalFree(blob_out.pbData)
    return result


def unprotect(data: bytes) -> bytes:
    blob_in = _bytes_to_blob(data)
    blob_out = DATA_BLOB()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
    )
    if not ok:
        raise OSError("CryptUnprotectData 失败（可能换了用户或数据损坏）")
    result = _blob_to_bytes(blob_out)
    ctypes.windll.kernel32.LocalFree(blob_out.pbData)
    return result


def encrypt_str(text: str) -> str:
    """加密字符串，返回 base64 文本。"""
    return base64.b64encode(protect(text.encode("utf-8"))).decode("ascii")


def decrypt_str(text: str) -> str:
    """解密 encrypt_str 的结果，失败抛异常。"""
    return unprotect(base64.b64decode(text.encode("ascii"))).decode("utf-8")
