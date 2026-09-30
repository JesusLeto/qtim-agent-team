#!/usr/bin/env python3
"""Относительные markdown-ссылки в plugins/ указывают на существующие файлы.

Код (ограды и inline) вырезается до разбора: `- [Заголовок](файл.md)` в описании формата —
пример, а не ссылка. Ноль найденных .md — fail: запуск не из корня репозитория иначе дал бы
зелёную проверку по пустому списку.
"""
import pathlib
import re
import sys

CODE = re.compile(r"```.*?```|`[^`\n]*`", re.DOTALL)
files = sorted(pathlib.Path("plugins").rglob("*.md"))
if not files:
    print("plugins/**/*.md не найдено — запусти из корня репозитория; проверка не должна проходить молча",
          file=sys.stderr)
    sys.exit(1)

bad = []
for path in files:
    text = CODE.sub("", path.read_text(encoding="utf-8"))
    for m in re.finditer(r"\]\(([^)\s]+)\)", text):
        target = m.group(1)
        if target.startswith(("http://", "https://", "mailto:", "#")):
            continue
        rel = target.split("#")[0]
        if not rel:
            continue
        if not (path.parent / rel).exists():
            bad.append(f"{path}: {target}")

if bad:
    print("Битые относительные ссылки:")
    print("\n".join(bad))
    sys.exit(1)

print(f"OK: относительные ссылки в {len(files)} файлах plugins/**/*.md целы")
