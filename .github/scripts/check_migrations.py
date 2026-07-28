#!/usr/bin/env python3
"""Правка, меняющая сгенерированное, обязана нести запись в реестре миграций.

Контракт (CLAUDE.md, «Версионирование и миграции сгенерированного»): изменил шаблоны
`templates/`, структуру charter или settings/hooks-baseline в Phase 4 setup.md — добавь
запись `## → <версия>` в `reference/migrations.md`. Иначе собранные в проектах команды
останутся на старом канале, а `/qtim:team-sync` о правке не узнает.

Проверка держится на диффе против базы:
  * триггер — файлы в `plugins/qtim/templates/**` (роли и hook-скрипты) либо изменённые
    строки внутри `## Phase 4: Generation` в `commands/setup.md`;
  * требуется, чтобы `reference/migrations.md` был в том же диффе и содержал заголовок
    `## → <версия из plugin.json>`.

База: аргумент CLI → $CHECK_MIGRATIONS_BASE → origin/main → HEAD (локальный прогон
незакоммиченного). Untracked-файлы в триггерных путях тоже считаются изменениями —
иначе новый шаблон роли проехал бы мимо проверки.
"""
import json
import pathlib
import re
import subprocess
import sys

TEMPLATES_PREFIX = "plugins/qtim/templates/"
SETUP = "plugins/qtim/commands/setup.md"
MIGRATIONS = "plugins/qtim/reference/migrations.md"
PLUGIN_JSON = "plugins/qtim/.claude-plugin/plugin.json"
PHASE_START = re.compile(r"^## Phase 4: Generation\s*$")
PHASE_END = re.compile(r"^## Phase 5\b")
HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def git(*args):
    result = subprocess.run(
        ["git", *args], capture_output=True, text=True, check=False
    )
    return result.stdout if result.returncode == 0 else ""


def resolve_base():
    if len(sys.argv) > 1:
        return sys.argv[1]
    import os

    env_base = os.environ.get("CHECK_MIGRATIONS_BASE")
    if env_base:
        return env_base
    if git("rev-parse", "--verify", "--quiet", "origin/main").strip():
        return "origin/main"
    return "HEAD"


def changed_files(base):
    names = set(git("diff", "--name-only", base).split())
    for line in git("status", "--porcelain").splitlines():
        # '?? path' — untracked; новый шаблон роли обязан триггерить проверку так же, как правка.
        if line.startswith("?? "):
            path = line[3:].strip()
            if path.endswith("/"):
                names.update(
                    str(p) for p in pathlib.Path(path).rglob("*") if p.is_file()
                )
            else:
                names.add(path)
    return names


def phase4_range(path):
    """Границы Phase 4 в текущей версии setup.md (1-indexed, полуинтервал)."""
    lines = pathlib.Path(path).read_text(encoding="utf-8").splitlines()
    start = end = None
    for i, line in enumerate(lines, start=1):
        if start is None and PHASE_START.match(line):
            start = i
        elif start is not None and PHASE_END.match(line):
            end = i
            break
    if start is None:
        return None
    return start, end if end is not None else len(lines) + 1


def setup_phase4_touched(base):
    bounds = phase4_range(SETUP)
    if bounds is None:
        return False
    start, end = bounds
    diff = git("diff", "-U0", base, "--", SETUP)
    for line in diff.splitlines():
        m = HUNK.match(line)
        if not m:
            continue
        first = int(m.group(1))
        count = int(m.group(2)) if m.group(2) is not None else 1
        if count == 0:  # чистое удаление: правка «прилегает» к строке first
            if start <= first < end:
                return True
            continue
        if first < end and first + count - 1 >= start:
            return True
    return False


def main():
    base = resolve_base()
    # База не резолвится (обрезанная история, нулевой sha первого пуша) — падаем явно:
    # `git diff` по битой базе вернул бы пустой список, и проверка молча стала бы зелёной.
    if not git("rev-parse", "--verify", "--quiet", f"{base}^{{commit}}").strip():
        print(
            f"База сравнения «{base}» не резолвится в коммит — проверка не может отработать.\n"
            "В CI нужен actions/checkout с fetch-depth: 0; локально передай базу аргументом "
            "(например HEAD или origin/main).",
            file=sys.stderr,
        )
        return 1
    changed = changed_files(base)
    triggers = sorted(f for f in changed if f.startswith(TEMPLATES_PREFIX))
    if setup_phase4_touched(base):
        triggers.append(f"{SETUP} (Phase 4: Generation)")

    if not triggers:
        print(f"OK: сгенерированное не затронуто (база {base}) — запись в реестр не требуется")
        return 0

    version = json.loads(pathlib.Path(PLUGIN_JSON).read_text(encoding="utf-8"))["version"]
    entry = f"## → {version}"
    migrations_text = pathlib.Path(MIGRATIONS).read_text(encoding="utf-8")

    problems = []
    if MIGRATIONS not in changed:
        problems.append(
            f"{MIGRATIONS} не изменён в этом диффе — правка сгенерированного без записи "
            f"в реестре не доедет до собранных команд через /qtim:team-sync"
        )
    if entry not in migrations_text:
        problems.append(
            f"в {MIGRATIONS} нет записи «{entry}» под текущую версию плагина "
            f"(она в {PLUGIN_JSON}) — добавь её или забампай версию"
        )

    if problems:
        print("Изменено сгенерированное:")
        for t in triggers:
            print(f"  - {t}")
        print("\nПроблемы:")
        for p in problems:
            print(f"  - {p}")
        print(
            "\nКонтракт — CLAUDE.md, «Версионирование и миграции сгенерированного»: "
            "запись `## → <версия>` в реестре + строка «team-sync» в CHANGELOG + минорный бамп."
        )
        return 1

    print(
        f"OK: сгенерированное затронуто ({len(triggers)} путь(и)), "
        f"запись «{entry}» в реестре на месте"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
