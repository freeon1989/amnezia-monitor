#!/usr/bin/env python3
"""Генерирует android/.../data/ThemeData.kt и ui/ThemeRes.kt из theme.py — одни и те же страны,
цвета и ключевые слова на ПК и в телефоне. Запуск: python scripts/gen_android_theme.py [--check]"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import theme  # noqa: E402

PKG = os.path.join(ROOT, "android", "app", "src", "main", "java", "org", "amnezia", "monitor")
RES = os.path.join(ROOT, "android", "app", "src", "main", "res", "drawable-nodpi")
HEAD = "// Сгенерировано scripts/gen_android_theme.py из theme.py — не править вручную.\n"


def kstr(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$") + '"'


def argb(css: str) -> str:
    css = css.strip()
    m = re.match(r"^#([0-9a-fA-F]{6})$", css)
    if m:
        return "0xFF" + m.group(1).upper()
    m = re.match(r"^rgba\((\d+),(\d+),(\d+),([\d.]+)\)$", css.replace(" ", ""))
    if m:
        r, g, b, a = int(m.group(1)), int(m.group(2)), int(m.group(3)), float(m.group(4))
        return "0x%02X%02X%02X%02X" % (round(a * 255), r, g, b)
    raise ValueError(css)


def theme_data() -> str:
    out = [HEAD, "package org.amnezia.monitor.data\n\n",
           "/** Тема страны: название, акцент, второй акцент, свечение (ARGB). */\n",
           "data class ThemeSpec(val name: String, val accent: Long, val accent2: Long, val glow: Long)\n\n",
           "object ThemeData {\n    val THEMES: Map<String, ThemeSpec> = linkedMapOf(\n"]
    for k, (name, a1, a2, glow) in theme.THEMES.items():
        out.append(f"        {kstr(k)} to ThemeSpec({kstr(name)}, {argb(a1)}, {argb(a2)}, {argb(glow)}),\n")
    out.append("    )\n\n    /** Страны без своей героини: общая героиня, свой флаг. */\n")
    out.append("    val COUNTRY_NAMES: Map<String, String> = linkedMapOf(\n")
    for k, v in theme.COUNTRY_NAMES.items():
        out.append(f"        {kstr(k)} to {kstr(v)},\n")
    out.append("    )\n\n    /** Слова в названии сервера (RU/EN). Слова до 4 символов — только целиком. */\n")
    out.append("    val KEYWORDS: Map<String, List<String>> = linkedMapOf(\n")
    for k, words in theme.KEYWORDS.items():
        out.append(f"        {kstr(k)} to listOf(" + ", ".join(kstr(w) for w in words) + "),\n")
    out.append("    )\n}\n")
    return "".join(out)


def theme_res() -> str:
    flags = sorted(f[5:-5] for f in os.listdir(RES) if f.startswith("flag_") and f.endswith(".webp"))
    out = [HEAD, "package org.amnezia.monitor.ui\n\nimport org.amnezia.monitor.R\n\n", "object ThemeRes {\n",
           "    val HEROINES: Map<String, Int> = mapOf(\n"]
    for k in theme.THEMES:
        out.append(f"        {kstr(k)} to R.drawable.heroine_{k},\n")
    out.append("    )\n    /** Фон: город страны (720x1280, кроп из theme_assets/backgrounds). */\n")
    out.append("    val BACKGROUNDS: Map<String, Int> = mapOf(\n")
    for k in theme.THEMES:
        out.append(f"        {kstr(k)} to R.drawable.bg_{k},\n")
    out.append("    )\n    val FLAGS: Map<String, Int> = mapOf(\n")
    for cc in flags:
        out.append(f"        {kstr(cc)} to R.drawable.flag_{cc},\n")
    out.append("    )\n}\n")
    return "".join(out)


def main() -> int:
    targets = {os.path.join(PKG, "data", "ThemeData.kt"): theme_data(),
               os.path.join(PKG, "ui", "ThemeRes.kt"): theme_res()}
    check = "--check" in sys.argv
    bad = []
    for path, text in targets.items():
        cur = open(path, encoding="utf-8").read() if os.path.exists(path) else None
        if cur != text:
            if check:
                bad.append(path)
            else:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(text)
    if bad:
        print("устарели (запустите scripts/gen_android_theme.py):", *bad)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
