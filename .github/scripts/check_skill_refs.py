#!/usr/bin/env python3
"""Ссылки на собственные скилы плагина резолвятся, имена скилов совпадают с каталогами.

Зачем: вызов скила стал строкой обязательных чеклистов ролей (`qtim:minimal-diff`
у `front` и `db`, `qtim:brainstorm` у `architect`, `qtim:debug-loop` у исполнителей).
Опечатка в имени не ломает ни одну другую проверку: `check_links.py` смотрит только
markdown-ссылки, `check_golden.py` — лишь наличие инструмента `Skill` во frontmatter.
Роль при этом молча не выполнит пункт чеклиста — скил с таким именем не резолвится.

Проверяем три инварианта:
  * `name:` в **первом frontmatter-блоке** `skills/<dir>/SKILL.md` равен имени каталога —
    рантайм адресует скил по `name`, а ссылки в промптах и Standalone-копирование идут
    по каталогу;
  * каждое упоминание `qtim:<имя>` без ведущего слэша в `plugins/`, `examples/` и корневых
    `*.md` резолвится в существующий скил. Имя захватывается целиком, включая недопустимые
    символы, — иначе `qtim:minimal-diff_typo` прошло бы как валидный префикс. Упоминание
    команды (`qtim:setup`) допускаем: со слэшем или без, это другой namespace;
  * охват ненулевой: ноль просмотренных ссылок = проверка молча деградировала, это fail.
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SKILLS_DIR = ROOT / "plugins" / "qtim" / "skills"
COMMANDS_DIR = ROOT / "plugins" / "qtim" / "commands"
SCAN_DIRS = [ROOT / "plugins", ROOT / "examples"]

# Имя забираем целиком до разделителя: `qtim:foo_typo` должно стать `foo_typo`
# и не резолвиться, а не усечься до валидного префикса `foo`.
REF_RE = re.compile(r"(?<![/\w])qtim:([A-Za-z0-9_.-]+)")
FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?$", re.DOTALL | re.MULTILINE)
NAME_RE = re.compile(r"^name:\s*(.+?)\s*$", re.MULTILINE)


def declared_name(text):
    """Значение `name:` из первого frontmatter-блока; None, если блока или ключа нет."""
    block = FRONTMATTER_RE.search(text)
    if not block:
        return None
    found = NAME_RE.search(block.group(1))
    if not found:
        return None
    return found.group(1).strip().strip("\"'")


def skill_names(problems):
    names = set()
    if not SKILLS_DIR.is_dir():
        problems.append(f"{SKILLS_DIR}: каталога скилов нет — проверка деградировала бы молча")
        return names
    for skill_dir in sorted(p for p in SKILLS_DIR.iterdir() if p.is_dir()):
        manifest = skill_dir / "SKILL.md"
        if not manifest.is_file():
            problems.append(f"{skill_dir.relative_to(ROOT)}: нет SKILL.md")
            continue
        names.add(skill_dir.name)
        name = declared_name(manifest.read_text(encoding="utf-8"))
        if name is None:
            problems.append(
                f"{manifest.relative_to(ROOT)}: во frontmatter (первый блок `--- … ---`) нет `name:`"
            )
        elif name != skill_dir.name:
            problems.append(
                f"{manifest.relative_to(ROOT)}: `name: {name}` не совпадает с каталогом "
                f"`{skill_dir.name}` — рантайм адресует скил по name"
            )
    if not names:
        problems.append(f"{SKILLS_DIR.relative_to(ROOT)}: не найдено ни одного скила")
    return names


def command_names():
    if not COMMANDS_DIR.is_dir():
        return set()
    return {p.stem for p in COMMANDS_DIR.glob("*.md")}


def scanned_files(problems):
    for scan_dir in SCAN_DIRS:
        if not scan_dir.is_dir():
            problems.append(f"{scan_dir.relative_to(ROOT)}: каталога нет — область проверки сузилась")
            continue
        yield from sorted(scan_dir.rglob("*.md"))
    yield from sorted(ROOT.glob("*.md"))


def main():
    problems = []
    skills = skill_names(problems)
    commands = command_names()
    checked = 0

    for path in scanned_files(problems):
        text = path.read_text(encoding="utf-8")
        for match in REF_RE.finditer(text):
            ref = match.group(1)
            checked += 1
            if ref in skills or ref in commands:
                continue
            line = text.count("\n", 0, match.start()) + 1
            problems.append(
                f"{path.relative_to(ROOT)}:{line}: ссылка `qtim:{ref}` не резолвится "
                f"ни в скил `plugins/qtim/skills/{ref}/`, ни в команду"
            )

    if checked == 0:
        problems.append(
            "просмотрено 0 упоминаний `qtim:*` — стиль ссылок изменился или область скана "
            "пуста; проверка не должна проходить молча"
        )

    if problems:
        print("Проблемы со ссылками на скилы:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    print(
        f"OK: {len(skills)} скил(ов), имена совпадают с каталогами; "
        f"{checked} упоминаний `qtim:*` резолвятся"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
