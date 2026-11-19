#!/usr/bin/env python3
"""Собирает bot/miniapp.html из bot/miniapp.src.html: вставляет логотипы операторов (icons/operators/*.png).
Запуск после правок исходника: python3 miniapp-build.py"""
import base64
import json
import pathlib

root = pathlib.Path(__file__).resolve().parent
src = (root / "bot/miniapp.src.html").read_text(encoding="utf-8")
logos = {p.stem: "data:image/png;base64," + base64.b64encode(p.read_bytes()).decode()
         for p in sorted((root / "icons/operators").glob("*.png"))}
(root / "bot/miniapp.html").write_text(src.replace("__LOGOS__", json.dumps(logos)), encoding="utf-8")
print("bot/miniapp.html готов, логотипов:", len(logos))
