"""Current-user Windows DPAPI storage for the Youdao translation secret."""

import ctypes
import os
from ctypes import wintypes
from pathlib import Path


class CredentialError(RuntimeError):
    """A credential could not be saved or read without exposing its contents."""


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def secret_path(kind="translation"):
    local_app_data = os.environ.get("LOCALAPPDATA")
    if os.name != "nt" or not local_app_data:
        raise CredentialError("仅支持在 Windows 当前用户目录保存密钥")
    filenames = {"translation": "youdao_secret.dpapi", "tts": "youdao_tts_secret.dpapi"}
    if kind not in filenames:
        raise CredentialError("未知密钥类型")
    return Path(local_app_data) / "ScreenTrans" / filenames[kind]


def _transform(payload, protect):
    if os.name != "nt":
        raise CredentialError("当前系统不支持 Windows DPAPI")
    source = ctypes.create_string_buffer(payload)
    input_blob = _DataBlob(len(payload), ctypes.cast(source, ctypes.POINTER(ctypes.c_ubyte)))
    output_blob = _DataBlob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    function = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    function.argtypes = [ctypes.POINTER(_DataBlob), ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
                         ctypes.POINTER(_DataBlob)]
    function.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    try:
        if not function(ctypes.byref(input_blob), None, None, None, None, 0x1,
                        ctypes.byref(output_blob)):
            raise CredentialError("密钥加密失败" if protect else "无法解密本机密钥，请重新输入")
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        if output_blob.pbData:
            ctypes.memset(output_blob.pbData, 0, output_blob.cbData)
            kernel32.LocalFree(output_blob.pbData)
        ctypes.memset(source, 0, len(source))


def has_secret(kind="translation"):
    return secret_path(kind).is_file()


def read_secret(kind="translation"):
    path = secret_path(kind)
    if not path.exists():
        return ""
    try:
        return _transform(path.read_bytes(), False).decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise CredentialError("无法读取本机密钥，请重新输入") from exc


def save_secret(value, kind="translation"):
    if not isinstance(value, str) or not value or "\n" in value or "\r" in value:
        raise CredentialError("应用密钥不能为空或包含换行")
    path = secret_path(kind)
    encrypted = _transform(value.encode("utf-8"), True)
    if _transform(encrypted, False).decode("utf-8") != value:
        raise CredentialError("密钥保存验证失败")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        with open(temporary, "wb") as stream:
            stream.write(encrypted)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def clear_secret(kind="translation"):
    secret_path(kind).unlink(missing_ok=True)
