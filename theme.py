# -*- coding: utf-8 -*-
"""Оформление панели «Neon Latex»: страна сервера -> флаг, героиня и акцентные цвета.

Страна определяется так (первое найденное):
  1) поле ``country`` сервера (выбирается в настройках, «Страна»);
  2) слова в названии сервера (RU/EN/транслит: «Svecia», «Вена», «Kazahi», «Питер», «Frankfurt»…);
  3) GeoIP по IP-адресу (ip-api.com) — только если включено в настройках, по умолчанию выключено.
     Сервису уходит только IP-адрес; ответ кэшируется на 30 дней, запрос идёт в фоне и не тормозит панель;
  4) иначе — общая тема «Amnezia».
Флаги: flag-icons (MIT), лежат в theme_assets/flags. Героини: theme_assets/heroines/*.webp.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Optional

log = logging.getLogger("amnezia_monitor.theme")

_BASE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
ASSETS_DIR = _BASE / "theme_assets"
ASSET_RE = re.compile(r"^/theme/(heroines|flags)/([a-z0-9_]{1,40})\.(webp|svg)$")
CONTENT_TYPES = {"webp": "image/webp", "svg": "image/svg+xml"}

# Код страны -> (название, акцент, второй акцент, свечение)
THEMES = {
    "default": ("Amnezia", "#a855f7", "#22d3ee", "rgba(168,85,247,.55)"),
    "se": ("Швеция", "#1e8fe0", "#fecc02", "rgba(30,143,224,.55)"),
    "us": ("США", "#5b6cff", "#ff4d5e", "rgba(91,108,255,.55)"),
    "at": ("Австрия", "#ef3346", "#ffffff", "rgba(239,51,70,.5)"),
    "kz": ("Казахстан", "#00c2e0", "#fec50c", "rgba(0,194,224,.55)"),
    "nl": ("Нидерланды", "#ff7f00", "#3a6fd8", "rgba(255,127,0,.5)"),
    "ru": ("Россия", "#2f6bff", "#ff4b3e", "rgba(47,107,255,.55)"),
    "pl": ("Польша", "#e8213f", "#ffffff", "rgba(232,33,63,.5)"),
    "de": ("Германия", "#ff2a2a", "#ffce00", "rgba(255,206,0,.45)"),
    "fr": ("Франция", "#2f6fe0", "#ef4135", "rgba(47,111,224,.55)"),
    "gb": ("Великобритания", "#3a5bd9", "#e0263f", "rgba(58,91,217,.55)"),
    "fi": ("Финляндия", "#3c7ff0", "#e8f1ff", "rgba(60,127,240,.5)"),
    "in": ("Индия", "#ff9933", "#1fae3a", "rgba(255,153,51,.5)"),
    "il": ("Израиль", "#2f63e8", "#e8f0ff", "rgba(47,99,232,.55)"),
    "hk": ("Гонконг", "#e8301a", "#ffffff", "rgba(232,48,26,.55)"),
}

# Страны без своей героини: общая героиня, но свой флаг и название
COUNTRY_NAMES = {
    "no": "Норвегия", "dk": "Дания", "ie": "Ирландия", "es": "Испания", "pt": "Португалия", "it": "Италия",
    "ch": "Швейцария", "cz": "Чехия", "sk": "Словакия", "hu": "Венгрия", "ro": "Румыния", "bg": "Болгария",
    "md": "Молдова", "ua": "Украина", "by": "Беларусь", "lt": "Литва", "lv": "Латвия", "ee": "Эстония",
    "uz": "Узбекистан", "kg": "Киргизия", "tj": "Таджикистан", "tm": "Туркменистан", "tr": "Турция",
    "ge": "Грузия", "am": "Армения", "az": "Азербайджан", "ae": "ОАЭ", "sa": "Саудовская Аравия",
    "sg": "Сингапур", "jp": "Япония", "kr": "Южная Корея", "tw": "Тайвань", "cn": "Китай", "vn": "Вьетнам",
    "th": "Таиланд", "my": "Малайзия", "id": "Индонезия", "au": "Австралия", "nz": "Новая Зеландия",
    "ca": "Канада", "br": "Бразилия", "ar": "Аргентина", "mx": "Мексика", "za": "ЮАР", "eg": "Египет",
    "cy": "Кипр", "gr": "Греция", "rs": "Сербия", "hr": "Хорватия", "si": "Словения", "is": "Исландия",
    "lu": "Люксембург", "be": "Бельгия",
}

KEYWORDS = {
    "se": ["svecia", "sweden", "swe", "stockholm", "швец", "стокгольм"],
    "us": ["usa", "united states", "america", "new york", "nyc", "charlotte", "los angeles", "chicago", "dallas",
           "miami", "сша", "америк", "нью-йорк", "шарлотт", "лос-анджелес", "чикаго", "даллас", "майами"],
    "at": ["vena", "vienna", "wien", "austria", "вена", "австри"],
    "kz": ["kazahi", "kazakh", "almaty", "astana", "казах", "алматы", "астана"],
    "nl": ["amsterdam", "netherlands", "holland", "dutch", "амстердам", "нидерланд", "голланд"],
    "ru": ["piter", "spb", "petersburg", "moscow", "moskva", "russia",
           "питер", "спб", "петербург", "москва", "росси"],
    "de": ["germany", "deutschland", "frankfurt", "berlin", "munich", "falkenstein", "nuremberg",
           "герман", "франкфурт", "берлин", "мюнхен", "фалькенштайн", "нюрнберг"],
    "fi": ["finland", "suomi", "helsinki", "финлянд", "хельсинки"],
    "pl": ["poland", "polska", "warsaw", "warszawa", "krakow", "польш", "варшав", "краков"],
    "fr": ["france", "paris", "marseille", "франци", "париж", "марсел"],
    "gb": ["united kingdom", "england", "britain", "london", "manchester", "uk",
           "великобритан", "англи", "британ", "лондон", "манчестер"],
    "in": ["india", "mumbai", "bangalore", "delhi", "chennai", "индия", "индии", "мумба", "бангалор", "дели",
           "ченна"],
    "il": ["israel", "tel aviv", "jerusalem", "израил", "тель-авив", "иерусалим"],
    "hk": ["hong kong", "hongkong", "гонконг"],
}

GEO_CACHE_TTL = 30 * 24 * 3600
_geo_lock = threading.Lock()
_geo_pending: set = set()


def country_name(cc: str) -> str:
    cc = (cc or "").lower()
    if cc in THEMES and cc != "default":
        return THEMES[cc][0]
    return COUNTRY_NAMES.get(cc, cc.upper())


def known_countries() -> list:
    """Для выпадающего списка «Страна»: [(код, название)], сначала страны со своими героинями."""
    own = sorted(((c, v[0]) for c, v in THEMES.items() if c != "default"), key=lambda x: x[1])
    rest = sorted(COUNTRY_NAMES.items(), key=lambda x: x[1])
    return own + rest


def normalize_country(value) -> str:
    v = str(value or "").strip().lower()
    if v in ("", "auto", "авто"):
        return ""
    if not re.match(r"^[a-z]{2}$", v):
        raise ValueError("Страна: двухбуквенный код (например, de) или пусто = автоматически")
    return v


def by_name(name: str) -> Optional[str]:
    n = (name or "").lower()
    for cc, words in KEYWORDS.items():
        for w in words:
            # короткие слова («usa», «uk», «спб», «вена», «дели») — только целиком,
            # чтобы «Ukraine», «недели», «Swedbank» не срабатывали
            if len(w) <= 4:
                if re.search(rf"(?<!\w){re.escape(w)}(?!\w)", n):
                    return cc
            elif w in n:
                return cc
    return None


def _read_cache(path) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _geo_fetch(ip: str, cache_path: str, fetch=None) -> None:
    try:
        if fetch:
            cc = fetch(ip)
        else:
            url = f"http://ip-api.com/json/{ip}?fields=status,countryCode"
            with urllib.request.urlopen(url, timeout=5) as r:
                data = json.load(r)
            cc = data.get("countryCode", "").lower() if data.get("status") == "success" else ""
        cc = cc if re.match(r"^[a-z]{2}$", cc or "") else ""
        with _geo_lock:
            cache = _read_cache(cache_path)
            cache[ip] = {"cc": cc, "t": time.time()}
            tmp = cache_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(cache, f)
            os.replace(tmp, cache_path)
    except Exception as e:   # сеть недоступна и т. п. — просто без страны
        log.info("GeoIP: не удалось определить страну (%s)", type(e).__name__)
    finally:
        with _geo_lock:
            _geo_pending.discard(ip)


def by_geoip(host: str, cache_path: Optional[str], background: bool = True, fetch=None) -> Optional[str]:
    """Страна по IP из кэша; если в кэше нет — запускает фоновый запрос и пока возвращает None."""
    try:
        ip = ipaddress.ip_address(str(host or "").strip("[]"))
    except ValueError:
        return None                       # домен — не резолвим (лишний DNS-запрос)
    if not ip.is_global or not cache_path:
        return None
    key = str(ip)
    with _geo_lock:
        hit = _read_cache(cache_path).get(key)
    if hit and time.time() - float(hit.get("t", 0)) < GEO_CACHE_TTL:
        return hit.get("cc") or None
    with _geo_lock:
        if key in _geo_pending:
            return (hit or {}).get("cc") or None
        _geo_pending.add(key)
    if background:
        threading.Thread(target=_geo_fetch, args=(key, cache_path, fetch), daemon=True).start()
        return (hit or {}).get("cc") or None
    _geo_fetch(key, cache_path, fetch)
    with _geo_lock:
        return (_read_cache(cache_path).get(key) or {}).get("cc") or None


def detect_country(name: str, host: str = "", manual: str = "", geoip: bool = False,
                   cache_path: Optional[str] = None) -> tuple:
    """-> (код страны или "", источник: manual | name | geoip | none)."""
    m = (manual or "").strip().lower()
    if re.match(r"^[a-z]{2}$", m):
        return m, "manual"
    cc = by_name(name)
    if cc:
        return cc, "name"
    if geoip:
        cc = by_geoip(host, cache_path)
        if cc:
            return cc, "geoip"
    return "", "none"


def _flag_url(cc: str) -> Optional[str]:
    if cc and (ASSETS_DIR / "flags" / f"{cc}.svg").is_file():
        return f"/theme/flags/{cc}.svg"
    return None


def theme_for(name: str, host: str = "", manual: str = "", geoip: bool = False,
              cache_path: Optional[str] = None) -> dict:
    cc, source = detect_country(name, host, manual, geoip, cache_path)
    key = cc if cc in THEMES else "default"
    _, accent, accent2, glow = THEMES[key]
    return {"country": cc, "country_name": country_name(cc) if cc else "", "source": source, "key": key,
            "flag": _flag_url(cc), "heroine": f"/theme/heroines/heroine_{key}.webp",
            "accent": accent, "accent2": accent2, "glow": glow}


def asset(path: str):
    """URL /theme/... -> (bytes, content-type) или None. Только файлы из theme_assets по белому списку имён."""
    m = ASSET_RE.match(path or "")
    if not m:
        return None
    sub, name, ext = m.groups()
    if (sub == "flags") != (ext == "svg"):
        return None
    p = ASSETS_DIR / sub / f"{name}.{ext}"
    try:
        return p.read_bytes(), CONTENT_TYPES[ext]
    except OSError:
        return None
