# -*- coding: utf-8 -*-
"""Хранение секретов (пароли SSH, токен Telegram) в файле настроек.

Windows: шифрование DPAPI (CryptProtectData, область «текущий пользователь») через ctypes.
Расшифровать файл может только тот же пользователь Windows на этом же компьютере.
Значение хранится как "dpapi:<base64>".

Другие ОС: DPAPI нет, значение хранится открытым текстом ("plain:<текст>"),
а файл настроек получает права 600 (только владелец).
"""
from __future__ import annotations

import base64
import os
import sys

IS_WINDOWS = sys.platform.startswith("win")
_ENTROPY = b"AmneziaMonitor/v1"       # доп. «соль» DPAPI: чужие программы не расшифруют без неё
PREFIX_DPAPI, PREFIX_PLAIN = "dpapi:", "plain:"


def _dpapi_protect(data: bytes) -> bytes:  # pragma: no cover - только Windows
    import ctypes
    from ctypes import wintypes

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    def blob(b):
        buf = ctypes.create_string_buffer(b, len(b))
        return BLOB(len(b), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf

    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    src, _k1 = blob(data)
    ent, _k2 = blob(_ENTROPY)
    out = BLOB()
    CRYPTPROTECT_UI_FORBIDDEN = 0x1
    if not crypt32.CryptProtectData(ctypes.byref(src), "AmneziaMonitor", ctypes.byref(ent), None, None,
                                    CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out)):
        raise OSError("CryptProtectData failed")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


def _dpapi_unprotect(data: bytes) -> bytes:  # pragma: no cover - только Windows
    import ctypes
    from ctypes import wintypes

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    def blob(b):
        buf = ctypes.create_string_buffer(b, len(b))
        return BLOB(len(b), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf

    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    src, _k1 = blob(data)
    ent, _k2 = blob(_ENTROPY)
    out = BLOB()
    if not crypt32.CryptUnprotectData(ctypes.byref(src), None, ctypes.byref(ent), None, None, 0x1,
                                      ctypes.byref(out)):
        raise OSError("CryptUnprotectData failed")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


def protect(secret: str) -> str:
    """Текст -> строка для файла настроек."""
    if IS_WINDOWS:
        return PREFIX_DPAPI + base64.b64encode(_dpapi_protect(secret.encode("utf-8"))).decode("ascii")
    return PREFIX_PLAIN + secret


def unprotect(value: str) -> str:
    """Строка из файла -> текст. Бросает исключение, если расшифровать нельзя."""
    if value.startswith(PREFIX_DPAPI):
        if not IS_WINDOWS:
            raise OSError("DPAPI-значение можно расшифровать только в Windows")
        return _dpapi_unprotect(base64.b64decode(value[len(PREFIX_DPAPI):])).decode("utf-8")
    if value.startswith(PREFIX_PLAIN):
        return value[len(PREFIX_PLAIN):]
    raise ValueError("Неизвестный формат зашифрованного значения")


def restrict_file(path) -> None:
    """chmod 600 вне Windows (в Windows файл лежит в профиле пользователя %APPDATA%)."""
    if not IS_WINDOWS:
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
