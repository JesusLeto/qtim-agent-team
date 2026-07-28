#!/usr/bin/env python3
"""Семантический golden: examples/ должен выглядеть как валидный вывод /qtim:setup.

Отсутствие плейсхолдеров (check_placeholders.py) говорит лишь, что подстановка отработала.
Здесь проверяется то, на что опираются движок и `/qtim:doctor`: обязательные секции charter,
штамп версии, поля frontmatter ролей, инварианты `tools:` и agent-memory, схема отчёта
`reviewer`. Разъезд эталона с каноном ловится тут, а не на ревью глазами.

Проверки charter:
  * штамп `generated-by: qtim vX.Y.Z · mode: <plugin-linked|standalone>`, версия = plugin.json;
  * секции «Состав», «Intake-режим», «Правила работы», «Файлы памяти» (их требует doctor);
  * строка «Модели» под таблицей состава (с 1.11.0 — единственное место тира для ролей
    без собственного файла);
  * ни одного абсолютного пути к `reference/*` (с 1.12.0 их там быть не должно).

Проверки ролей:
  * frontmatter парсится, `name` совпадает с именем файла;
  * `model` — алиас тира (`opus`/`sonnet`/`haiku`), не `inherit` и не ID поколения;
  * `tools` без упразднённых (`MultiEdit`, `Computer`, голый `Task`);
  * роль ссылается на скилы → `Skill` заявлен в `tools` (иначе рантайм его не выдаст);
  * `memory` только из документированных значений + существует `agent-memory/<name>/MEMORY.md`;
  * header-note шаблона не перенесён, секция «Память роли» на месте;
  * `reviewer` несёт в схеме отчёта строки `## codex-consult:` и `## adversary:`;
  * каждая роль charter (кроме встроенных типов) имеет файл.
"""
import json
import pathlib
import re
import sys

PLUGIN_JSON = pathlib.Path("plugins/qtim/.claude-plugin/plugin.json")
EXAMPLES = pathlib.Path("examples")

STAMP = re.compile(
    r"generated-by: qtim v(\d+\.\d+\.\d+) · mode: (plugin-linked|standalone)"
)
CHARTER_SECTIONS = ["## Состав", "## Intake-режим", "## Правила работы", "## Файлы памяти"]
MODELS_LINE = re.compile(r"^\*\*Модели:\*\*", re.M)
ABS_REFERENCE = re.compile(r"(?:/|~/)[\w./-]*reference/[\w-]+\.md")
MODEL_TIERS = {"opus", "sonnet", "haiku"}
RETIRED_TOOLS = {"MultiEdit", "Computer", "Task"}
MEMORY_VALUES = {"user", "project", "local"}
BUILTIN_TYPES = {"Explore", "Plan", "general-purpose"}
HEADER_NOTE = "Это generic-шаблон роли"
CHARTER_ROW = re.compile(r"^\|\s*([\w-]+)\s*\|\s*([\w-]+)\s*\|")


def parse_frontmatter(text):
    """Минимальный парсер: ключи верхнего уровня, значения-скаляры и inline-списки."""
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---", 4)
    if end == -1:
        return None
    data = {}
    for line in text[4:end].splitlines():
        if not line or line.startswith((" ", "\t", "#")):
            continue
        key, sep, value = line.partition(":")
        if not sep:
            continue
        value = value.strip()
        if value.startswith("[") and value.endswith("]"):
            items = [v.strip().strip("\"'") for v in value[1:-1].split(",")]
            data[key.strip()] = [v for v in items if v]
        else:
            data[key.strip()] = value.strip("\"'")
    return data


def check_charter(path, plugin_version, problems):
    text = path.read_text(encoding="utf-8")

    stamp = STAMP.search(text)
    if not stamp:
        problems.append(f"{path}: нет штампа `generated-by: qtim vX.Y.Z · mode: …` — его читают hook дрейфа версий и team-sync")
    elif stamp.group(1) != plugin_version:
        problems.append(
            f"{path}: штамп версии {stamp.group(1)}, а в plugin.json {plugin_version} — эталон отстал от плагина"
        )

    for section in CHARTER_SECTIONS:
        if section not in text:
            problems.append(f"{path}: нет обязательной секции «{section}» (её требует /qtim:doctor)")

    if not MODELS_LINE.search(text):
        problems.append(
            f"{path}: нет строки «**Модели:**» под таблицей состава — для ролей без своего файла это единственное место тира"
        )

    for match in ABS_REFERENCE.finditer(text):
        problems.append(
            f"{path}: абсолютный путь к reference — «{match.group(0)}»; с 1.12.0 роли ходят в скил по имени"
        )

    roles = {}
    for line in text.splitlines():
        m = CHARTER_ROW.match(line)
        if m and m.group(1) not in {"Роль", "---"}:
            roles[m.group(1)] = m.group(2)
    return roles


