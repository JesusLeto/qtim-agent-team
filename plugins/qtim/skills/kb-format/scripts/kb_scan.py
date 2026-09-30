#!/usr/bin/env python3
"""kb_scan.py — факты о базе знаний проекта: memory/, docs/features/, auto-memory.

Скрипт отдаёт ФАКТЫ и счётчики. Глубину прогона, оценку стоимости и потолки считает
вызывающая команда; имён команд и скилов в выводе нет. Контракт JSON — schema "kb_scan/1".

Запуск из корня репозитория (из подкаталога скрипт сам переходит в корень):
  kb_scan.py prepare [--base REF] [--no-fetch]     шаг 0: база, где живёт память, затирание, lock
                                                   (единственная подкоманда с сетью: git fetch)
  kb_scan.py snapshot --run ID | --after --run ID | --abandon --run ID
                                                   копия memory/, docs/features/, auto-memory; закрытие прогона
  kb_scan.py stamp --sha SHA (--files F… | --intact)  ставит checked @ sha сверенным файлам
  kb_scan.py validate [--base REF]                 формат, якоря, бюджеты, потребители памяти
  kb_scan.py delta --base REF                      устаревшие файлы по дереву базы
  kb_scan.py features --base REF                   docs/features: статусы, «влита», служебное
  kb_scan.py automemory [--automemory-dir DIR]     auto-memory: лимиты индекса, дубли с проектом
  kb_scan.py lost --snapshot DIR [--tree automemory] [--expect-deleted P…]
                                                   единицы текста, потерянные относительно снимка
  kb_scan.py all --base REF                        validate + delta + features + automemory
  kb_scan.py selftest                              ломающие входы ловятся (без сети и ~/.claude)

Коды выхода: 0 — скан выполнен (находки — данные); validate с замечаниями — 1;
скан невозможен — 2 (JSON всё равно печатается, scan_complete:false).
python3 >= 3.9, только stdlib. Все git-списки путей — с -z; пути сравниваются после NFC.
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata

SCHEMA = "kb_scan/1"

# minimal-diff: калибровка по первым прогонам — все числа ниже; флаги командной строки
# переопределяют. Считать по ним глубину прогона и стоимость — дело вызывающей команды.
CFG = {
    "entry_words": 1800,   # точка входа: предупреждение выше (цель в каноне формата — 1200)
    "role_words": 8000,    # read-on-spawn роли: сумма слов файлов
    "journal_ratio": 1.0,  # journal <= ratio * слов точек входа
    "sample": 30,          # выборка утверждений вне дельты
    "lock_hours": 6,       # живой чужой lock моложе этого — «занято»
    "dup_overlap": 0.85,   # auto-memory: доля идентификаторов, найденных в проекте, от которой файл — дубль
    "only_here_overlap": 0.35,  # ...и до которой знание есть только в auto-memory
    "min_ids": 5,          # меньше идентификаторов — классифицировать нечем
    "history_window": 20,  # детектор затирания: сколько последних версий файла из git смотреть
}
DEFAULT_CFG = dict(CFG)
NOW = None  # epoch-секунды; --now фиксирует время (детерминизм selftest)
FETCH_TIMEOUT = 60

CANON_ARTIFACTS = ("intake.md", "prd.md", "decomposition.md", "estimate.md", "plan.md", "feature-brief.md")
REQUIRED_KEYS = ("name", "description", "type", "genre", "audience", "updated", "checked", "status")
STATUS_VALUES = ("Draft", "Approved", "In Development", "Done", "Archived")
NAV_MARK = "> **Когда читать:**"
EPIC_STATE = "memory/epic-state.md"
LINEREF_EXT = ("ts|tsx|mts|cts|js|mjs|cjs|vue|json|ya?ml|sql|py|go|rb|java|kt|kts|md|sh|css|scss|html|toml|rs|php|cs|"
               "c|h|cc|cpp|hpp|swift|scala|dart|ex|exs|lua|gradle")


def now():
    return NOW if NOW is not None else int(time.time())


def nfc(s):
    return unicodedata.normalize("NFC", s)


def count_words(text):
    return len(re.findall(r"\w+", text))


def read(path):
    try:
        with open(path, encoding="utf-8", errors="surrogateescape") as f:
            return f.read()
    except OSError:
        return None


# ---------------------------------------------------------------- git

def git_env():
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["LC_ALL"] = "C"  # сообщения git на английском: по ним различаем «ambiguous»
    return env


def git(*args, cwd=None, inp=None, timeout=None, env_extra=None):
    """-> (rc, stdout, stderr). Нет git — rc 127, таймаут — rc 124."""
    cmd = ["git", "--no-optional-locks", "-c", "core.quotepath=off"] + list(args)
    env = git_env()
    env.update(env_extra or {})
    try:
        r = subprocess.run(cmd, cwd=cwd, input=inp, capture_output=True, encoding="utf-8",
                           errors="surrogateescape", env=env, timeout=timeout)
    except FileNotFoundError:
        return 127, "", "git не найден"
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    return r.returncode, r.stdout, r.stderr


class Blobs:
    """Один долгоживущий `git cat-file --batch`: содержимое блобов по oid."""

    def __init__(self):
        self.p = None
        self.cache = {}

    def text(self, oid):
        if oid in self.cache:
            return self.cache[oid]
        if self.p is None:
            self.p = subprocess.Popen(["git", "--no-optional-locks", "cat-file", "--batch"],
                                      stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL, env=git_env())
        self.p.stdin.write(oid.encode() + b"\n")
        self.p.stdin.flush()
        head = self.p.stdout.readline().split()
        txt = None
        if len(head) >= 3 and head[1] == b"blob":
            size = int(head[2])
            data = self.p.stdout.read(size)
            self.p.stdout.read(1)
            txt = data.decode("utf-8", "replace")
        self.cache[oid] = txt
        return txt

    def close(self):
        if self.p is not None:
            try:
                self.p.stdin.close()
                self.p.wait(timeout=5)
            except Exception:
                self.p.kill()
            self.p = None


def tree_index(sha):
    """{nfc-путь: (oid, реальный путь)}, множество каталогов — по дереву коммита."""
    rc, out, _ = git("ls-tree", "-r", "-z", "--full-tree", sha)
    files = {}
    for ent in out.split("\0"):
        if not ent:
            continue
        meta, _, path = ent.partition("\t")
        parts = meta.split(" ")
        if len(parts) >= 3 and parts[1] == "blob":
            files[nfc(path)] = (parts[2], path)
    dirs = set()
    for p in files:
        while "/" in p:
            p = p.rsplit("/", 1)[0]
            dirs.add(p)
    return files, dirs


def name_status(a, b):
    """git diff -z --name-status -M a b -> {nfc-путь: (статус, переименован_в|None)}."""
    rc, out, _ = git("diff", "-z", "--name-status", "-M", a, b)
    parts = out.split("\0")
    res = {}
    i = 0
    while i < len(parts):
        st = parts[i]
        if not st:
            i += 1
            continue
        if st[0] in "RC" and i + 2 < len(parts):
            old, new = parts[i + 1], parts[i + 2]
            i += 3
            res[nfc(old)] = ("R", nfc(new)) if st[0] == "R" else res.get(nfc(old), ("C", None))
            if st[0] == "C":
                res[nfc(new)] = ("A", None)
        elif i + 1 < len(parts):
            res[nfc(parts[i + 1])] = (st[0], None)
            i += 2
        else:
            break
    return res


def repo_info():
    rc, out, _ = git("rev-parse", "--show-toplevel")
    info = {"git": "absent", "root": os.getcwd(), "main_worktree": os.getcwd(), "worktree": "main",
            "shallow": False, "head": None, "branch": None}
    if rc != 0:
        return info
    info["git"] = "ok"
    info["root"] = out.strip()
    rc, gd, _ = git("rev-parse", "--absolute-git-dir")
    rc2, cd, _ = git("rev-parse", "--git-common-dir")
    gd = gd.strip()
    cd = cd.strip()
    if cd and not os.path.isabs(cd):
        cd = os.path.normpath(os.path.join(info["root"], cd))
    if cd and os.path.basename(cd) == ".git":
        info["main_worktree"] = os.path.dirname(cd)
    else:
        info["main_worktree"] = info["root"]
    info["worktree"] = "linked" if gd and cd and os.path.realpath(gd) != os.path.realpath(cd) else "main"
    info["shallow"] = git("rev-parse", "--is-shallow-repository")[1].strip() == "true"
    rc, head, _ = git("rev-parse", "--verify", "--quiet", "HEAD")
    info["head"] = head.strip() or None
    rc, br, _ = git("symbolic-ref", "-q", "--short", "HEAD")
    info["branch"] = br.strip() or None
    return info


def frame(command, complete, errors, **fields):
    d = {"schema": SCHEMA, "command": command, "scan_complete": complete, "errors": errors}
    d.update(fields)
    return d


def err(code, message, fix):
    return {"code": code, "message": message, "fix": fix}


# ---------------------------------------------------------------- разбор markdown

FM_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.S)
CHECKED_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}) @ ([0-9a-f]{7,40})\b")
CLAIM_RE = re.compile(r"^\s*(?:[-*+]\s+\S|\d+[.)]\s+\S|\|)")
TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{2,}[\s:|-]*$")
TOKEN_RE = re.compile(r"`([^`\n]+)`")
SYM_RE = re.compile(r"^[\w.:@/$*\[\]-]+$")
OPEN_RE = re.compile(r"(?<![\w])open:")
MARK_RE = re.compile(r"(?<![\w])(?:policy|claimed):")


def split_fm(text):
    m = FM_RE.match(text)
    if not m:
        return None, text
    return m.group(1), text[m.end():]


def fm_keys(fm):
    keys = {}
    for line in (fm or "").split("\n"):
        m = re.match(r"^\s*([A-Za-z_][\w-]*)\s*:\s*(.*?)\s*$", line)
        if m and m.group(1) not in keys:
            keys[m.group(1)] = m.group(2).strip("'\"")
    return keys


def parse_checked(v):
    m = CHECKED_RE.match(v or "")
    return (m.group(1), m.group(2)) if m else None


def content_lines(text):
    """[(номер строки в файле, строка)] тела без frontmatter и без fenced-кода."""
    lines = text.split("\n")
    m = FM_RE.match(text)
    start = text.count("\n", 0, m.end()) if m else 0
    out, fence = [], False
    for i in range(start, len(lines)):
        ln = lines[i].rstrip("\r")
        if re.match(r"\s*(```|~~~)", ln):
            fence = not fence
            continue
        if not fence:
            out.append((i + 1, ln))
    return out


def is_claim(line):
    return bool(CLAIM_RE.match(line)) and not TABLE_SEP_RE.match(line)


def is_path_like(p):
    if not p or re.search(r"\s", p) or "://" in p or p.startswith(("~", "/", "mailto:")):
        return False
    base = p.rsplit("/", 1)[-1]
    return ("/" in p or bool(re.search(r"\.[A-Za-z0-9]+$", p))
            or bool(re.match(r"(Dockerfile|Makefile|Procfile)", base)))


def line_anchors(line):
    """Backtick-токены `<путь>#<символ>` строки -> ([(путь, символ)], число нераспознанных)."""
    found, unrec = [], 0
    for tok in TOKEN_RE.findall(line):
        if "#" not in tok:
            continue
        path, sym = tok.split("#", 1)
        if not path or not sym or not SYM_RE.match(sym) or re.search(r"\s", path):
            continue
        if is_path_like(path):
            found.append((path[2:] if path.startswith("./") else path, sym))
        else:
            unrec += 1
    return found, unrec


def sym_present(path, sym, text):
    """Символ по границам слова; `A.b` — по сегментам; .json — обход ключей."""
    segs = [s for s in sym.split(".") if s]
    if path.endswith(".json"):
        try:
            return json_has(json.loads(text), segs)
        except ValueError:
            pass
    return all(re.search(r"(?<![\w$])" + re.escape(s) + r"(?![\w$])", text) for s in segs)


def json_has(o, segs):
    # ponytail: потомок с нужным ключом на любой глубине принимается — строгий путь от корня
    # стал бы ложным symbol_missing на вложенных конфигах; ужесточать, если пойдут ложные ok.
    if not segs:
        return True
    if isinstance(o, dict):
        for k, v in o.items():
            if (segs[0] == "*" or k == segs[0]) and json_has(v, segs[1:]):
                return True
        return any(json_has(v, segs) for v in o.values())
    if isinstance(o, list):
        return any(json_has(v, segs) for v in o)
    return False


def md_walk(top):
    out = []
    for dp, dns, fns in os.walk(top):
        dns.sort()
        for fn in sorted(fns):
            if fn.endswith(".md"):
                out.append(os.path.join(dp, fn).replace(os.sep, "/"))
    return out


def load_memory():
    files = []
    for rel in md_walk("memory"):
        text = read(rel) or ""
        fm, body = split_fm(text)
        keys = fm_keys(fm) if fm is not None else {}
        status = keys.get("status", "")
        files.append({
            "rel": nfc(rel), "path": rel, "text": text, "fm": fm, "keys": keys,
            "words": count_words(body), "checked": parse_checked(keys.get("checked")),
            "journal": rel.startswith("memory/journal/"),
            "entry": rel.count("/") == 1 and status != "archive",
            "archive": status == "archive",
        })
    return files


def repo_age(path):
    """(возраст в днях, git|mtime): по последнему коммиту, для untracked — по mtime."""
    rc, out, _ = git("log", "-1", "--format=%ct", "--", path)
    if rc == 0 and out.strip().isdigit():
        return max(0, (now() - int(out.strip())) // 86400), "git"
    try:
        newest = max(os.path.getmtime(os.path.join(dp, f)) for dp, _, fs in os.walk(path) for f in fs) \
            if os.path.isdir(path) else os.path.getmtime(path)
    except (OSError, ValueError):
        return 0, "mtime"
    return max(0, (now() - int(newest)) // 86400), "mtime"


# ---------------------------------------------------------------- база и sha

def resolve_base(arg, mem):
    """Аргумент > `base:` в шапке memory/MEMORY.md > origin/HEAD > ask_base (main/master не угадываем)."""
    ref, source = None, "none"
    if arg:
        ref, source = arg, "arg"
    else:
        idx = next((f for f in mem if f["rel"] == "memory/MEMORY.md"), None)
        if idx and idx["keys"].get("base"):
            ref, source = idx["keys"]["base"], "memory_base"
        else:
            rc, out, _ = git("symbolic-ref", "-q", "refs/remotes/origin/HEAD")
            if rc == 0 and out.strip():
                ref, source = out.strip().replace("refs/remotes/", "", 1), "origin_head"
    sha = None
    if ref:
        rc, out, _ = git("rev-parse", "--verify", "--quiet", ref + "^{commit}")
        if rc == 0:
            sha = out.strip()
    return {"ref": ref, "sha": sha, "source": source, "ask_base": sha is None,
            "fetch_error": None, "fetch_head_age_s": None}


def base_error(base):
    if base["sha"]:
        return None
    what = f"«{base['ref']}» не резолвится в коммит" if base["ref"] else "база не задана и origin/HEAD не установлен"
    return err("base_unresolved", what,
               "передай --base origin/<ветка> (main/master не угадываются); "
               "ссылка не резолвится — сначала `git fetch origin`")


class Shas:
    """Состояния checked-sha относительно базы: один набор git-вызовов на уникальный sha."""

    def __init__(self, base_sha, shallow):
        self.base, self.shallow = base_sha, shallow
        self.cache, self.diffs, self.errors = {}, {}, []

    def state(self, short):
        if short in self.cache:
            return self.cache[short]
        rc, out, e = git("rev-parse", "--verify", short + "^{commit}")
        if rc != 0:
            st = "ambiguous" if "ambiguous" in e else ("shallow_missing" if self.shallow else "unresolved")
            res = {"state": st, "full": None, "merge_base": None, "behind": None}
        else:
            full = out.strip()
            rc, _, e2 = git("merge-base", "--is-ancestor", full, self.base)
            if rc == 0:
                st = "ok"
            elif rc == 1:
                st = "not_ancestor"
            else:  # код is-ancestor не 0/1 — ошибка, а не «другая ветка»
                st = "unresolved"
                self.errors.append(err("is_ancestor_failed", f"merge-base --is-ancestor {full[:9]}: {e2.strip()[:120]}",
                                       "проверь целостность репозитория: `git fsck`"))
            mb = None
            if st == "not_ancestor":
                mb = git("merge-base", full, self.base)[1].strip() or None
            behind = None
            if st in ("ok", "not_ancestor"):
                b = git("rev-list", "--count", f"{full}..{self.base}")[1].strip()
                behind = int(b) if b.isdigit() else None
            res = {"state": st, "full": full, "merge_base": mb, "behind": behind}
        self.cache[short] = res
        return res

    def by_date(self, date):
        rc, out, _ = git("rev-list", "-1", f"--before={date}T00:00:00Z", self.base)
        return out.strip() or None

    def diff(self, frm):
        if frm not in self.diffs:
            self.diffs[frm] = name_status(frm, self.base)
        return self.diffs[frm]


# ---------------------------------------------------------------- источник файлов для якорей

class Source:
    """Где искать файлы якорей: рабочее дерево или дерево базы."""

    def __init__(self, base=None, blobs=None):
        self.base = base
        self.blobs = blobs
        self.cache = {}
        if base:
            self.files, self.dirs = tree_index(base["sha"])

    def label(self):
        return f"{self.base['ref']}@{self.base['sha'][:9]}" if self.base else "worktree"

    def text(self, path):
        """Содержимое файла; None — файла нет. Второй элемент — worktree_only."""
        key = nfc(path)
        if key in self.cache:
            return self.cache[key]
        res = (None, False)
        if self.base:
            ent = self.files.get(key)
            if ent:
                res = (self.blobs.text(ent[0]) or "", False)
            elif os.path.isfile(path):
                res = (None, True)
        elif os.path.isfile(path):
            res = (read(path) or "", False)
        self.cache[key] = res
        return res

    def is_dir(self, path):
        return nfc(path).rstrip("/") in self.dirs if self.base else os.path.isdir(path)

    def state(self, path, sym):
        text, wt_only = self.text(path)
        if text is None:
            if wt_only:
                return "worktree_only"
            return "file_only" if self.is_dir(path) else "file_missing"
        if path.endswith(".md"):
            return "file_only"
        return "ok" if sym_present(path, sym, text) else "symbol_missing"


# ---------------------------------------------------------------- validate

def paragraphs(text):
    """[(номер первой строки, [строки])] блоков из подряд идущих непустых строк."""
    out, cur, start = [], [], 0
    for i, ln in enumerate(text.split("\n"), 1):
        if ln.strip():
            if not cur:
                start = i
            cur.append(ln)
        elif cur:
            out.append((start, cur))
            cur = []
    if cur:
        out.append((start, cur))
    return out


def rs_tokens(rel, text):
    """Backtick-токены read-on-spawn: (строка, токен, текст строки).
    Агент/rules — абзацы со словами read-on-spawn; charter — плюс ячейки колонки «Read on spawn»."""
    res = []
    for start, blk in paragraphs(text):
        joined = "\n".join(blk)
        if re.search(r"read[- ]on[- ]spawn", joined, re.I) and not blk[0].lstrip().startswith("|"):
            for k, ln in enumerate(blk):
                for tok in TOKEN_RE.findall(ln):
                    res.append((start + k, tok, ln))
    table_col = None
    for i, ln in enumerate(text.split("\n"), 1):
        if not ln.lstrip().startswith("|"):
            table_col = None
            continue
        cells = [c.strip() for c in ln.strip().strip("|").split("|")]
        if any(re.fullmatch(r"read[- ]on[- ]spawn", c, re.I) for c in cells):
            table_col = next(j for j, c in enumerate(cells) if re.fullmatch(r"read[- ]on[- ]spawn", c, re.I))
        elif table_col is not None and table_col < len(cells):
            for tok in TOKEN_RE.findall(cells[table_col]):
                res.append((i, tok, ln))
    return res


def rs_target(tok):
    """Токен read-on-spawn -> путь файла или None (не файл памяти/проекта)."""
    t = tok.split("§")[0].split("#")[0].strip()
    if not t.endswith(".md") or re.search(r"[*<>{}\s]", t):
        return None
    return t


def rs_exists(t):
    return os.path.isfile(t) or os.path.isfile("memory/" + t) or os.path.isfile(".claude/" + t)


def rs_resolve(t):
    for cand in (t, "memory/" + t, ".claude/" + t):
        if os.path.isfile(cand):
            return cand
    return None


SAFE_GIT = {"grep", "log", "show", "ls-files", "cat-file", "diff"}
KNOWN_CMDS = {"rg", "grep", "git", "ls", "cat", "head", "tail", "wc", "test", "find", "sed", "awk", "npm", "npx",
              "yarn", "pnpm", "node", "python", "python3", "pip", "curl", "wget", "docker", "kubectl", "helm",
              "psql", "make", "bash", "sh", "sudo", "rm", "mv", "cp", "ssh", "jq", "xargs", "tee", "kill", "echo"}


# Флаги, превращающие «читающую» команду в исполняющую или пишущую (rg --pre запускает программу,
# git diff --output пишет файл, git grep -O открывает пейджер, --ext-diff/--textconv — внешние драйверы).
FORBIDDEN_FLAGS = ("--pre", "--pre-glob", "--output", "--open-files-in-pager", "--ext-diff", "--textconv",
                   "--search-zip", "--exec", "--exec-path", "--upload-pack", "--hostname-bin")
SEP_TOKENS = {";", "|", "||", "&&", "&"}


def command_unsafe(cmd):
    """None — не похоже на команду; False — read-only allowlist; True — вне allowlist.
    ponytail: проверка лексическая (shlex с пунктуацией) — не шелл; всё неразобранное считаем небезопасным."""
    try:
        lex = shlex.shlex(re.sub(r"<[\w./:-]+>", "X", cmd), posix=True, punctuation_chars=True)  # <файл> — заглушка, не редирект
        lex.whitespace_split = True
        toks = list(lex)
    except ValueError:
        return True
    if not toks:
        return None
    if toks[0] not in KNOWN_CMDS:
        # неизвестное начало похоже на команду, только если после разделителя идёт известная команда (`foo; rm x`)
        heads, prev = [], True
        for t in toks:
            if prev and t not in SEP_TOKENS:
                heads.append(t)
            prev = t in SEP_TOKENS
        chained = any(t in SEP_TOKENS for t in toks) and any(h in KNOWN_CMDS for h in heads)
        return True if (chained or any("$(" in t or "`" in t for t in toks) and any(h in KNOWN_CMDS for h in heads)) else None
    if len(toks) < 2:
        return None
    if any("$(" in t or "`" in t for t in toks):
        return True
    seg, segs = [], []
    for t in toks:
        if t in SEP_TOKENS:
            segs.append(seg)
            seg = []
        elif t and set(t) <= set("<>()"):
            return True  # редирект, подстановка процесса, подшелл
        else:
            seg.append(t)
    segs.append(seg)
    for sg in segs:
        if not sg:
            return True
        h = sg[0]
        for t in sg[1:]:
            name = t.split("=", 1)[0]
            # git принимает однозначные сокращения длинных опций (`--outp=x`, `--open-files-in-pa=rm`)
            if t.startswith("-O") or (name.startswith("--") and len(name) >= 3 and any(f.startswith(name) for f in FORBIDDEN_FLAGS)):
                return True
            if h == "rg" and re.fullmatch(r"-[A-Za-z]*z[A-Za-z]*", t):
                return True  # rg -z = --search-zip: запускает внешние распаковщики
        if h in ("rg", "grep", "ls", "wc"):
            continue
        if h == "git" and len(sg) > 1 and sg[1] in SAFE_GIT:
            continue
        if h == "test" and len(sg) > 1 and sg[1] == "-e":
            continue
        return True
    return False


def consumer_files():
    """Матрица потребителей памяти: (путь, вид claude|codex)."""
    res = []
    if os.path.isfile(".claude/team-charter.md"):
        res.append((".claude/team-charter.md", "claude"))
    for d in (".claude/agents", ".claude/rules"):
        if os.path.isdir(d):
            res += [(f"{d}/{n}", "claude") for n in sorted(os.listdir(d)) if n.endswith(".md")]
    cm = read("CLAUDE.md")
    if cm:
        for m in re.finditer(r"^@(\S+)", cm, re.M):
            p = m.group(1)
            if not p.startswith(("~", "/")) and os.path.isfile(p) and (p, "claude") not in res:
                res.append((p, "claude"))
    if os.path.isdir(".codex"):
        for dp, dns, fns in os.walk(".codex"):
            dns.sort()
            res += [(os.path.join(dp, n).replace(os.sep, "/"), "codex") for n in sorted(fns)
                    if n.endswith((".md", ".toml"))]
    if os.path.isfile("AGENTS.md"):
        res.append(("AGENTS.md", "codex"))
    return res


def config_dir():
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")


def canon_score(text):
    marks = [r"checked:", r"journal/", r"якор", r"#Symbol|#Символ", r"Дописал", r"open:.*fixed:|fixed:.*open:"]
    return sum(1 for m in marks if re.search(m, text, re.S))


def run_validate(src, mem):
    problems, counts = [], {"total": 0, "ok": 0, "symbol_missing": 0, "file_missing": 0,
                            "worktree_only": 0, "file_only": 0, "unrecognized": 0}
    seen = {}
    for f in mem:
        rel, text = f["rel"], f["text"]
        stem = os.path.basename(rel)[:-3]
        if f["fm"] is None:
            problems.append({"file": rel, "line": 1, "code": "no_frontmatter",
                             "detail": "нет frontmatter (--- … ---) в начале файла"})
        else:
            for k in REQUIRED_KEYS:
                if k not in f["keys"] and not (k == "checked" and rel == EPIC_STATE):  # рабочий файл эпика — без checked
                    problems.append({"file": rel, "line": 1, "code": "frontmatter_key_missing", "detail": k})
            if "verified-at" in f["fm"]:
                problems.append({"file": rel, "line": 1, "code": "verified_at", "detail": "verified-at вместо checked"})
            if "checked" in f["keys"] and not f["checked"]:
                problems.append({"file": rel, "line": 1, "code": "checked_format",
                                 "detail": f"«{f['keys']['checked'][:40]}» — нужно `ГГГГ-ММ-ДД @ <sha 7–40 hex>`"})
            if f["keys"].get("name") and f["keys"]["name"] != stem:
                problems.append({"file": rel, "line": 1, "code": "name_mismatch",
                                 "detail": f"name: {f['keys']['name']} не совпадает с именем файла {stem}"})
        lines = content_lines(text)
        navs = [n for n, ln in lines if ln.startswith(NAV_MARK)]
        if len(navs) != 1:
            problems.append({"file": rel, "line": navs[0] if navs else 1, "code": "navigator_count",
                             "detail": f"строк-навигаторов {len(navs)} (нужна ровно одна: «{NAV_MARK} …»)"})
        else:
            nxt = dict(lines).get(navs[0] + 1, "")
            if nxt.startswith(">"):
                problems.append({"file": rel, "line": navs[0], "code": "navigator_split",
                                 "detail": "навигатор разбит на несколько строк"})
        for n, ln in lines:
            for m in re.finditer(r"\[(verified|claimed|policy|stale|fixed)[^\]]*\]", ln):
                problems.append({"file": rel, "line": n, "code": "old_label", "detail": m.group(0)[:32]})
            for tok in TOKEN_RE.findall(ln):
                if re.fullmatch(r"[\w./-]+\.(?:" + LINEREF_EXT + r"):\d+(?:[-–]\d+)?", tok):
                    problems.append({"file": rel, "line": n, "code": "line_ref",
                                     "detail": f"номер строки в ссылке: {tok} — перейди на path#Symbol"})
            for m in re.finditer(r"\]\(([^)\s]+)\)", TOKEN_RE.sub("", ln)):
                tgt = m.group(1)
                if tgt.startswith(("http://", "https://", "mailto:", "#")):
                    continue
                p = tgt.split("#")[0]
                if p and not os.path.exists(os.path.join(os.path.dirname(f["path"]), p)):
                    problems.append({"file": rel, "line": n, "code": "broken_link", "detail": tgt})
            if f["journal"]:
                continue  # якоря журнала устаревают законно — в проверку не входят
            anchors, unrec = line_anchors(ln)
            counts["unrecognized"] += unrec
            for path, sym in anchors:
                counts["total"] += 1
                st = seen.get((path, sym))
                if st is None:
                    st = seen[(path, sym)] = src.state(path, sym)
                counts[st] += 1
                if st in ("file_missing", "symbol_missing"):
                    problems.append({"file": rel, "line": n, "code": "anchor_" + st, "detail": f"{path}#{sym}"})
    return problems, counts


def charter_role_tokens(charter):
    """{роль: [токены]} из колонки «Read on spawn» таблицы charter."""
    res = {}
    lines = charter.split("\n")
    col = None
    for ln in lines:
        if not ln.lstrip().startswith("|"):
            col = None
            continue
        cells = [c.strip() for c in ln.strip().strip("|").split("|")]
        hit = [j for j, c in enumerate(cells) if re.fullmatch(r"read[- ]on[- ]spawn", c, re.I)]
        if hit:
            col = hit[0]
        elif col is not None and col < len(cells):
            role = re.sub(r"[*`]", "", cells[0]).strip()
            res.setdefault(role, []).extend(TOKEN_RE.findall(cells[col]))
    return res


def compute_role_budgets():
    charter = read(".claude/team-charter.md") or ""
    ct = charter_role_tokens(charter)
    out = []
    agents_dir = ".claude/agents"
    if not os.path.isdir(agents_dir):
        return out
    for n in sorted(os.listdir(agents_dir)):
        if not n.endswith(".md"):
            continue
        text = read(f"{agents_dir}/{n}") or ""
        name = fm_keys(split_fm(text)[0]).get("name") or n[:-3]
        role = re.sub(r"-agent$", "", name)
        toks = [t for _, t, _ in rs_tokens(n, text)] + ct.get(role, [])
        files = {}
        for t in toks:
            tgt = rs_target(t)
            path = rs_resolve(tgt) if tgt else None
            if path and path not in files:
                files[path] = count_words(split_fm(read(path) or "")[1])
        total = sum(files.values())
        top = sorted(files.items(), key=lambda kv: -kv[1])[:3]
        out.append({"role": role, "words": total, "limit": CFG["role_words"],
                    "over": total > CFG["role_words"], "top": [{"file": k, "words": v} for k, v in top]})
    return out


EXPECT_RE = re.compile(r"создаётся|создается|если создан|по требованию")


def token_neighbourhood(ln, tok):
    """Текст «рядом» с токеном: до следующего токена и (без запятой) после предыдущего."""
    pos = ln.find("`" + tok + "`")
    if pos < 0:
        return ""
    end = pos + len(tok) + 2
    nxt = ln.find("`", end)
    after = ln[end:nxt] if nxt != -1 else ln[end:]
    prev = ln.rfind("`", 0, pos)
    before = ln[prev + 1:pos] if prev != -1 else ln[:pos]
    return after + (" " + before if "," not in before else "")


def dead_and_foreign():
    dead, foreign = [], []
    for path, kind in consumer_files():
        text = read(path) or ""
        if kind == "claude":
            for ln_no, tok, ln in rs_tokens(path, text):
                tgt = rs_target(tok)
                if tgt and not rs_exists(tgt):
                    item = {"path": tgt, "mentioned_in": f"{path}:{ln_no}",
                            "expected_absent": bool(EXPECT_RE.search(token_neighbourhood(ln, tok)))}
                    if item not in dead:
                        dead.append(item)
        else:
            for i, ln in enumerate(text.split("\n"), 1):
                for m in re.finditer(r"memory/(?:[\w.-]+/)*[\w.-]*", ln):
                    p = m.group(0).rstrip(".")
                    if p in ("memory", "memory/"):
                        continue
                    h = re.match(r"`?\s*(?:§|#)\s*([^`|;·,)\n]+)", ln[m.end():])
                    item = {"path": p, "heading": h.group(1).strip().rstrip(".") if h else None,
                            "mentioned_in": f"{path}:{i}"}
                    if item not in foreign:
                        foreign.append(item)
    return dead, foreign


def do_validate(mem, base, blobs):
    """-> (поля payload, есть ли замечания). base=None — якоря по рабочему дереву."""
    src = Source(base, blobs)
    problems, counts = run_validate(src, mem)
    entry_words = sum(f["words"] for f in mem if f["entry"])
    jfiles = []
    for f in mem:
        if f["journal"]:
            age, how = repo_age(f["path"])
            jfiles.append({"file": f["rel"], "words": f["words"], "age_days": age, "age_source": how,
                           "has_open": bool(OPEN_RE.search(f["text"]))})
    jw = sum(x["words"] for x in jfiles)
    budgets = {
        "entry": sorted(({"file": f["rel"], "words": f["words"], "limit": CFG["entry_words"],
                          "over": f["words"] > CFG["entry_words"]} for f in mem if f["entry"]),
                        key=lambda x: -x["words"]),
        "journal": {"words": jw, "limit": int(CFG["journal_ratio"] * entry_words),
                    "over": jw > int(CFG["journal_ratio"] * entry_words), "files": jfiles},
        "roles": compute_role_budgets(),
    }
    dead, foreign = dead_and_foreign()
    canon = []
    for d in (".claude/rules", os.path.join(config_dir(), "rules")):
        if os.path.isdir(d):
            for n in sorted(os.listdir(d)):
                t = read(os.path.join(d, n)) if n.endswith(".md") else None
                if t and count_words(t) >= 200 and canon_score(t) >= 3:
                    canon.append({"path": os.path.join(d, n)})
    unsafe = []
    for f in mem:
        if f["journal"]:
            continue
        for n, ln in content_lines(f["text"]):
            if not line_anchors(ln)[0]:
                continue
            for tok in TOKEN_RE.findall(ln):
                if "#" not in tok and command_unsafe(tok) is True:
                    unsafe.append({"file": f["rel"], "line": n, "command": tok[:200]})
    fields = {"anchors_source": src.label(), "problems": problems, "anchors": counts, "budgets": budgets,
              "consumers": {"dead": dead, "foreign_contracts": foreign},
              "canon_copies": canon, "unsafe_commands": unsafe}
    issues = (bool(problems) or any(x["over"] for x in budgets["entry"]) or budgets["journal"]["over"]
              or any(r["over"] for r in budgets["roles"]) or any(not d["expected_absent"] for d in dead)
              or bool(canon) or bool(unsafe))
    return fields, issues


def no_memory_error():
    return err("no_memory_files", "в memory/ нет ни одного .md (или каталога нет)",
               "запусти из корня репозитория; память без файлов проверять нечего")


def cmd_validate(a):
    mem = load_memory()
    if not mem:
        return 2, frame("validate", False, [no_memory_error()], anchors_source=None, problems=[])
    base, blobs = None, Blobs()
    try:
        if a.base:
            info = repo_info()
            base = resolve_base(a.base, mem)
            e = base_error(base) if info["git"] == "ok" else err("git_absent", "не git-репозиторий", "запусти в репозитории")
            if e:
                return 2, frame("validate", False, [e], anchors_source=None, problems=[])
        fields, issues = do_validate(mem, base, blobs)
    finally:
        blobs.close()
    return (1 if issues else 0), frame("validate", True, [], constants=constants_view(), **fields)


def constants_view():
    return dict(CFG)


# ---------------------------------------------------------------- delta

STATE_RANK = {"intact": 0, "branch_only": 1, "modified": 2, "symbol_missing": 3, "renamed": 4, "deleted": 5}
STALE_STATES = ("modified", "symbol_missing", "renamed", "deleted")


def anchor_state(path, sym, diff, tree, blobs, head_files, mb_files=None):
    """Состояние якоря относительно диффа checked-sha..база: (состояние, renamed_to).
    branch_only — пути нет в базе, но он есть в HEAD/рабочем дереве: факт с другой ветки, «не проверено»."""
    files, dirs = tree
    key = nfc(path)
    ch = diff.get(key)
    if ch is None:
        if key in files:
            return "intact", None
        if key.rstrip("/") in dirs:
            under = any(k.startswith(key.rstrip("/") + "/") for k in diff)
            return ("modified" if under else "intact"), None
        if key in head_files or os.path.exists(path):  # ветка впереди базы либо файл вне git (node_modules)
            return "branch_only", None
        return "deleted", None  # пути нет ни в базе, ни в HEAD, ни на диске: битый ещё на момент checked
    st, new = ch
    if st == "D":
        # sha не предок базы: путь есть в дереве checked-sha, но нет в базе. Был и в merge-base — удалён в базе;
        # не было — добавлен на ветке памяти и в базу не вливался: факт с другой ветки, а не удаление.
        if mb_files is not None and key not in mb_files:
            return "branch_only", None
        return "deleted", None
    if st == "R":
        return "renamed", new
    if mb_files is not None and key in files and key in mb_files and mb_files[key][0] == files[key][0]:
        return "branch_only", None  # база файл с развилки не трогала: вся разница — правки ветки памяти
    if key in files and not path.endswith(".md"):
        text = blobs.text(files[key][0]) or ""
        if not sym_present(path, sym, text):
            return "symbol_missing", None
    return "modified", None


def do_delta(mem, base, info, blobs):
    shas = Shas(base["sha"], info["shallow"])
    tree = tree_index(base["sha"])
    head_files = tree_index(info["head"])[0] if info["head"] and info["head"] != base["sha"] else {}
    mb_cache = {}
    files_out, stale_claims_total, stale_file_claims_total, sample_pool = [], 0, 0, []
    for f in mem:
        if f["journal"] or f["archive"] or f["rel"] == EPIC_STATE:  # epic-state — рабочий файл эпика, не факты
            continue
        lines = content_lines(f["text"])
        claim_nos = {n for n, ln in lines if is_claim(ln)}
        anchors = []  # (строка, путь, символ)
        for n, ln in lines:
            anchors += [(n, p, s) for p, s in line_anchors(ln)[0]]
        counts = {"total": len(claim_nos), "intact": 0, "modified": 0, "deleted": 0, "renamed": 0, "symbol_missing": 0,
                  "branch_only": 0}
        rec = {"file": f["rel"], "checked_sha": None, "checked_date": None, "sha_state": "none", "merge_base": None,
               "commits_behind": None, "stale": False, "reason": None, "freshness": "exact",
               "claims": counts, "stale_anchors": [], "unverified_anchors": []}
        frm = None
        if f["checked"] is None:
            rec["reason"] = "no_checked"
        else:
            date, short = f["checked"]
            rec["checked_date"], rec["checked_sha"] = date, short
            st = shas.state(short)
            rec["sha_state"], rec["merge_base"], rec["commits_behind"] = st["state"], st["merge_base"], st["behind"]
            if st["state"] in ("ok", "not_ancestor"):
                frm = st["full"]
            else:
                rec["freshness"], rec["reason"] = "by_date", "sha_unresolved"
                frm = shas.by_date(date)
        if frm:
            diff = shas.diff(frm)
            worst = {}
            mb_files = None
            if rec["sha_state"] == "not_ancestor":
                mb = rec["merge_base"]
                if mb not in mb_cache:
                    mb_cache[mb] = tree_index(mb)[0] if mb else {}
                mb_files = mb_cache[mb]
            for n, p, s in anchors:
                state, to = anchor_state(p, s, diff, tree, blobs, head_files, mb_files)
                if state == "branch_only":
                    rec["unverified_anchors"].append({"line": n, "ref": f"{p}#{s}", "state": state})
                elif state != "intact":
                    rec["stale_anchors"].append({"line": n, "ref": f"{p}#{s}", "state": state, "renamed_to": to})
                if STATE_RANK[state] >= STATE_RANK[worst.get(n, "intact")]:
                    worst[n] = state
            for n, state in worst.items():
                if n in claim_nos:
                    counts[state] += 1
            if rec["stale_anchors"] and rec["reason"] is None:
                rec["reason"] = "anchor_changed"
        # Нет якорей и не целиком policy:/claimed: -> устарел целиком. Не считаем для файлов без утверждений,
        # для genre index (ссылки на файлы — их проверяет validate: битые ссылки) и genre decision (реестр
        # указателей и истории, а не факты о коде — указатели проверяет validate).
        if rec["reason"] is None and not anchors and f["keys"].get("genre") not in ("index", "decision"):
            marked = [n for n, ln in lines if is_claim(ln) and MARK_RE.search(ln)]
            if claim_nos and len(marked) < len(claim_nos):
                rec["reason"] = "no_anchors"
        rec["stale"] = rec["reason"] is not None
        stale_claims_total += sum(counts[k] for k in STALE_STATES)
        if rec["stale"]:
            stale_file_claims_total += counts["total"]  # единица проверки — файл целиком: n = все его утверждения
        files_out.append(rec)
        if f["entry"] and not rec["stale"]:
            for n, ln in lines:
                if is_claim(ln) and len(ln.strip()) >= 20:
                    key = hashlib.sha256(f"{base['sha']}:{f['rel']}:{n}".encode("utf-8", "replace")).hexdigest()
                    sample_pool.append((key, {"file": f["rel"], "line": n, "text": ln.strip()[:300]}))
    sample = [x for _, x in sorted(sample_pool, key=lambda kv: kv[0])[:CFG["sample"]]]
    fields = {"base": {"ref": base["ref"], "sha": base["sha"], "source": base["source"]},
              "ask_base": any(r["sha_state"] not in ("ok", "none") for r in files_out),
              "files_total": len(files_out), "stale_claims_total": stale_claims_total,
              "stale_file_claims_total": stale_file_claims_total, "files": files_out, "sample": sample}
    return fields, shas.errors


# ---------------------------------------------------------------- features

STATUS_RE = re.compile(r"^\s*(?:[-*>]\s*)?(?:\*\*)?(?:Status|Статус)(?:\*\*)?\s*:\s*(?:\*\*)?\s*(.*?)\s*$", re.I)
TICKET_RE = re.compile(r"(?<![A-Za-z0-9])[A-Z][A-Z0-9]+-\d+(?![0-9])")


def artifact_status(text):
    for ln in text.split("\n")[:15]:
        m = STATUS_RE.match(ln)
        if m:
            val = m.group(1).strip("*` ")
            for canon in STATUS_VALUES:
                if re.match(re.escape(canon) + r"\b", val, re.I):
                    return canon
            return "nonstandard:" + val[:40]
    return "none"


def do_features(base, info):
    root = "docs/features"
    if not os.path.isdir(root):
        return {"present": False, "items": []}
    rc, out, _ = git("log", "-z", "--format=%H%x1f%s%x1f%b", base["sha"])
    tickets = {}  # тикет -> [число коммитов, sha последнего]
    for rec in out.split("\0"):
        parts = rec.strip("\n").split("\x1f", 2)
        if len(parts) < 3:
            continue
        for t in set(TICKET_RE.findall(parts[1] + "\n" + parts[2])):
            e = tickets.setdefault(t, [0, parts[0].strip()])
            e[0] += 1
    rc, out, _ = git("for-each-ref", f"--no-merged={base['sha']}", "--format=%(refname:short)", "refs/heads", "refs/remotes")
    unmerged_refs = [r for r in out.split("\n") if r and not r.endswith("/HEAD")]
    items = []
    for slug in sorted(os.listdir(root)):
        sd = os.path.join(root, slug)
        if not os.path.isdir(sd):
            continue
        arts, service, extra = {}, [], []
        for dp, dns, fns in os.walk(sd):
            dns.sort()
            for fn in sorted(fns):
                full = os.path.join(dp, fn)
                rel = nfc(os.path.relpath(full, sd)).replace(os.sep, "/")
                size = os.path.getsize(full) if os.path.isfile(full) else 0
                if any(seg.startswith(".") or seg == "consult" for seg in rel.split("/")):
                    service.append({"path": rel, "bytes": size})
                elif rel in CANON_ARTIFACTS:
                    arts[rel] = {"status": artifact_status(read(full) or "")}
                else:
                    extra.append({"path": rel, "bytes": size})
        plan_doc = "plan.md" if "plan.md" in arts else ("feature-brief.md" if "feature-brief.md" in arts else None)
        ticket, tsrc = None, "none"
        for fn in CANON_ARTIFACTS:
            m = re.search(r"^\s*(?:\*\*)?Ticket(?:\*\*)?\s*:\s*(?:\*\*)?\s*([A-Z][A-Z0-9]+-\d+)",
                          "\n".join((read(os.path.join(sd, fn)) or "").split("\n")[:15]), re.M)
            if m:
                ticket, tsrc = m.group(1), "header"
                break
        if not ticket:
            m = TICKET_RE.search(slug)
            if m:
                ticket, tsrc = m.group(0), "slug"
        # slug с числовым суффиксом (`<slug>-2`) наследует тикет исходной фичи: «влита» про него ничего не говорит
        if ticket and re.search(r"-\d+$", TICKET_RE.sub("", slug)):
            tsrc = "slug_ambiguous"
        merge = {"state": "unknown", "base_commits": 0, "last_sha": None, "unmerged_branches": []}
        if ticket and tsrc != "slug_ambiguous":
            cnt, last = tickets.get(ticket, [0, None])
            pat = re.compile(r"(?<![A-Za-z0-9])" + re.escape(ticket) + r"(?![0-9])")
            merge.update(base_commits=cnt, last_sha=last if cnt else None,
                         unmerged_branches=[r for r in unmerged_refs if pat.search(r)])
            done = plan_doc is not None and arts[plan_doc]["status"] == "Done"
            if merge["unmerged_branches"] or cnt == 0:
                merge["state"] = "unmerged"
            elif done:
                merge["state"] = "merged"  # плановый документ Done + тикет в базе + нет невлитой ветки
        age, how = repo_age(sd)
        items.append({"slug": nfc(slug), "ticket": ticket, "ticket_source": tsrc, "artifacts": arts,
                      "plan_doc": plan_doc, "merge": merge, "service": service, "extra": extra,
                      "age_days": age, "age_source": how})
    return {"present": True, "items": items}


# ---------------------------------------------------------------- auto-memory

def b36(n):
    d, out = "0123456789abcdefghijklmnopqrstuvwxyz", ""
    while True:
        n, r = divmod(n, 36)
        out = d[r] + out
        if n == 0:
            return out


def sanitize_path(p):
    """Экранирование пути проекта, как в рантайме: всё вне [A-Za-z0-9] -> '-', длинные — хэш."""
    b = nfc(p).encode("utf-16-le", "surrogatepass")
    units = [b[i] | (b[i + 1] << 8) for i in range(0, len(b), 2)]
    s = "".join(chr(u) if (48 <= u <= 57 or 65 <= u <= 90 or 97 <= u <= 122) else "-" for u in units)
    if len(s) <= 200:
        return s
    h = 0
    for u in units:
        h = (h * 31 + u) & 0xFFFFFFFF
    if h >= 0x80000000:
        h -= 0x100000000
    return f"{s[:200]}-{b36(abs(h))}"


def override_configured():
    if os.environ.get("CLAUDE_COWORK_MEMORY_PATH_OVERRIDE"):
        return True
    for p in (os.path.join(config_dir(), "settings.json"), ".claude/settings.json", ".claude/settings.local.json"):
        t = read(p)
        if t and "autoMemoryDirectory" in t:
            return True
    return False


def derived_automemory_dir(info):
    return os.path.join(config_dir(), "projects", sanitize_path(info["main_worktree"]), "memory")


ID_PATTERNS = (
    re.compile(r"(?<![A-Za-z0-9])[A-Z][A-Z0-9]{1,9}-\d+(?![0-9])"),
    re.compile(r"(?<![\w!])![0-9]+\b"),
    re.compile(r"(?<![0-9A-Za-z])(?=[0-9a-f]*\d)[0-9a-f]{7,40}(?![0-9A-Za-z])"),
    re.compile(r"(?<![\w./-])(?:[\w.-]+/)+[\w.-]+\.\w+"),
)


def identifiers(text):
    ids = set()
    for pat in ID_PATTERNS:
        ids.update(m.group(0) for m in pat.finditer(text))
    ids.update(t.strip() for t in TOKEN_RE.findall(text) if len(t.strip()) >= 6)
    return ids


def corpus_texts():
    """Куда автомат-память могла бы переехать: файлы проекта, где живёт то же знание."""
    res = {}
    for p in md_walk("memory"):
        res[p] = read(p) or ""
    if os.path.isdir("docs/features"):
        for p in md_walk("docs/features"):
            if not any(seg.startswith(".") or seg == "consult" for seg in p.split("/")):
                res[p] = read(p) or ""
    for d in (".claude/rules", ".claude/agents"):
        if os.path.isdir(d):
            for p in md_walk(d):
                res[p] = read(p) or ""
    for p in ("CLAUDE.md", ".claude/team-charter.md"):
        if os.path.isfile(p):
            res[p] = read(p) or ""
    return res


def do_automemory(info, dir_arg):
    derived = derived_automemory_dir(info)
    d, dsrc = (dir_arg, "arg") if dir_arg else (derived, "derived")
    related = []
    cur = sanitize_path(info["main_worktree"])
    base_name = sanitize_path(os.path.basename(info["main_worktree"]))
    proj = os.path.join(config_dir(), "projects")
    if os.path.isdir(proj):
        for n in sorted(os.listdir(proj)):
            if n == cur or not os.path.isdir(os.path.join(proj, n, "memory")):
                continue
            if n.endswith("-" + base_name):
                related.append({"dir": os.path.join(proj, n), "relation": "old_path"})
            elif cur.startswith(n + "-"):
                related.append({"dir": os.path.join(proj, n), "relation": "ancestor"})
    out = {"status": "ok", "dir": d, "dir_source": dsrc, "index": None, "files": [], "related_dirs": related}
    if not dir_arg and override_configured():
        out["status"] = "override_unresolved"  # путь переопределён настройками — не угадываем
        return out
    if not os.path.isdir(d):
        out["status"] = "not_found"
        return out
    names = sorted(n for n in os.listdir(d) if n.endswith(".md"))
    idx_text = read(os.path.join(d, "MEMORY.md"))
    if idx_text is not None:
        lines = idx_text.count("\n") + (0 if idx_text.endswith("\n") or not idx_text else 1)
        chars = len(idx_text.encode("utf-16-le", "surrogatepass")) // 2
        broken = sorted({m.group(1) for m in re.finditer(r"\]\(([^)\s#]+\.md)\)", idx_text)
                         if not os.path.exists(os.path.join(d, m.group(1)))})
        out["index"] = {"lines": lines, "chars": chars, "limit_lines": 200, "limit_chars": 25000,
                        "over": lines > 200 or chars > 25000, "broken_links": broken}
    files = [n for n in names if n != "MEMORY.md"]
    if not files:
        out["status"] = "empty"
        return out
    corpus = corpus_texts()
    blob = "\n".join(corpus.values())
    for n in files:
        text = read(os.path.join(d, n)) or ""
        ids = identifiers(text)
        found = [i for i in ids if i in blob]
        overlap = round(len(found) / len(ids), 3) if ids else 0.0
        if len(ids) < CFG["min_ids"]:
            cls = "insufficient_ids"
        elif overlap >= CFG["dup_overlap"]:
            cls = "dup_candidate"
        elif overlap <= CFG["only_here_overlap"]:
            cls = "only_here"
        else:
            cls = "mixed"
        homes = sorted(corpus, key=lambda p: -sum(1 for i in found if i in corpus[p]))[:3]
        homes = [h for h in homes if any(i in corpus[h] for i in found)]
        out["files"].append({"name": n, "bytes": len(text.encode("utf-8", "surrogateescape")), "ids": len(ids),
                             "id_overlap": overlap, "class": cls, "homes": homes})
    return out


def cmd_automemory(a):
    return 0, frame("automemory", True, [], constants=constants_view(), **do_automemory(repo_info(), a.automemory_dir))


# ---------------------------------------------------------------- lost

LIST_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+\S")
UNIT_MIN = 25  # короче — структура (заголовки, «---»), а не знание


def md_units(text):
    """Единицы сравнения: абзац, пункт списка (с продолжением), строка таблицы, fenced-блок, заголовок."""
    body = split_fm(text)[1]
    units, cur, fence = [], [], False

    def flush():
        if cur:
            units.append("\n".join(cur))
            del cur[:]

    for raw in body.split("\n"):
        ln = raw.rstrip("\r")
        if re.match(r"\s*(```|~~~)", ln):
            if not fence:
                flush()
                cur.append(ln)
            else:
                cur.append(ln)
                flush()
            fence = not fence
            continue
        if fence:
            cur.append(ln)
        elif not ln.strip():
            flush()
        elif ln.lstrip().startswith("|"):
            flush()
            if not TABLE_SEP_RE.match(ln):
                units.append(ln)
        elif LIST_RE.match(ln):
            flush()
            cur.append(ln)
        elif ln.startswith("#"):
            flush()
            units.append(ln)
        else:
            cur.append(ln)  # абзац или ленивое продолжение пункта
    flush()
    return units


def norm_unit(u):
    u = re.sub(r"^\s*(?:[-*+>]\s+|\d+[.)]\s+)", "", u.strip())
    return re.sub(r"\s+", " ", u.replace("**", "").replace("`", "")).casefold()


def snapshot_root(snap):
    """Каталог «до» внутри снимка: <run>/before либо сам переданный каталог."""
    if os.path.isdir(os.path.join(snap, "before")):
        return os.path.join(snap, "before")
    return snap if any(os.path.isdir(os.path.join(snap, d)) for d in ("memory", "docs", "automemory")) else None


def approved(path, expect):
    return any(path == e or path.startswith(e.rstrip("/") + "/") for e in expect)


def ids_split(text):
    """(сильные идентификаторы, слабые): якоря, пути, backtick-токены против тикетов, MR, sha."""
    strong = {f"{p}#{s_}" for p, s_ in line_anchors(text)[0]}
    strong.update(t.strip() for t in TOKEN_RE.findall(text) if len(t.strip()) >= 6)
    weak = {m.group(0) for pat in ID_PATTERNS[:3] for m in pat.finditer(text)}
    return strong, weak


def open_cleared(unit, fixed_units):
    """open: снимается только fixed:-единицей с теми же якорем/идентификаторами (не по одному общему тикету)."""
    strong, weak = ids_split(unit)
    need = strong if strong else (weak if len(weak) >= 2 else None)
    return bool(need) and any(all(i in fu for i in need) for fu in fixed_units)


def cmd_lost(a):
    root = snapshot_root(a.snapshot)
    tree = a.tree
    expect = [nfc(p).rstrip("/") for p in (a.expect_deleted or [])]
    if tree == "automemory":
        src = os.path.join(root, "automemory") if root else None
        cur_dir = a.automemory_dir or derived_automemory_dir(repo_info())
        old = {"automemory/" + os.path.basename(p): p for p in md_walk(src)} if src and os.path.isdir(src) else {}
        cur_files = {"automemory/" + os.path.basename(p): p for p in md_walk(cur_dir)} if os.path.isdir(cur_dir) else {}
    else:
        old, cur_files = {}, {}
        for top in ("memory", "docs/features"):
            base = os.path.join(root, top) if root else None
            if base and os.path.isdir(base):
                old.update({nfc(top + "/" + os.path.relpath(p, base).replace(os.sep, "/")): p for p in md_walk(base)})
            if os.path.isdir(top):
                cur_files.update({nfc(p): p for p in md_walk(top)})
    if not old:
        return 2, frame("lost", False, [err("snapshot_empty", f"в снимке {a.snapshot} нет файлов для дерева {tree}",
                                             "передай каталог прогона (<снимки>/<run>) или его before/")],
                        tree=tree, open_lost=[], candidates=[], growth=None, shrunk_files=[], shrink_alarm=False)
    corpus = corpus_texts()
    # Текущие файлы дерева — в корпусе целиком: corpus_texts() пропускает служебные (.work/, consult/, dot-файлы фич),
    # и неизменённый служебный файл иначе выглядел бы потерей всех своих единиц.
    corpus.update({k: read(v) or "" for k, v in cur_files.items()})
    found, found_nonjournal, fixed_units = set(), set(), []
    for path, text in corpus.items():
        for u in md_units(text):
            n = norm_unit(u)
            found.add(n)
            if not path.startswith("memory/journal/"):
                found_nonjournal.add(n)
            if re.search(r"(?<![\w])fixed:", u):
                fixed_units.append(u)
    open_lost, cands, shrunk = [], [], []
    wb = wa = 0
    for rel, path in sorted(old.items()):
        text = read(path) or ""
        gone = rel not in cur_files
        waived = gone and approved(rel, expect)
        if waived and tree != "automemory":
            continue  # одобренное удаление файла проекта: содержимое ушло по решению пользователя
        # auto-memory: одобренное удаление гасит только тревогу сжатия — перенос единиц текста проверяется всё равно
        for u in md_units(text):
            n = norm_unit(u)
            if len(n) < UNIT_MIN:
                continue
            is_open = bool(OPEN_RE.search(u))
            if n in (found_nonjournal if is_open else found):
                continue
            if is_open:
                if open_cleared(u, fixed_units):
                    continue
                open_lost.append({"file": rel, "text": u.strip()[:600]})
                continue
            ids = sorted(identifiers(u))
            hint = [c for c in corpus if ids and any(i in corpus[c] for i in ids)][:3]
            cands.append({"file": rel, "text": u.strip()[:600], "ids": ids, "hint_files": hint})
        if waived:
            continue
        before_w = count_words(split_fm(text)[1])
        after_w = count_words(split_fm(read(cur_files[rel]) or "")[1]) if not gone else 0
        wb, wa = wb + before_w, wa + after_w
        if before_w >= 40 and after_w < 0.5 * before_w:
            shrunk.append({"file": rel, "words_before": before_w, "words_after": after_w,
                           "pct": round(100.0 * (after_w - before_w) / before_w, 1)})
    cands.sort(key=lambda c: (c["file"], c["text"]))
    growth = None
    if tree == "memory":
        pairs_b = [(os.path.relpath(p, os.path.join(root, "memory")).replace(os.sep, "/"), p)
                   for r_, p in old.items() if r_.startswith("memory/") and not approved(r_, expect)]
        eb, jb = entry_journal_words(pairs_b)
        ea, ja = entry_journal_words([(os.path.relpath(p, "memory").replace(os.sep, "/"), p) for p in md_walk("memory")])
        tb, ta = eb + jb, ea + ja
        growth = {"entry_words_before": eb, "entry_words_after": ea, "journal_words_before": jb, "journal_words_after": ja,
                  "pct": round(100.0 * (ta - tb) / tb, 1) if tb else None,
                  "total_drop_pct": round(100.0 * (tb - ta) / tb, 1) if tb else 0.0}
    drop = growth["total_drop_pct"] if growth else (100.0 * (wb - wa) / wb if wb else 0.0)
    return 0, frame("lost", True, [], tree=tree, open_lost=open_lost, candidates=cands, growth=growth,
                    shrunk_files=shrunk, shrink_alarm=bool(shrunk) or drop > 30, constants=constants_view())


def entry_journal_words(pairs):
    """[(путь внутри memory/, путь на диске)] -> (слова точек входа, слова journal/)."""
    e = j = 0
    for rel, path in pairs:
        fm, body = split_fm(read(path) or "")
        if rel.startswith("journal/"):
            j += count_words(body)
        elif "/" not in rel and fm_keys(fm).get("status") != "archive":
            e += count_words(body)
    return e, j


# ---------------------------------------------------------------- prepare / snapshot

def snapshots_dir(info, override):
    o = override or os.environ.get("KB_SCAN_SNAPSHOTS_DIR")
    if o:
        return o
    real = os.path.realpath(info["main_worktree"])
    slug = re.sub(r"[^A-Za-z0-9-]", "-", real)  # экранирование склеивает разные пути — различает хэш реального пути
    h = hashlib.sha256(real.encode("utf-8", "surrogateescape")).hexdigest()[:8]
    return os.path.join(os.path.expanduser("~/.claude"), "qtim-snapshots", f"{slug}-{h}")


def session_id():
    for k in ("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "CLAUDE_CODE_HOST_SESSION_ID"):
        if os.environ.get(k):
            return os.environ[k]
    return None


def load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def list_runs(snaps):
    runs = []
    if os.path.isdir(snaps):
        for n in sorted(os.listdir(snaps)):
            m = load_json(os.path.join(snaps, n, "marker.json"))
            if isinstance(m, dict):
                runs.append((n, m))
    return runs


def write_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)


def fingerprint():
    """Хэш содержимого memory/ + docs/features/ (включая untracked и ignored). Не git-дерево: не пишет объекты
    в репозиторий пользователя и видит правки, даже если memory/ — отдельный репозиторий."""
    h = hashlib.sha256()
    for top in ("memory", "docs/features"):
        for dp, dns, fns in os.walk(top):
            dns.sort()
            for fn in sorted(fns):
                p = os.path.join(dp, fn)
                h.update(nfc(p).encode("utf-8", "surrogateescape"))
                try:
                    with open(p, "rb") as f:
                        h.update(hashlib.sha256(f.read()).digest())
                except OSError:
                    pass
    return "sha256:" + h.hexdigest()


def lock_state(snaps):
    """free | mine | locked. mine — только при совпадении id сессии; чужой свежий lock — всегда locked.
    ponytail: живость по возрасту (pid скрипта короткоживущий, проверять нечем)."""
    lk = load_json(os.path.join(snaps, ".lock"))
    if not isinstance(lk, dict) or now() - int(lk.get("started_at") or 0) > CFG["lock_hours"] * 3600:
        return "free", None
    mine = bool(lk.get("session")) and lk.get("session") == session_id()
    return ("mine" if mine else "locked"), {"session": lk.get("session"), "pid": lk.get("pid"),
                                             "started_at": lk.get("started_at"), "run": lk.get("run")}


def md_dates(pairs):
    """[(rel, текст)] -> {rel: дата checked|None}."""
    res = {}
    for rel, text in pairs:
        ck = parse_checked(fm_keys(split_fm(text)[0]).get("checked"))
        res[rel] = ck[0] if ck else None
    return res


def tree_texts(treeish, blobs):
    """{nfc-путь: текст} всех memory/**/*.md в дереве/коммите."""
    rc, out, _ = git("ls-tree", "-r", "-z", "--full-tree", treeish, "--", "memory")
    res = {}
    for ent in out.split("\0"):
        meta, _, path = ent.partition("\t")
        parts = meta.split(" ")
        if len(parts) >= 3 and parts[1] == "blob" and path.endswith(".md"):
            res[nfc(path)] = blobs.text(parts[2]) or ""
    return res


def compare_version(disk, cand):
    """disk/cand: {rel: текст}. -> (файлов новее по дате, макс. дата, единиц только на диске, файлов только на диске).
    Версия — надмножество диска, если единиц только на диске нет (построчный lost диска против кандидата пуст)."""
    dd, cd = md_dates(disk.items()), md_dates(cand.items())
    newer = [r for r, d in cd.items() if d and (dd.get(r) is None or d > dd[r])]
    dates = [d for d in cd.values() if d]
    cand_units = {norm_unit(u) for t in cand.values() for u in md_units(t)}
    lines_only = 0
    for text in disk.values():
        lines_only += sum(1 for u in md_units(text) if len(norm_unit(u)) >= UNIT_MIN and norm_unit(u) not in cand_units)
    return len(newer), (max(dates) if dates else None), lines_only, sum(1 for r in disk if r not in cand)


_ANC = {}


def strict_ancestor(a_sha, b_sha):
    """a — строгий предок b (оба резолвятся)."""
    key = (a_sha, b_sha)
    if key not in _ANC:
        ra, fa, _ = git("rev-parse", "--verify", "--quiet", a_sha + "^{commit}")
        rb, fb, _ = git("rev-parse", "--verify", "--quiet", b_sha + "^{commit}")
        _ANC[key] = not (ra or rb or fa.strip() == fb.strip()) and \
            git("merge-base", "--is-ancestor", fa.strip(), fb.strip())[0] == 0
    return _ANC[key]


def ck_words(text):
    fm, body = split_fm(text)
    return parse_checked(fm_keys(fm).get("checked")), count_words(body)


def snapshot_versions(snaps):
    """[(run, состояние, before|after, каталог memory/)]: принятая версия — after у done, before у остальных."""
    out = []
    for run, m in sorted(list_runs(snaps), key=lambda x: x[1].get("started_at", 0)):
        sub = "after" if m.get("state") == "done" else "before"
        d = os.path.join(snaps, run, sub, "memory")
        if os.path.isdir(d):
            out.append((run, m.get("state"), sub, d))
    return out


def overwrite_suspects(mem, snaps, blobs):
    sus, seen = [], set()

    def add(f, reason, evidence):
        if (f, reason) not in seen:
            seen.add((f, reason))
            sus.append({"file": f, "reason": reason, "evidence": evidence})

    disk_paths = {f["rel"] for f in mem}
    richer = []  # на диске богаче HEAD (в HEAD заглушка без checked) — предупреждение, не затирание
    body = [f for f in mem if not f["journal"] and f["rel"] != EPIC_STATE]
    tracked = {nfc(p): p for p in git("ls-files", "-z", "--", "memory")[1].split("\0") if p}
    hist_cache = {}

    def versions(real):
        """(текст в HEAD, [(sha, текст)] последних версий из git) — один раз на файл."""
        if real not in hist_cache:
            hist = [(sha, blobs.text(f"{sha}:{real}") or "")
                    for sha in git("log", f"-{CFG['history_window']}", "--format=%H", "--", real)[1].split()]
            hist_cache[real] = (blobs.text("HEAD:" + real), hist)
        return hist_cache[real]

    def never_had_checked(f):
        """Файл под git, диск равен HEAD и ни в одной версии окна истории не было checked: так он и появился (legacy),
        затирания нет."""
        real = tracked.get(nfc(f["path"]))
        if real is None:
            return False
        head_txt, hist = versions(real)
        return head_txt == f["text"] and not any(ck_words(t)[0] for _, t in hist)

    for f in body:
        others = [g for g in body if g is not f]
        if (f["fm"] is None or not f["checked"]) and len(others) >= 3:
            share = sum(1 for g in others if g["checked"]) / len(others)
            if share >= 0.5 and not never_had_checked(f):
                add(f["rel"], "no_frontmatter", f"{round(share * 100)}% соседних файлов с checked; здесь "
                    f"{'нет frontmatter' if f['fm'] is None else 'frontmatter без валидного checked'}")
    # снимки: принятые версии всех прогонов, не только последнего
    best, newest_done = {}, None
    for run, st, sub, d in snapshot_versions(snaps):
        if st == "done":
            newest_done = (run, sub, d)
        for p in md_walk(d):
            rel = nfc("memory/" + os.path.relpath(p, d).replace(os.sep, "/"))
            w = count_words(split_fm(read(p) or "")[1])
            if w > best.get(rel, (0,))[0]:
                best[rel] = (w, run, sub)
    for f in mem:
        if f["rel"] == EPIC_STATE:
            continue
        w, run, sub = best.get(f["rel"], (0, None, None))
        if w >= 50 and f["words"] < 0.3 * w:
            add(f["rel"], "shrunk_vs_snapshot", f"{f['words']} слов сейчас против {w} в снимке {run}/{sub}")
    if newest_done:
        run, sub, d = newest_done
        for p in md_walk(d):
            rel = nfc("memory/" + os.path.relpath(p, d).replace(os.sep, "/"))
            if rel not in disk_paths and count_words(split_fm(read(p) or "")[1]) >= 50:
                add(rel, "vanished", f"файл был в последнем завершённом снимке {run}/{sub}, на диске его нет")
    # git-история: до 20 последних версий файла
    rc, out, _ = git("ls-tree", "-r", "-z", "--name-only", "HEAD", "--", "memory")
    for p in out.split("\0"):
        if p.endswith(".md") and nfc(p) not in disk_paths:
            add(nfc(p), "vanished", "файл есть в HEAD, на диске его нет")
    for f in mem:
        real = tracked.get(nfc(f["path"]))
        if real is None or f["rel"] == EPIC_STATE:
            continue
        head_txt, hist = versions(real)
        disk_ck = f["checked"]
        if head_txt is not None and f["text"] != head_txt:
            hck, hw = ck_words(head_txt)
            if hck is None and disk_ck and f["words"] >= hw:
                richer.append(f["rel"])  # HEAD — заглушка, диск богаче: не затирание, а незакоммиченная работа пользователя
            elif hck is None and disk_ck and hw >= 50 and f["words"] < 0.5 * hw:
                add(f["rel"], "baseline_over_existing", f"на диске {f['words']} слов с checked поверх {hw} слов в HEAD без checked")
            elif disk_ck is None and hck:
                add(f["rel"], "baseline_over_existing",
                    f"на диске заглушка без checked ({f['words']} слов) поверх версии в HEAD с checked @ {hck[1]} ({hw} слов)")
        if disk_ck is None:
            for sha, txt in hist:
                pck, pw = ck_words(txt)
                if pck:
                    add(f["rel"], "baseline_over_existing",
                        f"в истории (коммит {sha[:9]}) версия с checked @ {pck[1]} ({pw} слов), текущая — без checked")
                    break
        else:
            for x in {ck_words(t)[0][1] for _, t in hist if ck_words(t)[0]}:
                if x != disk_ck[1] and strict_ancestor(disk_ck[1], x):
                    add(f["rel"], "regressed_checked", f"checked @ {disk_ck[1]} — предок checked @ {x} из истории git")
                    break
    return sus, richer


def free_run_id(snaps, base):
    """Свободный id прогона `<дата>-<sha9 базы>[-N]`: занят — есть marker.json или before/after в каталоге."""
    prefix = f"{today()}-{(base.get('sha') or 'nobase')[:9]}"
    n, rid = 1, prefix
    while True:
        rd = os.path.join(snaps, rid)
        if not (os.path.isfile(os.path.join(rd, "marker.json")) or os.path.isdir(os.path.join(rd, "before"))
                or os.path.isdir(os.path.join(rd, "after"))):
            return rid
        n += 1
        rid = f"{prefix}-{n}"


def cmd_prepare(a):
    info = repo_info()
    snaps = snapshots_dir(info, a.snapshots_dir)
    empty_resume = {"present": False, "run": None, "snapshot": None, "fingerprint_matches": None, "foreign": []}
    if info["git"] != "ok":
        return 2, frame("prepare", False, [err("git_absent", "не git-репозиторий",
                                                "запусти из корня репозитория: без git нет базы сравнения")],
                        repo=dict(info, base=None), storage=None, overwrite={"suspects": []},
                        lock={"state": "free", "holder": None}, resume=empty_resume, snapshots_dir=snaps)
    errors = []
    fetch_error = None
    if not a.no_fetch:
        rc, _, e = git("fetch", "origin", timeout=FETCH_TIMEOUT)
        if rc != 0:
            fetch_error = (e.strip().split("\n")[0] if e.strip() else f"git fetch завершился с кодом {rc}")[:200]
            if rc == 124:
                fetch_error = f"таймаут {FETCH_TIMEOUT} с"
    mem = load_memory()
    base = resolve_base(a.base, mem)
    base["fetch_error"] = fetch_error
    fp = git("rev-parse", "--git-path", "FETCH_HEAD")[1].strip()
    if fp and os.path.exists(fp):
        base["fetch_head_age_s"] = max(0, now() - int(os.path.getmtime(fp)))
    be = base_error(base)
    if be and (a.base or base["source"] != "none"):
        errors.append(be)
    info["base"] = base

    def count(opts, path, md_only):
        rc, out, _ = git("ls-files", "-z", *opts, "--", path)
        return len([p for p in out.split("\0") if p and (not md_only or p.endswith(".md"))])

    disk = {f["rel"]: f["text"] for f in mem}
    blobs = Blobs()
    try:
        rc, out, _ = git("for-each-ref", "--format=%(refname:short)", "refs/heads", "refs/remotes")
        refs = [r for r in out.split("\n") if r and not r.endswith("/HEAD") and r != "origin"]
        rc, out, _ = git("cat-file", "--batch-check", inp="".join(f"{r}:memory\n" for r in refs))
        with_mem = []
        for r, line in zip(refs, out.split("\n")):
            parts = line.split(" ")
            if len(parts) >= 2 and parts[1] == "tree":
                with_mem.append({"ref": r, "tree": parts[0]})
        newer, differs = [], []

        def classify(source, ref_or_path, texts):
            cnt, mx, lines_only, files_only = compare_version(disk, texts)
            if not cnt:
                return
            if lines_only == 0:  # надмножество диска — единственный случай, когда «восстановить» безопасно
                newer.append({"source": source, "ref_or_path": ref_or_path, "checked_max": mx, "files": cnt})
            else:
                differs.append({"source": source, "ref_or_path": ref_or_path, "checked_max": mx, "files_newer": cnt,
                                "lines_only_on_disk": lines_only, "files_only_on_disk": files_only})

        seen_trees = set()
        for e in with_mem:
            if e["tree"] not in seen_trees:
                seen_trees.add(e["tree"])
                classify("ref", e["ref"], tree_texts(e["ref"], blobs))
        stashes = []
        for sref in [x for x in git("stash", "list", "--format=%gd")[1].split("\n") if x]:
            fl = [p for p in git("diff", "-z", "--name-only", sref + "^1", sref, "--", "memory", "docs/features")[1].split("\0") if p]
            texts = tree_texts(sref, blobs)
            if git("rev-parse", "--verify", "--quiet", sref + "^3")[0] == 0:
                fl += [p for p in git("ls-tree", "-r", "-z", "--name-only", sref + "^3", "--", "memory", "docs/features")[1].split("\0") if p]
                texts.update(tree_texts(sref + "^3", blobs))
            if fl:
                stashes.append({"ref": sref, "files": sorted(set(nfc(p) for p in fl))})
                classify("stash", sref, texts)
        runs = list_runs(snaps)
        for run, m in runs:
            root = os.path.join(snaps, run, "after", "memory")
            if m.get("state") == "done" and os.path.isdir(root):
                classify("snapshot", os.path.join(snaps, run, "after"),
                         {"memory/" + nfc(os.path.relpath(p, root)).replace(os.sep, "/"): read(p) or "" for p in md_walk(root)})
        suspects, richer = overwrite_suspects(mem, snaps, blobs)
        modified = sorted({nfc(p) for p in git("diff", "-z", "--name-only", "HEAD", "--", "memory", "docs/features")[1].split("\0") if p})
        head_vs_base = None
        if base["sha"]:
            ht = git("rev-parse", "--verify", "--quiet", "HEAD:memory")[1].strip() or None
            bt = git("rev-parse", "--verify", "--quiet", base["sha"] + ":memory")[1].strip() or None
            lr = git("rev-list", "--left-right", "--count", f"{base['sha']}...HEAD")[1].split()
            head_vs_base = {"head_tree": ht, "base_tree": bt, "same": ht == bt,
                            "ahead": int(lr[1]) if len(lr) == 2 else None, "behind": int(lr[0]) if len(lr) == 2 else None}
    finally:
        blobs.close()
    storage = {
        "memory": {"present": os.path.isdir("memory"), "md_files": len(md_walk("memory")),
                   "tracked": count([], "memory", True), "untracked": count(["--others", "--exclude-standard"], "memory", True),
                   "ignored": count(["--others", "--ignored", "--exclude-standard"], "memory", True),
                   "modified": modified, "disk_richer_than_head": richer, "head_vs_base": head_vs_base,
                   "refs_with_memory": with_mem, "distinct_trees": len({e["tree"] for e in with_mem}),
                   "stashes": stashes, "newer_versions": newer, "differs": differs},
        "features": {"present": os.path.isdir("docs/features"), "tracked": count([], "docs/features", False),
                     "untracked": count(["--others", "--exclude-standard"], "docs/features", False),
                     "ignored": count(["--others", "--ignored", "--exclude-standard"], "docs/features", False)},
    }
    running = sorted(((r, m) for r, m in runs if m.get("state") == "running"), key=lambda x: x[1].get("started_at", 0))
    resume = dict(empty_resume)
    resume["foreign"] = [{"run": r, "root": m.get("root"), "branch": m.get("branch")}
                         for r, m in running if m.get("root") != info["root"]]
    mine_runs = [(r, m) for r, m in running if m.get("root") == info["root"]]
    if mine_runs:
        run, m = mine_runs[-1]
        resume.update(present=True, run=run, snapshot=os.path.join(snaps, run), branch=m.get("branch"),
                      branch_matches=m.get("branch") == info["branch"],
                      fingerprint_matches=m.get("fingerprint") == fingerprint())
    state, holder = lock_state(snaps)
    return (2 if errors else 0), frame("prepare", not errors, errors, repo=info, storage=storage,
                    overwrite={"suspects": suspects}, lock={"state": state, "holder": holder}, resume=resume,
                    run={"free_id": free_run_id(snaps, base)}, snapshots_dir=snaps, constants=constants_view())


def copy_tree(src, dst):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copytree(src, dst, symlinks=True)


def is_only_copy(snaps, run, m, kept, am_dir):
    """Снимок хранит то, что детектор считает утраченным (файл съёжился < 30% или исчез с диска) — в memory/ или
    в auto-memory: ротация его не удаляет, это может быть единственная хорошая копия (auto-memory вне git)."""
    sub = "after" if m.get("state") == "done" else "before"
    newest = kept[-1] if kept else None
    for tree, cur_base in (("memory", "memory"), ("automemory", am_dir)):
        d = os.path.join(snaps, run, sub, tree)
        newest_d = (os.path.join(snaps, newest[0], "after", tree)
                    if newest and newest[1].get("state") == "done" else None)
        for p in md_walk(d) if os.path.isdir(d) else []:
            rel = os.path.relpath(p, d).replace(os.sep, "/")
            w = count_words(split_fm(read(p) or "")[1])
            if w < 50:
                continue
            cur = os.path.join(cur_base, rel)
            if os.path.isfile(cur):
                if count_words(split_fm(read(cur) or "")[1]) < 0.3 * w:
                    return f"{tree}/{rel}"
            elif newest_d and os.path.isfile(os.path.join(newest_d, rel)):
                return f"{tree}/{rel}"
    return None


def rotate(snaps, am_dir, keep=3):
    """-> (удалённые, сохранённые сверх лимита с причиной). Незавершённые снимки не вытесняются."""
    done = sorted(((r, m) for r, m in list_runs(snaps) if m.get("state") == "done"), key=lambda x: x[1].get("started_at", 0))
    gone, blocked = [], []
    kept = done[-keep:] if keep else []
    for r, m in (done[:-keep] if len(done) > keep else []):
        why = is_only_copy(snaps, r, m, kept, am_dir)
        if why:
            blocked.append({"run": r, "file": why, "reason": "содержит файл, который на диске (или в auto-memory) съёжился или исчез"})
        else:
            shutil.rmtree(os.path.join(snaps, r), ignore_errors=True)
            gone.append(r)
    return gone, blocked


def copy_automemory(am_dir, dst, arg):
    """-> статус (ok|not_found|override_unresolved|copy_failed)."""
    if not arg and override_configured():
        return "override_unresolved"
    if not os.path.isdir(am_dir):
        return "not_found"
    try:
        copy_tree(am_dir, dst)
    except OSError:
        return "copy_failed"
    return "ok"


def cmd_snapshot(a):
    info = repo_info()
    snaps = snapshots_dir(info, a.snapshots_dir)

    def fail(code, msg, fix, rd=None):
        return 2, frame("snapshot", False, [err(code, msg, fix)], run=a.run, path=rd, fingerprint=None,
                        automemory_copied=False, automemory_status=None, rotated=[], rotation_blocked=[], checkpoint=False)

    if not a.run or not re.fullmatch(r"[\w.-]+", a.run):
        return fail("bad_run", "нужен --run <id> из [A-Za-z0-9_.-]", "передай --run <id>")
    rd, mp = os.path.join(snaps, a.run), os.path.join(snaps, a.run, "marker.json")
    os.makedirs(snaps, mode=0o700, exist_ok=True)
    os.chmod(snaps, 0o700)
    marker = load_json(mp)
    marker = marker if isinstance(marker, dict) else None
    lock_p = os.path.join(snaps, ".lock")

    def release():
        lk = load_json(lock_p)
        if isinstance(lk, dict) and lk.get("run") == a.run:
            os.remove(lock_p)
            return True
        return False

    if a.abandon:
        if not marker:
            return fail("run_unknown", f"прогон {a.run} не найден в {snaps}", "проверь id прогона", rd)
        if marker.get("state") != "running":
            return fail("run_done", f"прогон {a.run} уже закрыт ({marker.get('state')})", "закрывать нечего", rd)
        marker.update(state="abandoned", abandoned_at=now())
        write_json(mp, marker)
        return 0, frame("snapshot", True, [], run=a.run, path=rd, state="abandoned", released_lock=release())
    am_dir = a.automemory_dir or derived_automemory_dir(info)
    if a.after:
        if not marker:
            return fail("run_unknown", f"прогон {a.run} не найден в {snaps}", "сначала `snapshot --run <id>` (до первой записи)", rd)
        if marker.get("root") != info["root"]:
            return fail("run_foreign", f"прогон {a.run} начат в другом дереве: {marker.get('root')}: копия «после» была бы снята не с того дерева",
                        "запусти из дерева, где начат прогон", rd)
        after = os.path.join(rd, "after")
        shutil.rmtree(after, ignore_errors=True)
        for src in ("memory", "docs/features"):
            if os.path.isdir(src):
                copy_tree(src, os.path.join(after, src))
        am_status = copy_automemory(am_dir, os.path.join(after, "automemory"), a.automemory_dir)
        marker.update(state="done", finished_at=now(), fingerprint_after=fingerprint())
        write_json(mp, marker)
        release()
        gone, blocked = rotate(snaps, am_dir)
        return 0, frame("snapshot", True, [], run=a.run, path=rd, fingerprint=marker["fingerprint_after"],
                        automemory_copied=am_status == "ok", automemory_status=am_status, rotated=gone,
                        rotation_blocked=blocked, checkpoint=False)
    if marker and marker.get("state") in ("done", "abandoned"):
        return fail("run_done", f"прогон {a.run} уже закрыт ({marker['state']}): снимок «до» от него не подходит",
                    "возьми новый --run (например, с суффиксом -2)", rd)
    if marker and marker.get("root") != info["root"]:
        return fail("run_foreign", f"прогон {a.run} начат в другом дереве: {marker.get('root')}",
                    "продолжай из того дерева или возьми новый --run", rd)
    state, holder = lock_state(snaps)
    if state == "locked" and not a.force_lock:
        return fail("locked", f"другой прогон держит lock: {holder}",
                    "спроси пользователя; продолжить — повтори с --force-lock", rd)
    fp = fingerprint()
    copied, am_status, checkpoint = False, None, False
    if marker and os.path.isdir(os.path.join(rd, "before")):
        checkpoint = True  # resume: снимок «до» не перезаписывается, только отпечаток маркера
        marker["fingerprint"] = fp
    else:
        os.makedirs(rd, mode=0o700, exist_ok=True)
        shutil.rmtree(os.path.join(rd, "before"), ignore_errors=True)  # остаток оборванного снимка без маркера
        for src in ("memory", "docs/features"):
            if os.path.isdir(src):
                copy_tree(src, os.path.join(rd, "before", src))
        am_status = copy_automemory(am_dir, os.path.join(rd, "before", "automemory"), a.automemory_dir)
        if am_status == "copy_failed":
            return fail("automemory_copy_failed", f"каталог auto-memory {am_dir} есть, но не скопировался в снимок",
                        "исправь права на каталог и повтори: без копии auto-memory не трогают", rd)
        copied = am_status == "ok"
        marker = {"run": a.run, "state": "running", "started_at": now(), "fingerprint": fp,
                  "root": info["root"], "branch": info["branch"]}
    write_json(mp, marker)
    write_json(lock_p, {"session": session_id(), "pid": os.getppid(), "started_at": now(), "run": a.run})
    gone, blocked = ([], []) if checkpoint else rotate(snaps, am_dir)
    return 0, frame("snapshot", True, [], run=a.run, path=rd, fingerprint=fp, automemory_copied=copied,
                    automemory_status=am_status, rotated=gone, rotation_blocked=blocked, checkpoint=checkpoint)


# ---------------------------------------------------------------- stamp

def today():
    return time.strftime("%Y-%m-%d", time.localtime(now()))


def set_fm_key(lines, key, value):
    """Меняет `key:` во frontmatter (любой отступ) или вставляет после последнего ключа блока metadata."""
    for i, ln in enumerate(lines):
        m = re.match(r"^(\s*)" + re.escape(key) + r":", ln)
        if m:
            lines[i] = f"{m.group(1)}{key}: {value}"
            return
    anchor, indent = None, ""
    for i, ln in enumerate(lines):
        m = re.match(r"^(\s+)(?:type|genre|audience|updated|checked|status):", ln)
        if m:
            anchor, indent = i, m.group(1)
    if anchor is None:
        lines.append(f"{key}: {value}")
    else:
        lines.insert(anchor + 1, f"{indent}{key}: {value}")


def stamp_text(text, checked, date):
    m = FM_RE.match(text)
    if not m:
        return None
    lines = m.group(1).split("\n")
    set_fm_key(lines, "updated", date)
    set_fm_key(lines, "checked", checked)
    return text[:m.start(1)] + "\n".join(lines) + text[m.end(1):]


def same_bytes(p1, p2):
    try:
        with open(p1, "rb") as f1, open(p2, "rb") as f2:
            return f1.read() == f2.read()
    except OSError:
        return False


def cmd_stamp(a):
    def fail(code, msg, fix):
        return 2, frame("stamp", False, [err(code, msg, fix)], checked=None, stamped=[], skipped=[])

    if not a.sha or not (a.files or a.intact):
        return fail("bad_args", "нужны --sha <base-sha> и --files <…> и/или --intact",
                    "передай sha базы и файлы, сверенные целиком")
    before_root = None
    if a.intact:
        # --intact считает «якоря не менялись» по диску ПОСЛЕ правок писателя; штампуем только то, что байт-в-байт
        # равно снимку «до», иначе правленый непроверенный файл получил бы checked.
        before_root = snapshot_root(a.snapshot) if a.snapshot else None
        if not before_root or not os.path.isdir(os.path.join(before_root, "memory")):
            return fail("bad_args", "--intact требует --snapshot <снимки>/<run>/before со снимком memory/",
                        "передай каталог «до» из ответа snapshot")
    rc, full, _ = git("rev-parse", "--verify", "--quiet", a.sha + "^{commit}")
    if rc != 0:
        return fail("sha_unresolved", f"{a.sha} не резолвится в коммит", "передай sha базы из ответа prepare")
    full = full.strip()
    checked = f"{today()} @ {full[:9]}"
    files, skipped = [], []
    for p in a.files or []:
        p = nfc(p[2:] if p.startswith("./") else p)
        if not re.fullmatch(r"memory/.+\.md", p) or ".." in p.split("/"):
            skipped.append({"file": p, "reason": "not_memory_file"})
        elif p == "memory/epic-state.md":
            skipped.append({"file": p, "reason": "epic_state"})
        elif not os.path.isfile(p):
            skipped.append({"file": p, "reason": "missing"})
        elif p not in files:
            files.append(p)
    # Один расчёт delta на sha базы: файл с непроверяемыми якорями (branch_only) не сверен целиком —
    # checked ему не ставится ни по --files, ни по --intact.
    mem = load_memory()
    blobs = Blobs()
    try:
        fields, derr = do_delta(mem, {"ref": full, "sha": full, "source": "arg"}, repo_info(), blobs)
    finally:
        blobs.close()
    if derr:
        return fail("delta_failed", derr[0]["message"], derr[0]["fix"])
    by_file = {r["file"]: r for r in fields["files"]}
    explicit, files = files, []
    for p in explicit:
        if by_file.get(p, {}).get("unverified_anchors"):
            skipped.append({"file": p, "reason": "unverified_anchors"})
        else:
            files.append(p)
    if a.intact:
        for r in fields["files"]:
            if r["stale"] or r["file"] in explicit:
                continue
            if r["unverified_anchors"]:
                skipped.append({"file": r["file"], "reason": "unverified_anchors"})
            elif not same_bytes(r["file"], os.path.join(before_root, r["file"])):
                skipped.append({"file": r["file"], "reason": "changed_in_run"})
            else:
                files.append(r["file"])
    stamped = []
    for p in files:
        text = read(p) or ""
        new = stamp_text(text, checked, today())
        if new is None:
            skipped.append({"file": p, "reason": "no_frontmatter"})
            continue
        if new != text:
            with open(p, "w", encoding="utf-8", errors="surrogateescape") as f:
                f.write(new)
        stamped.append({"file": p, "changed": new != text})
    return 0, frame("stamp", True, [], checked=checked, stamped=stamped, skipped=skipped)



# ---------------------------------------------------------------- delta / features / all

def cmd_delta(a):
    info, mem = repo_info(), load_memory()
    if info["git"] != "ok":
        return 2, frame("delta", False, [err("git_absent", "не git-репозиторий", "запусти в репозитории")], present=False)
    base = resolve_base(a.base, mem)
    be = base_error(base)
    if be:
        return 2, frame("delta", False, [be], present=False, ask_base=True)
    if not mem:
        return 2, frame("delta", False, [no_memory_error()], present=False)
    blobs = Blobs()
    try:
        fields, errors = do_delta(mem, base, info, blobs)
    finally:
        blobs.close()
    return (2 if errors else 0), frame("delta", not errors, errors, constants=constants_view(), **fields)


def cmd_features(a):
    info, mem = repo_info(), load_memory()
    if info["git"] != "ok":
        return 2, frame("features", False, [err("git_absent", "не git-репозиторий", "запусти в репозитории")], present=False, items=[])
    base = resolve_base(a.base, mem)
    be = base_error(base)
    if be:
        return 2, frame("features", False, [be], present=False, items=[])
    return 0, frame("features", True, [], **do_features(base, info))


def cmd_all(a):
    info, mem = repo_info(), load_memory()
    errors, out = [], {}
    if not mem:
        errors.append(no_memory_error())
    base = resolve_base(a.base, mem) if info["git"] == "ok" else None
    if info["git"] != "ok":
        errors.append(err("git_absent", "не git-репозиторий", "запусти в репозитории: без git нет базы сравнения"))
    elif base_error(base):
        errors.append(base_error(base))
        base = None
    blobs = Blobs()
    try:
        if mem:
            fields, _ = do_validate(mem, base, blobs)
            out["validate"] = fields
        else:
            out["validate"] = {"present": False}
        if mem and base:
            out["delta"], derr = do_delta(mem, base, info, blobs)
            errors += derr
        else:
            out["delta"] = {"present": False}
        out["features"] = do_features(base, info) if base else {"present": False, "items": []}
    finally:
        blobs.close()
    out["automemory"] = do_automemory(info, a.automemory_dir)
    out["memory_words"] = sum(f["words"] for f in mem)
    return (2 if errors else 0), frame("all", not errors, errors, constants=constants_view(), **out)


# ---------------------------------------------------------------- CLI

def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="совместимость: вывод всегда JSON")
    common.add_argument("--now", type=int, help="фиксированное время (epoch), для тестов")
    common.add_argument("--snapshots-dir", help="каталог снимков проекта (по умолчанию ~/.claude/qtim-snapshots/<проект>)")
    common.add_argument("--entry-words", type=int, help=f"предупреждение для точки входа, слов (сейчас {CFG['entry_words']})")
    common.add_argument("--role-words", type=int, help=f"потолок read-on-spawn роли, слов (сейчас {CFG['role_words']})")
    common.add_argument("--journal-ratio", type=float, help=f"journal <= N x слов точек входа (сейчас {CFG['journal_ratio']})")
    common.add_argument("--sample", type=int, help=f"выборка утверждений вне дельты (сейчас {CFG['sample']})")
    p = argparse.ArgumentParser(prog="kb_scan.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, *flags):
        sp = sub.add_parser(name, parents=[common])
        sp.set_defaults(fn=fn)
        for f, kw in flags:
            sp.add_argument(f, **kw)
        return sp

    base = ("--base", {"help": "база сравнения: origin/<ветка> (main/master не угадываются)"})
    am = ("--automemory-dir", {"help": "каталог auto-memory дословно из блока системного промпта"})
    add("prepare", cmd_prepare, base, ("--no-fetch", {"action": "store_true", "help": "не ходить в сеть"}))
    add("snapshot", cmd_snapshot, ("--run", {"help": "id прогона"}), ("--after", {"action": "store_true"}),
        ("--abandon", {"action": "store_true", "help": "закрыть брошенный прогон (маркер -> abandoned, lock снят)"}), am,
        ("--force-lock", {"action": "store_true"}))
    add("stamp", cmd_stamp, ("--sha", {"help": "sha базы, на котором сверены файлы"}),
        ("--files", {"nargs": "*", "help": "memory/**.md, сверенные целиком"}),
        ("--intact", {"action": "store_true", "help": "плюс файлы delta со stale:false, равные копии в --snapshot"}),
        ("--snapshot", {"help": "каталог «до» (<снимки>/<run>/before): обязателен с --intact"}))
    add("validate", cmd_validate, base)
    add("delta", cmd_delta, base)
    add("features", cmd_features, base)
    add("automemory", cmd_automemory, am)
    add("lost", cmd_lost, ("--snapshot", {"required": True, "help": "каталог прогона или его before/"}),
        ("--tree", {"choices": ("memory", "automemory"), "default": "memory", "help": "что сверять со снимком"}), am,
        ("--expect-deleted", {"nargs": "*", "help": "одобренные удаления: файлы или каталоги-префиксы (пусто — не передавать)"}))
    add("all", cmd_all, base, am)
    add("selftest", cmd_selftest)
    return p


def execute(argv):
    """-> (код выхода, payload|None). Без побочных печатей — selftest зовёт это же."""
    global NOW
    a = build_parser().parse_args(argv)
    CFG.clear()
    CFG.update(DEFAULT_CFG)  # флаги прошлого вызова (selftest зовёт execute подряд) не протекают
    for k, dest in (("entry_words", "entry_words"), ("role_words", "role_words"),
                    ("journal_ratio", "journal_ratio"), ("sample", "sample")):
        if getattr(a, k, None) is not None:
            CFG[dest] = getattr(a, k)
    if a.now is not None:
        NOW = a.now
    rc, top, _ = git("rev-parse", "--show-toplevel")
    if rc == 0 and top.strip():
        os.chdir(top.strip())
    return a.fn(a)


def emit(payload):
    txt = json.dumps(payload, ensure_ascii=False, indent=1 if sys.stdout.isatty() else None)
    sys.stdout.buffer.write((txt + "\n").encode("utf-8", "replace"))
    sys.stdout.flush()


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):  # локаль CI может быть не UTF-8; печатаем кириллицу в любом случае
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    rc, payload = execute(sys.argv[1:] if argv is None else argv)
    if payload is not None:
        emit(payload)
    return rc


# ---------------------------------------------------------------- selftest

EXPECTED_CASES = 100  # меньше выполненных кейсов — выход 1: тест, который молча перестал бежать, не зелёный


class _Repo:
    def __init__(self, path):
        self.path, self.n = path, 0
        os.makedirs(path)
        self.sh("init", "-q")
        self.sh("symbolic-ref", "HEAD", "refs/heads/main")

    def sh(self, *args, env=None):
        rc, out, e = git(*args, cwd=self.path, env_extra=env)
        if rc != 0:
            raise RuntimeError(f"git {' '.join(args)}: {e.strip()}")
        return out.strip()

    def put(self, rel, text):
        p = os.path.join(self.path, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)

    def commit(self, msg):
        self.n += 1
        d = f"2026-01-{self.n:02d}T12:00:00Z"
        self.sh("add", "-A")
        self.sh("commit", "-q", "-m", msg, env={"GIT_AUTHOR_DATE": d, "GIT_COMMITTER_DATE": d})
        return self.sh("rev-parse", "HEAD")


def _mem(name, checked, body, genre="reference", status="current"):
    ck = f"  checked: {checked}\n" if checked else ""
    return (f"---\nname: {name}\ndescription: тест\nmetadata:\n  type: project\n  genre: {genre}\n  audience: none\n"
            f"  updated: 2026-01-02\n{ck}  status: {status}\n---\n\n# {name}\n\n{NAV_MARK} тест · **Не здесь:** ничего\n\n{body}\n")


def _run(argv, cwd):
    old = os.getcwd()
    os.chdir(cwd)
    try:
        return execute(argv)
    finally:
        os.chdir(old)


def _rec(payload, name):
    return next((f for f in payload["files"] if f["file"] == "memory/" + name), None)


def _selftest_body(tmp, t):
    R = _Repo(os.path.join(tmp, "mixed"))
    R.put("src/a.ts", "export const KEEP = 1;\n")
    R.put("src/b.ts", "export function gone() {}\nexport const stay = 2;\n")
    R.put("src/del.ts", "export const x = 1;\n")
    R.put("src/old.ts", "export function moved() {}\n")
    R.put("src/модуль/файл.ts", "export function Foo() {}\n")
    R.put("src/comp.vue", "<script>export default { name: 'Comp' }</script>\n")
    R.put("src/foo.ts", "export class FooBar {}\n")
    R.put("package.json", '{"scripts": {"build": "x"}}\n')
    c1 = R.commit("code")
    ck = f"2026-01-01 @ {c1[:12]}"
    R.sh("checkout", "-q", "-b", "side")
    R.put("side.txt", "s\n")
    side = R.commit("side")
    R.sh("checkout", "-q", "main")
    files = {
        "MEMORY.md": ("- индекс", ck),
        "intact.md": ("- `src/a.ts#KEEP` живая константа", ck),
        "sym_removed.md": ("- `src/b.ts#gone` функция", ck),
        "file_deleted.md": ("- `src/del.ts#x` константа", ck),
        "renamed.md": ("- `src/old.ts#moved` функция", ck),
        "cyr.md": ("- `src/модуль/файл.ts#Foo` функция", ck),
        "vue.md": ("- `src/comp.vue#Comp` компонент", ck),
        "vue_ok.md": ("- `src/comp.vue#Other` компонент", ck),
        "foo.md": ("- `src/foo.ts#Foo` класс", ck),
        "nochecked.md": ("- `src/a.ts#KEEP` константа", None),
        "unknown_sha.md": ("- `src/a.ts#KEEP` константа", "2026-01-01 @ deadbeefdeadbeef"),
        "not_ancestor.md": ("- `src/a.ts#KEEP` константа", f"2026-01-01 @ {side[:12]}"),
        "json.md": ("- `package.json#scripts.build` ок\n- `package.json#scripts.nope` нет", ck),
        "policy.md": ("- policy: правило команды без якоря", ck),
        "wt.md": ("- `src/wt.ts#Wt` только на диске", ck),
        "cmd.md": ("- `src/a.ts#KEEP` проверка: `rg -n KEEP src` и `rm -rf build`", ck),
    }
    for n, (body, c) in files.items():
        R.put("memory/" + n, _mem(n[:-3], c, body))
    R.put("memory/idx.md", _mem("idx", ck, "- строка индекса без якоря", genre="index"))
    R.put("memory/dec.md", _mem("dec", ck, "- 2026-01-01 · решение · указатель без якоря", genre="decision"))
    R.put("memory/ref.md", _mem("ref", ck, "- утверждение о коде без якоря", genre="reference"))
    R.put("memory/plain.md", "# без frontmatter\n\n- `src/a.ts#KEEP` заглушка\n")
    R.put("memory/journal/old.md", _mem("old", ck, "- open: живая гонка в X", genre="journal", status="archive"))
    R.put(".claude/team-charter.md", "# Charter\n\n| Роль | subagent_type | Read on spawn |\n|---|---|---|\n"
          "| **ci** | `ci-agent` | `memory/intact.md`, `memory/ghost.md`, `memory/later.md` (создаётся по требованию) |\n")
    R.put(".claude/agents/ci-agent.md", "---\nname: ci-agent\n---\nПеред началом прочитай read-on-spawn: `MEMORY.md`, `intact.md`.\n")
    R.put(".claude/rules/memory.md", "# Память\n\n" + "Правило: checked: дата @ sha, якорь #Symbol, journal/ для закрытого. Дописал — сожми. open: и fixed: метки. " * 25)
    R.put(".codex/team-charter.md", "Читай `memory/lessons.md` § Уроки перед стартом.\n")
    R.commit("memory")
    R.put("src/b.ts", "export const stay = 2;\n")
    os.remove(os.path.join(R.path, "src/del.ts"))
    R.sh("mv", "src/old.ts", "src/new.ts")
    R.put("src/модуль/файл.ts", "export function Foo() {}\nexport const Extra = 1;\n")
    R.put("src/comp.vue", "<script>export default { name: 'Other' }</script>\n")
    R.commit("mutations")
    R.put("src/wt.ts", "export const Wt = 1;\n")  # только на диске
    R.put("memory/plain2.md", "# без frontmatter, не в git\n\n- `src/a.ts#KEEP` заглушка\n")
    R.put("memory/journal/fresh.md", _mem("fresh", ck, "- закрыто", genre="journal", status="archive"))
    global NOW
    NOW = 1782864000  # 2026-07-01

    rc, v = _run(["validate"], R.path)
    codes = [(p["code"], p["detail"]) for p in v["problems"]]
    t.check("validate: Foo при наличии только FooBar — symbol_missing", ("anchor_symbol_missing", "src/foo.ts#Foo") in codes, codes)
    t.check("validate: .vue#символ — распознан и проверен", ("anchor_symbol_missing", "src/comp.vue#Comp") in codes
            and not any("Other" in d for _, d in codes), codes)
    t.check("validate: символ удалён / файл удалён / json-ключ отсутствует",
            all(x in codes for x in [("anchor_symbol_missing", "src/b.ts#gone"), ("anchor_file_missing", "src/del.ts#x"),
                                     ("anchor_symbol_missing", "package.json#scripts.nope")]), codes)
    t.check("validate: json-ключ scripts.build найден обходом ключей", ("anchor_symbol_missing", "package.json#scripts.build") not in codes)
    t.check("validate: замечания -> exit 1, schema/scan_complete", rc == 1 and v["schema"] == SCHEMA and v["scan_complete"] is True, rc)
    j = {f["file"]: f for f in v["budgets"]["journal"]["files"]}
    t.check("validate: journal.files — возраст по git, has_open",
            j["memory/journal/old.md"]["age_source"] == "git" and j["memory/journal/old.md"]["age_days"] >= 170
            and j["memory/journal/old.md"]["has_open"] is True, j)
    t.check("validate: journal.files — untracked по mtime, без open",
            j["memory/journal/fresh.md"]["age_source"] == "mtime" and j["memory/journal/fresh.md"]["has_open"] is False, j)
    dead = {(d["path"], d["expected_absent"]) for d in v["consumers"]["dead"]}
    t.check("validate: мёртвый контракт в read-on-spawn; «создаётся» -> expected_absent",
            ("memory/ghost.md", False) in dead and ("memory/later.md", True) in dead, dead)
    t.check("validate: чужой контракт .codex -> foreign_contracts с заголовком",
            any(x["path"] == "memory/lessons.md" and x["heading"] == "Уроки перед стартом" for x in v["consumers"]["foreign_contracts"]),
            v["consumers"]["foreign_contracts"])
    t.check("validate: команда вне read-only allowlist -> unsafe_commands",
            any(u["command"] == "rm -rf build" for u in v["unsafe_commands"]) and not any(u["command"].startswith("rg") for u in v["unsafe_commands"]),
            v["unsafe_commands"])
    t.check("validate: копия канона в .claude/rules -> canon_copies", [c["path"] for c in v["canon_copies"]] == [".claude/rules/memory.md"], v["canon_copies"])
    rb = {r["role"]: r for r in v["budgets"]["roles"]}
    rc3, v3 = _run(["validate", "--role-words", "1"], R.path)
    t.check("validate: бюджет read-on-spawn роли — слова по файлам агента, top; флаг переопределяет потолок",
            rb["ci"]["words"] > 0 and rb["ci"]["over"] is False and len(rb["ci"]["top"]) == 2
            and v3["budgets"]["roles"][0]["over"] is True and v3["budgets"]["roles"][0]["limit"] == 1, (rb, v3["budgets"]["roles"]))
    rc2, v2 = _run(["validate", "--base", "main", "--entry-words", "3"], R.path)
    t.check("validate --base: файл только на диске -> worktree_only; флаг переопределяет бюджет",
            v2["anchors"]["worktree_only"] == 1 and v2["anchors_source"].startswith("main@")
            and all(e["over"] and e["limit"] == 3 for e in v2["budgets"]["entry"]), (v2["anchors"], v2["anchors_source"]))

    rc, d = _run(["delta", "--base", "main"], R.path)
    st = lambda n: [x["state"] for x in _rec(d, n)["stale_anchors"]]
    t.check("delta: символ удалён -> stale, symbol_missing", _rec(d, "sym_removed.md")["stale"] and st("sym_removed.md") == ["symbol_missing"], _rec(d, "sym_removed.md"))
    t.check("delta: файл удалён -> deleted", st("file_deleted.md") == ["deleted"], st("file_deleted.md"))
    rn = _rec(d, "renamed.md")["stale_anchors"]
    t.check("delta: переименован -> renamed_to", rn and rn[0]["state"] == "renamed" and rn[0]["renamed_to"] == "src/new.ts", rn)
    t.check("delta: путь с кириллицей — изменённый файл попадает в stale", st("cyr.md") == ["modified"], st("cyr.md"))
    ok = _rec(d, "intact.md")
    t.check("delta: нетронутый якорь — файл свежий, sha ok, отставание 2", not ok["stale"] and ok["sha_state"] == "ok"
            and ok["commits_behind"] == 2, ok)
    t.check("delta: файл без checked -> устаревший целиком", _rec(d, "nochecked.md")["reason"] == "no_checked", _rec(d, "nochecked.md"))
    un = _rec(d, "unknown_sha.md")
    t.check("delta: неизвестный sha -> unresolved, свежесть по дате", un["sha_state"] == "unresolved"
            and un["reason"] == "sha_unresolved" and un["freshness"] == "by_date", un)
    na = _rec(d, "not_ancestor.md")
    t.check("delta: sha не предок базы -> not_ancestor + merge_base, ask_base", na["sha_state"] == "not_ancestor"
            and na["merge_base"] == c1 and d["ask_base"] is True, na)
    t.check("delta: genre index / decision без якорей — не устарели; reference без якорей — no_anchors",
            not _rec(d, "idx.md")["stale"] and not _rec(d, "dec.md")["stale"]
            and _rec(d, "ref.md")["reason"] == "no_anchors", (_rec(d, "idx.md"), _rec(d, "dec.md"), _rec(d, "ref.md")))
    t.check("delta: файл целиком из policy: без якорей не устарел", not _rec(d, "policy.md")["stale"], _rec(d, "policy.md"))
    t.check("delta: выборка вне дельты детерминирована (seed = sha базы)",
            _run(["delta", "--base", "main"], R.path)[1]["sample"] == d["sample"] and len(d["sample"]) > 0, len(d["sample"]))

    sh = os.path.join(tmp, "shallow")
    subprocess.run(["git", "clone", "-q", "--depth", "1", "file://" + R.path, sh], check=True, env=git_env(), capture_output=True)
    rc, ds = _run(["delta", "--base", "main"], sh)
    t.check("delta: обрезанная история -> shallow_missing, а не «не предок»", _rec(ds, "intact.md")["sha_state"] == "shallow_missing"
            and _rec(ds, "intact.md")["reason"] == "sha_unresolved", _rec(ds, "intact.md"))

    E = _Repo(os.path.join(tmp, "empty"))
    E.put("memory/notes.txt", "x\n")
    E.commit("x")
    rc, ev = _run(["validate"], E.path)
    t.check("validate: memory/ без .md -> exit 2, не «ЧИСТО»", rc == 2 and ev["scan_complete"] is False
            and ev["errors"][0]["code"] == "no_memory_files", (rc, ev["errors"]))

    F = _Repo(os.path.join(tmp, "feat"))
    F.put("docs/features/OSHV-1355-x/plan.md", "# План\n\nStatus: Done\n")
    F.put("docs/features/OSHV-1355-x/.work/log.md", "служебное\n")
    F.put("docs/features/NOTICKET-thing/plan.md", "# План\n\nСтатус: На ревью\n")
    F.put("a.txt", "1\n")
    F.commit("feat: OSHV-13550 другая задача")
    rc, f1 = _run(["features", "--base", "main"], F.path)
    nt = next(i for i in f1["items"] if i["slug"] == "NOTICKET-thing")
    t.check("features: нестандартный статус не отбрасывается; нет тикета -> unknown", nt["artifacts"]["plan.md"]["status"] == "nonstandard:На ревью"
            and nt["ticket"] is None and nt["ticket_source"] == "none" and nt["merge"]["state"] == "unknown", nt)
    it = next(i for i in f1["items"] if i["slug"] == "OSHV-1355-x")
    t.check("features: OSHV-1355 не совпадает с OSHV-13550 -> не влита", it["ticket"] == "OSHV-1355" and it["merge"]["state"] == "unmerged"
            and it["merge"]["base_commits"] == 0, it["merge"])
    t.check("features: служебное (.work/) отделено с размерами", [s["path"] for s in it["service"]] == [".work/log.md"], it["service"])
    F.put("a.txt", "2\n")
    F.commit("fix: OSHV-1355 закрыто")
    it = next(i for i in _run(["features", "--base", "main"], F.path)[1]["items"] if i["slug"] == "OSHV-1355-x")
    t.check("features: тикет в базе + Done -> merged", it["merge"]["state"] == "merged" and it["merge"]["base_commits"] == 1, it["merge"])

    L = os.path.join(tmp, "lostrepo")
    os.makedirs(os.path.join(L, "memory"))
    os.makedirs(os.path.join(tmp, "snap", "before", "memory"))
    old = ("- open: гонка в очереди при повторной доставке, см. `src/queue.ts#Retry`\n"
           "- переехавшая строка про идемпотентность обработчика событий\n"
           "- выкинутый факт про лимит размера вложения в мегабайтах\n")
    with open(os.path.join(tmp, "snap", "before", "memory", "x.md"), "w", encoding="utf-8") as f:
        f.write(old)
    with open(os.path.join(L, "memory", "x.md"), "w", encoding="utf-8") as f:
        f.write("- новая строка\n")
    with open(os.path.join(L, "memory", "y.md"), "w", encoding="utf-8") as f:
        f.write("- переехавшая строка про идемпотентность обработчика событий\n")
    rc, lo = _run(["lost", "--snapshot", os.path.join(tmp, "snap")], L)
    t.check("lost: open: без fixed: -> open_lost без лимита", len(lo["open_lost"]) == 1 and "гонка в очереди" in lo["open_lost"][0]["text"], lo["open_lost"])
    t.check("lost: строка перенесена в другой файл — не кандидат; выкинутая — кандидат",
            [c["text"] for c in lo["candidates"]] == ["- выкинутый факт про лимит размера вложения в мегабайтах"], lo["candidates"])

    snaps = os.path.join(tmp, "snaps-p")
    os.makedirs(os.path.join(snaps, "rZ", "after", "memory"))
    with open(os.path.join(snaps, "rZ", "marker.json"), "w", encoding="utf-8") as f:
        json.dump({"run": "rZ", "state": "done", "started_at": 1}, f)
    with open(os.path.join(snaps, "rZ", "after", "memory", "intact.md"), "w", encoding="utf-8") as f:
        f.write("слово " * 200)
    rc, p = _run(["prepare", "--no-fetch", "--base", "main", "--snapshots-dir", snaps], R.path)
    sus = {(s["file"], s["reason"]) for s in p["overwrite"]["suspects"]}
    t.check("prepare: затирание — файл без frontmatter/checked при большинстве соседей с checked; но не файл под git, равный HEAD "
            "и никогда не имевший checked (legacy)",
            ("memory/plain2.md", "no_frontmatter") in sus and ("memory/plain.md", "no_frontmatter") not in sus
            and ("memory/nochecked.md", "no_frontmatter") not in sus, sus)
    t.check("prepare: файл < 30% своей версии в последнем снимке -> shrunk_vs_snapshot", ("memory/intact.md", "shrunk_vs_snapshot") in sus, sus)
    t.check("prepare: где живёт память, база из аргумента, без сети", p["storage"]["memory"]["tracked"] >= 15
            and p["repo"]["base"]["source"] == "arg" and p["repo"]["base"]["fetch_error"] is None and p["snapshots_dir"] == snaps,
            (p["storage"]["memory"], p["repo"]["base"]))
    rc, pf = _run(["prepare", "--base", "main", "--snapshots-dir", snaps], F.path)
    t.check("prepare: fetch без origin -> fetch_error, работа по локальным ref'ам", pf["repo"]["base"]["fetch_error"]
            and pf["repo"]["base"]["sha"], pf["repo"]["base"])

    W = _Repo(os.path.join(tmp, "baseline"))
    for i in range(4):
        W.put(f"memory/f{i}.md", _mem(f"f{i}", "2026-01-01 @ abcdef1", "- x"))
    W.put("memory/stub.md", "# Заглушка\n\n- коротко\n")
    W.commit("baseline")
    W.put("memory/stub.md", _mem("stub", "2026-01-01 @ abcdef1", "\n".join(f"- факт номер {i} про поведение подсистемы" for i in range(60))))
    W.put("memory/epic-state.md", "# эпик в полёте\n\n- без frontmatter и без checked\n")
    W.put("memory/f0.md", "# заглушка поверх версии с checked\n\n- коротко\n")
    rc, pw = _run(["prepare", "--no-fetch", "--base", "main", "--snapshots-dir", snaps], W.path)
    sw = {(x["file"], x["reason"]) for x in pw["overwrite"]["suspects"]}
    t.check("prepare: в HEAD заглушка без checked, на диске богаче -> disk_richer_than_head (предупреждение, не suspect); "
            "заглушка на диске поверх HEAD с checked — suspect; epic-state не suspect",
            pw["storage"]["memory"]["disk_richer_than_head"] == ["memory/stub.md"]
            and not any(f == "memory/stub.md" for f, _ in sw) and ("memory/f0.md", "baseline_over_existing") in sw
            and not any(f == "memory/epic-state.md" for f, _ in sw), (sw, pw["storage"]["memory"]["disk_richer_than_head"]))
    H = _Repo(os.path.join(tmp, "hist"))
    H.put("src/a.ts", "1\n")
    h1 = H.commit("c1")
    H.put("src/a.ts", "2\n")
    h2 = H.commit("c2")
    for n in ("g0", "g1", "g2", "hist", "disk"):
        H.put(f"memory/{n}.md", _mem(n, f"2026-01-02 @ {h2[:12]}", "- x"))
    H.commit("memory")
    H.put("memory/hist.md", _mem("hist", f"2026-01-01 @ {h1[:12]}", "- x"))
    H.commit("откат checked")
    H.put("memory/disk.md", _mem("disk", f"2026-01-01 @ {h1[:12]}", "- x"))  # не закоммичено
    rc, ph = _run(["prepare", "--no-fetch", "--base", "main", "--snapshots-dir", snaps], H.path)
    rs = {(x["file"], x["reason"]) for x in ph["overwrite"]["suspects"]}
    t.check("prepare: checked откатился на предка — в истории git и на диске против HEAD",
            ("memory/hist.md", "regressed_checked") in rs and ("memory/disk.md", "regressed_checked") in rs, rs)

    rc, g = _run(["all", "--base", "main"], E.path)
    t.check("all: нет .md в memory/ -> scan_complete=false, exit 2, delta present:false",
            rc == 2 and g["scan_complete"] is False and g["delta"]["present"] is False, (rc, g["errors"]))
    G = _Repo(os.path.join(tmp, "green"))
    G.put("src/a.ts", "export const KEEP = 1;\n")
    g1 = G.commit("code")
    G.put("memory/MEMORY.md", _mem("MEMORY", f"2026-01-01 @ {g1[:12]}", "- `src/a.ts#KEEP` индекс"))
    G.put("memory/ok.md", _mem("ok", f"2026-01-01 @ {g1[:12]}", "- `src/a.ts#KEEP` константа"))
    G.commit("memory")
    rc, ga = _run(["all", "--base", "main"], G.path)
    t.check("зелёный эталон: exit 0, ничего не устарело, замечаний нет", rc == 0 and ga["scan_complete"] is True
            and not any(f["stale"] for f in ga["delta"]["files"]) and ga["validate"]["problems"] == []
            and ga["delta"]["ask_base"] is False and ga["features"]["present"] is False, (rc, ga["errors"], ga["validate"]["problems"]))
    rc, gv = _run(["validate"], G.path)
    t.check("зелёный эталон: validate exit 0", rc == 0, (rc, gv["problems"], gv["budgets"]["roles"]))
    rc, nb = _run(["delta"], G.path)
    t.check("delta без базы и origin/HEAD -> exit 2 с действием", rc == 2 and nb["errors"][0]["code"] == "base_unresolved"
            and "--base" in nb["errors"][0]["fix"], nb["errors"])

    os.makedirs(snaps, exist_ok=True)
    with open(os.path.join(snaps, ".lock"), "w", encoding="utf-8") as f:
        json.dump({"session": "чужая", "pid": 1, "started_at": NOW - 3600, "run": "r0"}, f)
    t.check("prepare: живой чужой lock моложе 6 ч -> locked", _run(["prepare", "--no-fetch", "--base", "main", "--snapshots-dir", snaps], G.path)[1]["lock"]["state"] == "locked")
    with open(os.path.join(snaps, ".lock"), "w", encoding="utf-8") as f:
        json.dump({"session": "чужая", "pid": 1, "started_at": NOW - 7 * 3600, "run": "r0"}, f)
    t.check("prepare: lock старше 6 ч не держит", _run(["prepare", "--no-fetch", "--base", "main", "--snapshots-dir", snaps], G.path)[1]["lock"]["state"] == "free")
    with open(os.path.join(snaps, ".lock"), "w", encoding="utf-8") as f:
        json.dump({"session": "чужая", "pid": 1, "started_at": NOW - 60, "run": "r0"}, f)
    rc, sl = _run(["snapshot", "--run", "rx", "--snapshots-dir", snaps], G.path)
    t.check("snapshot: чужой живой lock блокирует новый прогон (exit 2, действие в fix)", rc == 2
            and sl["errors"][0]["code"] == "locked" and "--force-lock" in sl["errors"][0]["fix"], sl["errors"])
    rc, sl = _run(["snapshot", "--run", "rx", "--snapshots-dir", snaps, "--force-lock"], G.path)
    t.check("snapshot --force-lock: продолжение по решению пользователя", rc == 0 and sl["checkpoint"] is False, sl)
    shutil.rmtree(snaps)

    S = os.path.join(tmp, "snaps-s")
    rc, s1 = _run(["snapshot", "--run", "r1", "--snapshots-dir", S], G.path)
    t.check("snapshot: копия до, маркер, отпечаток, каталог 700", rc == 0 and os.path.isfile(os.path.join(S, "r1", "before", "memory", "ok.md"))
            and s1["fingerprint"] and (os.stat(S).st_mode & 0o777) == 0o700, s1)
    pr = _run(["prepare", "--no-fetch", "--base", "main", "--snapshots-dir", S], G.path)[1]
    t.check("prepare: незавершённый прогон -> resume с совпавшим отпечатком", pr["resume"]["present"] and pr["resume"]["run"] == "r1"
            and pr["resume"]["fingerprint_matches"] is True, pr["resume"])
    with open(os.path.join(G.path, "memory", "ok.md"), "a", encoding="utf-8") as f:
        f.write("- правка\n")
    pr = _run(["prepare", "--no-fetch", "--base", "main", "--snapshots-dir", S], G.path)[1]
    t.check("prepare: память изменилась после маркера -> отпечаток не совпал", pr["resume"]["fingerprint_matches"] is False, pr["resume"])
    s2 = _run(["snapshot", "--run", "r1", "--snapshots-dir", S], G.path)[1]
    t.check("snapshot: повторный на существующем прогоне — чекпоинт, «до» не перезаписан",
            s2["checkpoint"] is True and "правка" not in read(os.path.join(S, "r1", "before", "memory", "ok.md")), s2)
    _run(["snapshot", "--after", "--run", "r1", "--snapshots-dir", S], G.path)
    pr = _run(["prepare", "--no-fetch", "--base", "main", "--snapshots-dir", S], G.path)[1]
    t.check("snapshot --after закрывает прогон: resume нет, lock снят",
            pr["resume"]["present"] is False and pr["lock"]["state"] == "free" and os.path.isdir(os.path.join(S, "r1", "after")), pr["resume"])
    for i in range(2, 7):
        NOW += 100
        _run(["snapshot", "--run", f"r{i}", "--snapshots-dir", S, "--now", str(NOW)], G.path)
        _run(["snapshot", "--after", "--run", f"r{i}", "--snapshots-dir", S, "--now", str(NOW)], G.path)
    kept = sorted(n for n in os.listdir(S) if not n.startswith("."))
    t.check("snapshot: ротация — 3 последних завершённых", kept == ["r4", "r5", "r6"], kept)

    cfg = os.path.join(tmp, "cfg")
    t.check("automemory: экранирование пути как в рантайме (не-[A-Za-z0-9] -> '-', длинные — хэш)",
            sanitize_path("/Users/u/x-y (z)") == "-Users-u-x-y--z-"
            and sanitize_path("/Users/u/a" + "a" * 250).endswith("-18gk35")
            and sanitize_path("/Users/u/" + "очень-длинный-каталог/" * 12 + "repo").endswith("-ra2p0m"))
    am = _run(["automemory", "--automemory-dir", os.path.join(tmp, "нет")], G.path)[1]
    t.check("automemory: каталога нет -> not_found, а не «пусто»", am["status"] == "not_found", am["status"])
    amd = os.path.join(tmp, "am")
    os.makedirs(amd)
    with open(os.path.join(amd, "MEMORY.md"), "w", encoding="utf-8") as f:
        f.write("".join(f"- строка {i}\n" for i in range(250)) + "[x](нет.md)\n")
    ids = " ".join(["OSHV-1", "OSHV-2", "OSHV-3", "OSHV-4", "OSHV-5"])
    with open(os.path.join(amd, "dup.md"), "w", encoding="utf-8") as f:
        f.write(f"- заметка {ids}\n")
    with open(os.path.join(G.path, "memory", "ok.md"), "a", encoding="utf-8") as f:
        f.write(f"- {ids}\n")
    am = _run(["automemory", "--automemory-dir", amd], G.path)[1]
    t.check("automemory: индекс сверх 200 строк -> over, битая ссылка; идентификаторы в проекте -> dup_candidate",
            am["index"]["over"] and am["index"]["broken_links"] == ["нет.md"] and am["files"][0]["class"] == "dup_candidate", am)
    os.environ["CLAUDE_CONFIG_DIR"] = cfg
    os.makedirs(os.path.join(G.path, ".claude"), exist_ok=True)
    with open(os.path.join(G.path, ".claude", "settings.json"), "w", encoding="utf-8") as f:
        f.write('{"autoMemoryDirectory": "/x"}')
    am = _run(["automemory"], G.path)[1]
    t.check("automemory: путь переопределён настройками -> override_unresolved, не угадываем", am["status"] == "override_unresolved", am["status"])
    nomem = os.path.join(tmp, "nomem")
    os.makedirs(os.path.join(nomem, "memory"))
    rc, _ = _run(["validate"], nomem)
    t.check("validate: memory/ без файлов и без git -> exit 2", rc == 2, rc)


def _words(n):
    return " ".join(f"слово{i}" for i in range(n))


def _selftest_round2(tmp, t):
    """Целостность данных: stamp, lost по единицам, снимки и lock, детектор затирания, версии памяти."""
    global NOW
    NOW = 1782864000

    # ---- delta: n по файлам, epic-state, branch_only
    N = _Repo(os.path.join(tmp, "nochk"))
    N.put("src/a.ts", "export const KEEP = 1;\n")
    N.commit("code")
    N.put("memory/only.md", _mem("only", None, "- утверждение один про код без якоря\n- утверждение два про код без якоря\n- утверждение три про код без якоря"))
    N.put("memory/epic-state.md", _mem("epic-state", None, "- эпик в полёте без якоря на код проекта"))
    N.commit("memory")
    rc, d = _run(["delta", "--base", "main"], N.path)
    t.check("delta: единственный no_checked-файл даёт n > 0 (stale_file_claims_total), epic-state исключён",
            d["stale_file_claims_total"] == 3 and d["stale_claims_total"] == 0 and d["files_total"] == 1
            and d["files"][0]["reason"] == "no_checked", (d["stale_file_claims_total"], d["files_total"]))
    rc, vn = _run(["validate"], N.path)
    t.check("validate: epic-state.md не требует checked (рабочий файл эпика)",
            not any(p["file"] == "memory/epic-state.md" and p["detail"] == "checked" for p in vn["problems"])
            and any(p["file"] == "memory/only.md" and p["detail"] == "checked" for p in vn["problems"]), vn["problems"][:4])
    B = _Repo(os.path.join(tmp, "branch"))
    B.put("src/a.ts", "export const KEEP = 1;\n")
    b1 = B.commit("c1")
    B.sh("checkout", "-q", "-b", "feat")
    B.put("src/newf.ts", "export const N = 1;\n")
    B.put("memory/bo.md", _mem("bo", f"2026-01-01 @ {b1[:12]}", "- `src/newf.ts#N` константа с ветки\n- `src/a.ts#KEEP` константа"))
    B.commit("feat")
    rc, d = _run(["delta", "--base", "main"], B.path)
    r = _rec(d, "bo.md")
    t.check("delta: якоря нет в базе, но есть в HEAD -> branch_only, не удаление и не stale",
            not r["stale"] and r["claims"]["branch_only"] == 1 and r["stale_anchors"] == []
            and [u["ref"] for u in r["unverified_anchors"]] == ["src/newf.ts#N"], r)

    Ne = _Repo(os.path.join(tmp, "notanc"))
    Ne.put("src/keep.ts", "export const K = 1;\n")
    Ne.put("src/gone.ts", "export const G = 1;\n")
    n1 = Ne.commit("c1")
    Ne.sh("checkout", "-q", "-b", "memb")
    Ne.put("src/side.ts", "export const Side = 1;\n")
    side = Ne.commit("side")
    Ne.sh("checkout", "-q", "main")
    os.remove(os.path.join(Ne.path, "src/gone.ts"))
    Ne.commit("main: удалён gone.ts")
    Ne.put("memory/na.md", _mem("na", f"2026-01-01 @ {side[:12]}", "- `src/side.ts#Side` добавлен на ветке памяти\n- `src/gone.ts#G` удалён в базе\n- `src/keep.ts#K` цел"))
    rc, dn = _run(["delta", "--base", "main"], Ne.path)
    rn_ = _rec(dn, "na.md")
    t.check("delta: sha не предок — путь из дерева checked-sha, которого нет в базе и не было в merge-base -> branch_only, а не deleted; "
            "удалённый в базе (был в merge-base) — deleted",
            rn_["sha_state"] == "not_ancestor" and [u["ref"] for u in rn_["unverified_anchors"]] == ["src/side.ts#Side"]
            and [(x["ref"], x["state"]) for x in rn_["stale_anchors"]] == [("src/gone.ts#G", "deleted")], rn_)

    Jb = _Repo(os.path.join(tmp, "j3"))
    Jb.put("src/auth.ts", "export class AuthGuard {}\n")
    Jb.commit("i")
    Jb.sh("checkout", "-q", "-b", "feature")
    Jb.put("src/auth.ts", "export class AuthGuard {}\nexport function impersonate() {}\n")
    fs_ = Jb.commit("wip")
    Jb.put("memory/inv.md", _mem("inv", f"2026-01-05 @ {fs_[:12]}",
                                 "- лимит: `src/auth.ts#impersonate`\n- проверка: `src/auth.ts#AuthGuard`"))
    rc, dj = _run(["delta", "--base", "main"], Jb.path)
    rj = _rec(dj, "inv.md")
    t.check("delta: символ добавлен на ветке памяти в файл, который в базе есть (база файл с развилки не трогала) -> branch_only, не symbol_missing",
            rj["sha_state"] == "not_ancestor" and not rj["stale"] and rj["stale_anchors"] == []
            and "src/auth.ts#impersonate" in [u["ref"] for u in rj["unverified_anchors"]], rj)

    Lg = _Repo(os.path.join(tmp, "legacy"))
    body_ = "\n".join(f"- legacy правило {i}: изоляция арендаторов для биллинга и возвратов" for i in range(60))
    for n in ("invariants", "decisions", "conventions", "gotchas"):
        Lg.put(f"memory/{n}.md", f"---\nname: {n}\ndescription: d\n---\n# {n}\n{body_}\n")
    Lg.commit("legacy без checked")
    Lg.put("memory/invariants.md", _mem("invariants", "2026-01-05 @ abcdef1", "- заглушка после сбойного прогона"))
    rc, pl = _run(["prepare", "--no-fetch", "--base", "main", "--snapshots-dir", os.path.join(tmp, "snaps-lg")], Lg.path)
    t.check("prepare: legacy без checked в HEAD (728 слов) заменён на диске короткой заглушкой С checked -> suspect, а не disk_richer",
            ("memory/invariants.md", "baseline_over_existing") in {(x["file"], x["reason"]) for x in pl["overwrite"]["suspects"]}
            and "memory/invariants.md" not in pl["storage"]["memory"]["disk_richer_than_head"], (pl["overwrite"], pl["storage"]["memory"]["disk_richer_than_head"]))

    # ---- stamp
    R = _Repo(os.path.join(tmp, "stampr"))
    R.put("src/a.ts", "export const KEEP = 1;\n")
    R.put("src/b.ts", "export function gone() {}\n")
    s1 = R.commit("code")
    ck = f"2026-01-01 @ {s1[:12]}"
    R.put("memory/intact.md", _mem("intact", ck, "- `src/a.ts#KEEP` константа"))
    R.put("memory/sym.md", _mem("sym", ck, "- `src/b.ts#gone` функция"))
    R.put("memory/nochk.md", _mem("nochk", None, "- `src/a.ts#KEEP` константа"))
    R.put("memory/wt.md", _mem("wt", ck, "- `src/wt.ts#Wt` только на диске"))
    R.commit("memory")
    R.put("src/b.ts", "export const other = 1;\n")
    head = R.commit("mutation")
    R.put("src/wt.ts", "export const Wt = 1;\n")
    SS = os.path.join(tmp, "snaps-stamp")
    _run(["snapshot", "--run", "st1", "--snapshots-dir", SS], R.path)
    # писатель переякорил устаревший файл на нетронутый символ: по диску он уже «не устарел», но правка не проверена
    R.put("memory/sym.md", _mem("sym", ck, "- `src/a.ts#KEEP` переякорено писателем"))
    sym_before = read(os.path.join(R.path, "memory/sym.md"))
    before_dir = os.path.join(SS, "st1", "before")
    rc0, st0 = _run(["stamp", "--sha", head, "--intact"], R.path)
    t.check("stamp --intact без --snapshot — ошибка bad_args (exit 2)", rc0 == 2 and st0["errors"][0]["code"] == "bad_args", st0["errors"])
    rc, st = _run(["stamp", "--sha", head, "--files", "memory/nochk.md", "src/a.ts", "memory/ghost.md", "memory/epic-state.md", "--intact",
                   "--snapshot", before_dir], R.path)
    got = {x["file"]: x["changed"] for x in st["stamped"]}
    sk = {x["file"]: x["reason"] for x in st["skipped"]}
    want = f"{today()} @ {head[:9]}"
    fm = lambda n: fm_keys(split_fm(read(os.path.join(R.path, "memory", n)))[0])
    t.check("stamp: checked/updated ставятся сверенным файлам (в т. ч. вставкой при отсутствии), --intact — только stale:false",
            rc == 0 and st["checked"] == want and got == {"memory/nochk.md": True, "memory/intact.md": True}
            and fm("nochk.md")["checked"] == want and fm("intact.md")["checked"] == want and fm("intact.md")["updated"] == today()
            and read(os.path.join(R.path, "memory/sym.md")) == sym_before, (st, got))
    t.check("stamp: пропуски с причиной — не memory, нет файла, epic-state, непроверяемые якоря (branch_only)",
            sk == {"src/a.ts": "not_memory_file", "memory/ghost.md": "missing", "memory/epic-state.md": "epic_state",
                   "memory/wt.md": "unverified_anchors", "memory/sym.md": "changed_in_run"}, sk)
    rc, stw = _run(["stamp", "--sha", head, "--files", "memory/wt.md"], R.path)
    t.check("stamp --files: файл с непроверяемыми якорями (branch_only) не штампуется — то же правило, что у --intact",
            stw["stamped"] == [] and stw["skipped"] == [{"file": "memory/wt.md", "reason": "unverified_anchors"}], stw)
    rc, st2 = _run(["stamp", "--sha", head, "--files", "memory/intact.md"], R.path)
    t.check("stamp: повтор идемпотентен; неразрешимый sha -> exit 2", st2["stamped"] == [{"file": "memory/intact.md", "changed": False}]
            and _run(["stamp", "--sha", "deadbeef", "--files", "memory/intact.md"], R.path)[0] == 2, st2)

    # ---- снимки: закрытые прогоны, abandon, lock по сессии, другой корень, auto-memory, каталоги без коллизий
    G = _Repo(os.path.join(tmp, "snapr"))
    G.put("memory/big.md", _mem("big", None, "- " + _words(10)))
    G.put("memory/f.md", _mem("f", None, "- ф"))
    G.commit("memory")
    S = os.path.join(tmp, "snaps-r2")
    amd = os.path.join(tmp, "am2")
    os.makedirs(amd)
    with open(os.path.join(amd, "n.md"), "w", encoding="utf-8") as f:
        f.write("- заметка auto-memory\n")
    rc, s1_ = _run(["snapshot", "--run", "a1", "--snapshots-dir", S, "--automemory-dir", amd], G.path)
    t.check("snapshot: auto-memory копируется (automemory_copied/status), каталога нет -> not_found",
            s1_["automemory_copied"] is True and s1_["automemory_status"] == "ok"
            and os.path.isfile(os.path.join(S, "a1", "before", "automemory", "n.md"))
            and _run(["snapshot", "--run", "a0", "--snapshots-dir", S, "--automemory-dir", os.path.join(tmp, "нет-такого")], G.path)[1]["automemory_status"] == "not_found", s1_)
    pr = _run(["prepare", "--no-fetch", "--base", "main", "--snapshots-dir", S], G.path)[1]
    t.check("prepare: lock своей сессии -> mine; чужой session id -> locked", pr["lock"]["state"] == "mine"
            and _with_session("другая", lambda: _run(["prepare", "--no-fetch", "--base", "main", "--snapshots-dir", S], G.path)[1]["lock"]["state"]) == "locked", pr["lock"])
    rc, blk = _with_session("другая", lambda: _run(["snapshot", "--run", "a2", "--snapshots-dir", S], G.path))
    t.check("snapshot: новый прогон чужой сессии при живом lock блокируется", rc == 2 and blk["errors"][0]["code"] == "locked", blk["errors"])
    _run(["snapshot", "--abandon", "--run", "a0", "--snapshots-dir", S], G.path)
    _run(["snapshot", "--after", "--run", "a1", "--snapshots-dir", S], G.path)
    rc, rd_ = _run(["snapshot", "--run", "a1", "--snapshots-dir", S], G.path)
    t.check("snapshot: повторный --run на закрытом прогоне -> run_done, «до» не подменяется", rc == 2 and rd_["errors"][0]["code"] == "run_done"
            and "-2" in rd_["errors"][0]["fix"], rd_["errors"])
    rc, ab = _run(["snapshot", "--run", "b1", "--snapshots-dir", S], G.path)
    rc, ab = _run(["snapshot", "--abandon", "--run", "b1", "--snapshots-dir", S], G.path)
    pr = _run(["prepare", "--no-fetch", "--base", "main", "--snapshots-dir", S], G.path)[1]
    t.check("snapshot --abandon: маркер abandoned, lock снят, resume не предлагается", ab["state"] == "abandoned" and ab["released_lock"] is True
            and pr["resume"]["present"] is False and pr["lock"]["state"] == "free", (ab, pr["resume"], pr["lock"]))
    _run(["snapshot", "--run", "c1", "--snapshots-dir", S], G.path)
    mp = os.path.join(S, "c1", "marker.json")
    mk = load_json(mp)
    t.check("snapshot: в маркер пишутся корень дерева и ветка", mk["root"] in (os.path.realpath(G.path), G.path) and mk.get("branch") == "main", mk)
    mk["root"] = "/elsewhere/tree"
    write_json(mp, mk)
    pr = _run(["prepare", "--no-fetch", "--base", "main", "--snapshots-dir", S], G.path)[1]
    rc, fo = _run(["snapshot", "--run", "c1", "--snapshots-dir", S], G.path)
    rc2, fa = _run(["snapshot", "--after", "--run", "c1", "--snapshots-dir", S], G.path)
    t.check("resume из другого корня отклоняется: present=false, foreign, snapshot и --after -> run_foreign",
            pr["resume"]["present"] is False and pr["resume"]["foreign"][0]["run"] == "c1" and rc == 2 and rc2 == 2
            and fo["errors"][0]["code"] == "run_foreign" and fa["errors"][0]["code"] == "run_foreign", (pr["resume"], fo["errors"], fa["errors"]))
    a_info, b_info, c_info = ({"main_worktree": p} for p in ("/x/проект", "/x/работа", "/x/a.b"))
    d_info = {"main_worktree": "/x/a_b"}
    dirs = [snapshots_dir(i, None) for i in (a_info, b_info, c_info, d_info)]
    t.check("каталог снимков различает пути, которые экранирование склеивает (хэш реального пути)", len(set(dirs)) == 4, dirs)
    # ротация не удаляет единственную хорошую копию
    S3 = os.path.join(tmp, "snaps-rot")
    for i in range(1, 6):
        rd = os.path.join(S3, f"r{i}")
        os.makedirs(os.path.join(rd, "after", "memory"))
        write_json(os.path.join(rd, "marker.json"), {"run": f"r{i}", "state": "done", "started_at": i, "root": G.path})
        with open(os.path.join(rd, "after", "memory", "big.md"), "w", encoding="utf-8") as f:
            f.write(_words(200 if i == 1 else 10))
    _run(["snapshot", "--run", "r9", "--snapshots-dir", S3], G.path)
    rc, rot = _run(["snapshot", "--after", "--run", "r9", "--snapshots-dir", S3], G.path)
    t.check("ротация: снимок с файлом, съёжившимся на диске, сохраняется (rotation_blocked), прочие лишние удаляются",
            sorted(n for n in os.listdir(S3) if not n.startswith(".")) == ["r1", "r4", "r5", "r9"]
            and [b["run"] for b in rot["rotation_blocked"]] == ["r1"], (rot, sorted(os.listdir(S3))))
    pr = _run(["prepare", "--no-fetch", "--base", "main", "--snapshots-dir", S3], G.path)[1]
    t.check("prepare: детектор затирания сверяется со всеми снимками (не только последним) и видит исчезнувший файл",
            ("memory/big.md", "shrunk_vs_snapshot") in {(x["file"], x["reason"]) for x in pr["overwrite"]["suspects"]}, pr["overwrite"])
    with open(os.path.join(S3, "r9", "after", "memory", "ghost2.md"), "w", encoding="utf-8") as f:
        f.write(_words(60))
    pr = _run(["prepare", "--no-fetch", "--base", "main", "--snapshots-dir", S3], G.path)[1]
    t.check("prepare: файл из последнего завершённого снимка, которого нет на диске -> vanished",
            ("memory/ghost2.md", "vanished") in {(x["file"], x["reason"]) for x in pr["overwrite"]["suspects"]}, pr["overwrite"])
    os.remove(os.path.join(G.path, "memory", "f.md"))
    pr = _run(["prepare", "--no-fetch", "--base", "main", "--snapshots-dir", S3], G.path)[1]
    t.check("prepare: исчезнувший файл (есть в HEAD, на диске нет) -> vanished; изменения относительно HEAD -> modified",
            ("memory/f.md", "vanished") in {(x["file"], x["reason"]) for x in pr["overwrite"]["suspects"]}
            and "memory/f.md" in pr["storage"]["memory"]["modified"], (pr["overwrite"], pr["storage"]["memory"]["modified"]))

    # ротация защищает и auto-memory снимка (она вне git: копия в снимке — единственная)
    S4 = os.path.join(tmp, "snaps-am")
    amx = os.path.join(tmp, "am-rot")
    os.makedirs(amx)
    with open(os.path.join(amx, "n.md"), "w", encoding="utf-8") as f:
        f.write(_words(10))
    for i in range(1, 6):
        rd = os.path.join(S4, f"r{i}")
        os.makedirs(os.path.join(rd, "after", "automemory"))
        write_json(os.path.join(rd, "marker.json"), {"run": f"r{i}", "state": "done", "started_at": i, "root": G.path})
        with open(os.path.join(rd, "after", "automemory", "n.md"), "w", encoding="utf-8") as f:
            f.write(_words(200 if i == 1 else 10))
    _run(["snapshot", "--run", "r9", "--snapshots-dir", S4, "--automemory-dir", amx], G.path)
    rc, rot4 = _run(["snapshot", "--after", "--run", "r9", "--snapshots-dir", S4, "--automemory-dir", amx], G.path)
    t.check("ротация: снимок с файлом auto-memory, съёжившимся в текущем каталоге, сохраняется",
            sorted(n for n in os.listdir(S4) if not n.startswith(".")) == ["r1", "r4", "r5", "r9"]
            and [(b["run"], b["file"]) for b in rot4["rotation_blocked"]] == [("r1", "automemory/n.md")], rot4)

    # ---- свободный id прогона
    SI = os.path.join(tmp, "snaps-id")
    gsha = G.sh("rev-parse", "HEAD")[:9]
    free = lambda: _run(["prepare", "--no-fetch", "--base", "main", "--snapshots-dir", SI], G.path)[1]["run"]["free_id"]
    fid = free()
    _run(["snapshot", "--run", fid, "--snapshots-dir", SI], G.path)
    fid2 = free()
    os.makedirs(os.path.join(SI, fid2, "before"))  # каталог без маркера, но со снимком — тоже занят
    t.check("prepare: run.free_id = <дата>-<sha9 базы>; занятые маркером или снимком id дают суффикс -2, -3",
            fid == f"{today()}-{gsha}" and fid2 == f"{fid}-2" and free() == f"{fid}-3", (fid, fid2))

    # ---- версии памяти: надмножество -> newer_versions, иначе differs; HEAD против базы
    V = _Repo(os.path.join(tmp, "versions"))
    for i in range(3):
        V.put(f"memory/s{i}.md", _mem(f"s{i}", "2026-01-01 @ abcdef1", "- строка про подсистему номер один в проекте\n- строка про подсистему номер два в проекте"))
    V.put("memory/p.md", _mem("p", "2026-01-01 @ abcdef1", "- первая строка про поведение подсистемы в проекте\n- вторая строка про поведение подсистемы в проекте"))
    V.commit("base")
    V.sh("checkout", "-q", "-b", "br1")
    V.put("memory/p.md", _mem("p", "2026-01-05 @ abcdef1", "- первая строка про поведение подсистемы в проекте\n- вторая строка про поведение подсистемы в проекте\n- третья строка только на ветке br1 про подсистему"))
    V.commit("br1")
    V.sh("checkout", "-q", "main")
    V.sh("checkout", "-q", "-b", "br2")
    V.put("memory/p.md", _mem("p", "2026-01-06 @ abcdef1", "- совсем другая строка про поведение подсистемы на ветке br2"))
    V.commit("br2")
    V.sh("checkout", "-q", "main")
    rc, pv = _run(["prepare", "--no-fetch", "--base", "br1", "--snapshots-dir", os.path.join(tmp, "snaps-v")], V.path)
    m_ = pv["storage"]["memory"]
    t.check("prepare: новее диска и надмножество -> newer_versions; новее, но теряет строки диска -> differs со сводкой",
            [x["ref_or_path"] for x in m_["newer_versions"]] == ["br1"]
            and [(x["ref_or_path"], x["lines_only_on_disk"] > 0) for x in m_["differs"]] == [("br2", True)], (m_["newer_versions"], m_["differs"]))
    hb = m_["head_vs_base"]
    t.check("prepare: head_vs_base — деревья memory HEAD и базы, ahead/behind", hb["same"] is False and hb["ahead"] == 0 and hb["behind"] == 1, hb)
    # окно истории шире двух коммитов; подкаталоги
    W = _Repo(os.path.join(tmp, "window"))
    for i in range(4):
        W.put(f"memory/w{i}.md", _mem(f"w{i}", "2026-01-01 @ abcdef1", "- x"))
    W.put("memory/f.md", _mem("f", "2026-01-01 @ abcdef1", "- " + _words(60)))
    W.commit("rich")
    W.put("memory/f.md", "# заглушка\n\n- коротко\n")
    W.commit("baseline поверх")
    for i in range(4):
        W.put("memory/f.md", f"# заглушка\n\n- коротко {i}\n")
        W.commit(f"правка {i}")
    W.put("memory/sub/z.md", "# без frontmatter в подкаталоге\n\n- x\n")
    rc, pw = _run(["prepare", "--no-fetch", "--base", "main", "--snapshots-dir", os.path.join(tmp, "snaps-w")], W.path)
    sw = {(x["file"], x["reason"]) for x in pw["overwrite"]["suspects"]}
    t.check("prepare: затирание найдено за окном в 2 коммита; подкаталоги проверяются",
            ("memory/f.md", "baseline_over_existing") in sw and ("memory/sub/z.md", "no_frontmatter") in sw, sw)

    # ---- features: локальная невлитая ветка, slug с суффиксом
    F = _Repo(os.path.join(tmp, "feat2"))
    F.put("docs/features/OSHV-1500-payments/plan.md", "# План\n\nStatus: Done\n")
    F.put("docs/features/OSHV-1500-payments-2/plan.md", "# План\n\nStatus: Done\n")
    F.put("a.txt", "1\n")
    F.commit("feat: OSHV-1500 платежи")
    F.sh("checkout", "-q", "-b", "feature/OSHV-1500-payments")
    F.put("a.txt", "2\n")
    F.commit("wip")
    F.sh("checkout", "-q", "main")
    its = {i["slug"]: i for i in _run(["features", "--base", "main"], F.path)[1]["items"]}
    t.check("features: невлитая локальная ветка с тикетом -> unmerged", its["OSHV-1500-payments"]["merge"]["state"] == "unmerged"
            and its["OSHV-1500-payments"]["merge"]["unmerged_branches"] == ["feature/OSHV-1500-payments"], its["OSHV-1500-payments"]["merge"])
    t.check("features: slug с суффиксом -2 -> slug_ambiguous, merge unknown", its["OSHV-1500-payments-2"]["ticket_source"] == "slug_ambiguous"
            and its["OSHV-1500-payments-2"]["merge"]["state"] == "unknown", its["OSHV-1500-payments-2"])

    # ---- lost: единицы, точное совпадение, open:, docs/features, automemory, одобренные удаления, shrink
    L = os.path.join(tmp, "lost2")
    snap = os.path.join(tmp, "snap2")
    os.makedirs(os.path.join(L, "memory", "journal"))
    os.makedirs(os.path.join(snap, "before", "memory"))
    os.makedirs(os.path.join(snap, "before", "docs", "features", "s1", ".work"))
    os.makedirs(os.path.join(snap, "before", "automemory"))

    def w(base, rel, text):
        p = os.path.join(base, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)

    prose = "Абзац прозы про порядок обработки повторной доставки события:\nсначала идемпотентность, затем ретрай с бэкоффом."
    w(snap, "before/memory/a.md", f"{prose}\n\n- длинная строка про идемпотентность обработчика событий очереди\n"
      "- open: баг в очереди при повторной доставке, см. `src/q.ts#Retry` и `src/q.ts#Ack`\n"
      "- open: OSHV-77 гонка при доставке события в очереди воркера\n"
      "- open: перенесённый в журнал живой дефект про блокировку строк\n"
      "- перенесённый в журнал закрытый факт про миграцию индекса таблицы\n")
    w(snap, "before/docs/features/s1/plan.md", "# План\n\n- Принятый риск: откат миграции невозможен без простоя сервиса\n")
    w(snap, "before/docs/features/s1/.work/log.md", "- служебный лог прогона проверки, который удалили по пакету архивации\n")
    w(snap, "before/docs/features/s1/.work/keep.md", "- служебный файл, который не менялся и остаётся на месте после прогона\n")
    w(L, "docs/features/s1/.work/keep.md", "- служебный файл, который не менялся и остаётся на месте после прогона\n")
    w(snap, "before/automemory/n.md", "- факт auto-memory, который перенесён в память проекта дословно\n- факт auto-memory, которого больше нигде нет\n")
    w(L, "memory/a.md", "- длинная строка про идемпотентность обработчика событий очереди и ещё хвост\n"
      "- fixed: `src/q.ts#Retry` и `src/q.ts#Ack` исправлено в abc1234\n- fixed: OSHV-77 совсем другая история\n")
    w(L, "memory/journal/j.md", "- open: перенесённый в журнал живой дефект про блокировку строк\n"
      "- перенесённый в журнал закрытый факт про миграцию индекса таблицы\n")
    w(L, "memory/other.md", "- факт auto-memory, который перенесён в память проекта дословно\n")
    am_cur = os.path.join(tmp, "am-cur")
    w(am_cur, "n.md", "- факт auto-memory, который перенесён в память проекта дословно\n")
    old_cwd = os.getcwd()
    rc, lo = _run(["lost", "--snapshot", snap], L)
    texts = [c["text"] for c in lo["candidates"]]
    ol = [o["text"] for o in lo["open_lost"]]
    t.check("lost: единица — абзац; совпадение точное, не подстрока (строка внутри длинной — кандидат)",
            any(x.startswith("Абзац прозы") for x in texts) and "- длинная строка про идемпотентность обработчика событий очереди" in texts, texts)
    t.check("lost: open: в journal/ не снимает потерю; fixed: снимает только с тем же якорем, а не по общему тикету",
            any("блокировку строк" in x for x in ol) and any("OSHV-77" in x for x in ol) and not any("Retry" in x for x in ol)
            and not any("закрытый факт" in x for x in texts), (ol, texts))
    t.check("lost: docs/features входит в сравнение; служебное без --expect-deleted — кандидат",
            any(c["file"] == "docs/features/s1/plan.md" for c in lo["candidates"])
            and any(c["file"] == "docs/features/s1/.work/log.md" for c in lo["candidates"]), [c["file"] for c in lo["candidates"]])
    t.check("lost: неизменённый служебный файл фичи (.work/) не даёт кандидатов — текущие файлы целиком в корпусе",
            not any(c["file"].endswith("keep.md") for c in lo["candidates"]), [c["file"] for c in lo["candidates"]])
    rc, lo2 = _run(["lost", "--snapshot", snap, "--expect-deleted", "docs/features/s1/.work"], L)
    t.check("lost --expect-deleted: одобренное удаление каталога не кандидат", not any(".work" in c["file"] for c in lo2["candidates"]), lo2["candidates"])
    rc, la = _run(["lost", "--snapshot", snap, "--tree", "automemory", "--automemory-dir", am_cur], L)
    t.check("lost --tree automemory: перенесённое в проект — не потеря, остальное — кандидат",
            [c["text"] for c in la["candidates"]] == ["- факт auto-memory, которого больше нигде нет"] and la["candidates"][0]["file"] == "automemory/n.md", la["candidates"])
    snap5 = os.path.join(tmp, "snap5")
    am5 = os.path.join(tmp, "am5")
    os.makedirs(am5)
    w(snap5, "before/automemory/gone.md", "- факт auto-memory, который перенесён в память проекта дословно\n- " + _words(45) + " уникальный хвост\n")
    rc, e1 = _run(["lost", "--snapshot", snap5, "--tree", "automemory", "--automemory-dir", am5, "--expect-deleted", "automemory/gone.md"], L)
    rc, e2 = _run(["lost", "--snapshot", snap5, "--tree", "automemory", "--automemory-dir", am5], L)
    t.check("lost --tree automemory --expect-deleted: одобренное удаление гасит только тревогу сжатия, перенос единиц проверяется",
            e1["shrink_alarm"] is False and e1["shrunk_files"] == [] and len(e1["candidates"]) == 1
            and "уникальный хвост" in e1["candidates"][0]["text"]
            and e2["shrink_alarm"] is True and [x["file"] for x in e2["shrunk_files"]] == ["automemory/gone.md"], (e1["candidates"], e2["shrunk_files"]))
    many = os.path.join(tmp, "snap3")
    w(many, "before/memory/m.md", "\n".join(f"- уникальная строка номер {i} про поведение подсистемы в проекте" for i in range(70)) + "\n")
    L3 = os.path.join(tmp, "lost3")
    w(L3, "memory/x.md", "- пусто\n")
    rc, lm = _run(["lost", "--snapshot", many], L3)
    t.check("lost: без усечения — все 70 кандидатов; мёртвых полей truncated/lost_limit нет",
            len(lm["candidates"]) == 70 and "truncated" not in lm and "lost_limit" not in lm["constants"], len(lm["candidates"]))
    sh1 = os.path.join(tmp, "snap4")
    L4 = os.path.join(tmp, "lost4")
    for i in range(3):
        w(sh1, f"before/memory/e{i}.md", _words(100))
        w(L4, f"memory/e{i}.md", _words(60))
    rc, ls = _run(["lost", "--snapshot", sh1], L4)
    t.check("lost: сумма точек входа упала > 30% (каждый файл < 50%) -> shrink_alarm", ls["shrink_alarm"] is True and ls["shrunk_files"] == []
            and ls["growth"]["total_drop_pct"] > 30, ls["growth"])
    w(sh1, "before/memory/big.md", _words(100))
    rc, ls = _run(["lost", "--snapshot", sh1], L4)
    ok1 = ls["shrink_alarm"] and [x["file"] for x in ls["shrunk_files"]] == ["memory/big.md"]
    rc, ls2 = _run(["lost", "--snapshot", sh1, "--expect-deleted", "memory/big.md"], L4)
    t.check("lost: файл потерял > 50% слов -> shrunk_files; одобренное удаление тревогу не поднимает", ok1 and [x["file"] for x in ls2["shrunk_files"]] == [], (ls["shrunk_files"], ls2["shrunk_files"]))

    red = ["git diff --output=memory/a.md", "git log --output=/tmp/x", "rg --pre 'sh -c id' foo src", "git grep -O'sh -c id' x",
           "ls; rm -rf memory", "git diff --ext-diff", "rg -z foo", "rg foo > out.txt", "rg foo | sh", "git -c core.pager=x diff", "rg \"$(rm x)\" src",
           "git diff --outp=memory/a.md", "git grep --open-files-in-pa=echo fact", "rg --hostname-bin 'sh -c id' x",
           "git grep --op=echo x", "git diff --ou=x"]
    green = ["rg -n KEEP src", "git log --oneline -5", "git grep -n x", "rg -c foo src", "ls memory | wc -l", "test -e src/a.ts"]
    t.check("command_unsafe: флаги, исполняющие или пишущие (--output, --pre, -O, --ext-diff, -c, ;|&) — небезопасны",
            [c for c in red if command_unsafe(c) is not True] == [], [c for c in red if command_unsafe(c) is not True])
    t.check("command_unsafe: читающие команды allowlist проходят", [c for c in green if command_unsafe(c) is not False] == [], [c for c in green if command_unsafe(c) is not False])
    notcmd = ["@Public()", "down()", "chats/<chatId>/", "a | b", "Math.random()", "if (userId)"]
    t.check("command_unsafe: фрагменты кода и подписи с метасимволами — не команды (None), а не шум unsafe_commands",
            [c for c in notcmd if command_unsafe(c) is not None] == [], [c for c in notcmd if command_unsafe(c) is not None])
    os.chdir(old_cwd)


def _with_session(sid, fn):
    old = os.environ.get("CLAUDE_CODE_SESSION_ID")
    os.environ["CLAUDE_CODE_SESSION_ID"] = sid
    try:
        return fn()
    finally:
        os.environ["CLAUDE_CODE_SESSION_ID"] = old


class _T:
    def __init__(self):
        self.n, self.failed = 0, []

    def check(self, name, cond, detail=""):
        self.n += 1
        ok = bool(cond)
        print(("OK   " if ok else "FAIL ") + name + ("" if ok else f"\n       факт: {str(detail)[:400]}"))
        if not ok:
            self.failed.append(name)


def cmd_selftest(a):
    """Ломающие входы ловятся: временные репозитории, без сети и без ~/.claude."""
    global NOW
    tmp = tempfile.mkdtemp(prefix="kb-selftest-")
    saved_env, saved_cwd, saved_now, saved_cfg = dict(os.environ), os.getcwd(), NOW, dict(CFG)
    for k in list(os.environ):
        if k.startswith(("CLAUDE", "GIT_")) or k == "KB_SCAN_SNAPSHOTS_DIR":
            del os.environ[k]
    os.environ.update({"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "HOME": tmp,
                       "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                       "GIT_COMMITTER_EMAIL": "t@t", "CLAUDE_CONFIG_DIR": os.path.join(tmp, "cfg"),
                       "CLAUDE_CODE_SESSION_ID": "selftest-session"})
    t = _T()
    try:
        _selftest_body(tmp, t)
        _selftest_round2(tmp, t)
    except Exception as e:  # падение самого теста — тоже красный
        import traceback
        traceback.print_exc()
        t.failed.append(f"исключение в selftest: {e}")
    finally:
        os.environ.clear()
        os.environ.update(saved_env)
        os.chdir(saved_cwd)
        NOW = saved_now
        CFG.update(saved_cfg)
        shutil.rmtree(tmp, ignore_errors=True)
    if t.n < EXPECTED_CASES:
        print(f"FAIL выполнено кейсов {t.n} < ожидаемых {EXPECTED_CASES}: тест не дошёл до конца")
        return 1, None
    if t.failed:
        print(f"FAIL {len(t.failed)} из {t.n}: " + "; ".join(t.failed))
        return 1, None
    print(f"OK: {t.n} кейсов selftest")
    return 0, None


if __name__ == "__main__":
    sys.exit(main())