def check_role(path, problems):
    text = path.read_text(encoding="utf-8")
    fm = parse_frontmatter(text)
    if fm is None:
        problems.append(f"{path}: frontmatter не парсится")
        return

    expected_name = path.stem
    if fm.get("name") != expected_name:
        problems.append(f"{path}: name={fm.get('name')!r}, а файл называется {expected_name!r}")

    model = fm.get("model")
    if model not in MODEL_TIERS:
        hint = "inherit привязал бы роль к модели сессии" if model == "inherit" else "тир пишем алиасом, не ID поколения"
        problems.append(f"{path}: model={model!r} — ожидается алиас тира {sorted(MODEL_TIERS)} ({hint})")

    tools = fm.get("tools") or []
    if not isinstance(tools, list) or not tools:
        problems.append(f"{path}: tools не список или пуст")
        tools = []
    for retired in RETIRED_TOOLS & set(tools):
        problems.append(f"{path}: в tools упразднённый {retired!r}")

    body = text.split("\n---", 1)[-1]
    if ("qtim:" in body or "Skill`" in body) and "Skill" not in tools:
        problems.append(
            f"{path}: роль ссылается на скилы, но `Skill` не заявлен в tools — рантайм его не выдаст"
        )

    memory = fm.get("memory")
    has_memory_section = "## Память роли" in text
    if memory is not None:
        if memory not in MEMORY_VALUES:
            problems.append(
                f"{path}: memory={memory!r} — документированы только {sorted(MEMORY_VALUES)}; отключение = отсутствие поля"
            )
        index = path.parent.parent / "agent-memory" / expected_name / "MEMORY.md"
        if not index.exists():
            problems.append(f"{path}: memory включена, но нет {index} — первый спавн роли шумит ошибкой чтения")
        if not has_memory_section:
            problems.append(f"{path}: memory включена, но нет секции «## Память роли»")
    elif has_memory_section:
        problems.append(
            f"{path}: секция «## Память роли» есть, а поля memory нет — роль учат вести память, которой рантайм ей не выдаст"
        )

    if HEADER_NOTE in text:
        problems.append(f"{path}: header-note шаблона перенесён в сгенерированное (setup 4.2 запрещает)")

    if expected_name.startswith("reviewer"):
        # Именно строка схемы, а не упоминание в прозе под ней («Строка `## adversary:` обязательна…»):
        # иначе удаление строки из шаблона отчёта пройдёт незамеченным.
        for required in ("## codex-consult:", "## adversary:"):
            if not re.search(rf"^{re.escape(required)}", text, re.M):
                problems.append(
                    f"{path}: в схеме отчёта нет строки «{required}» — вердикт потеряет сигнал риска"
                )


def main():
    problems = []
    plugin_version = json.loads(PLUGIN_JSON.read_text(encoding="utf-8"))["version"]
    charters = sorted(EXAMPLES.rglob(".claude/team-charter.md"))
    if not charters:
        print("Не найдено ни одного charter в examples/ — проверка деградировала", file=sys.stderr)
        return 1

    roles_checked = 0
    for charter in charters:
        roles = check_charter(charter, plugin_version, problems)
        agents_dir = charter.parent / "agents"
        for role, subagent_type in roles.items():
            if subagent_type in BUILTIN_TYPES:
                continue
            path = agents_dir / f"{subagent_type}.md"
            if not path.exists():
                problems.append(
                    f"{charter}: роль {role!r} заявлена как {subagent_type!r}, но {path} отсутствует"
                )
        for path in sorted(agents_dir.glob("*.md")):
            check_role(path, problems)
            roles_checked += 1

    if problems:
        print("Golden-эталон разошёлся с каноном:")
        for p in problems:
            print(f"  - {p}")
        return 1

    print(
        f"OK: golden семантически валиден ({len(charters)} charter, {roles_checked} ролей, "
        f"версия {plugin_version})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
