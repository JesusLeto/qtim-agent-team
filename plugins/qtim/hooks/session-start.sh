#!/bin/sh
# SessionStart-hook плагина qtim: анонс команды + детектор дрейфа версий charter <-> плагин.
# stdout инжектится в контекст сессии. Fail-soft: любая проблема -> обычный анонс или тишина.
# Формат штампа charter — контракт с setup 4.1 и /qtim:team-sync: generated-by: qtim vX.Y.Z · mode: ...
# Вторая часть — подсказка свежести memory/ (формат `checked` — контракт с каноном формата памяти и kb_scan.py).

PLUGIN_ROOT="${1:-$CLAUDE_PLUGIN_ROOT}"

# cwd hook-процесса не обязан совпадать с корнем проекта — якоримся на $CLAUDE_PROJECT_DIR.
cd "${CLAUDE_PROJECT_DIR:-.}" 2>/dev/null || exit 0

[ -f .claude/team-charter.md ] || exit 0

PLUGIN_V=$(sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$PLUGIN_ROOT/.claude-plugin/plugin.json" 2>/dev/null | head -n 1)
CHARTER_V=$(grep -m 1 -o 'generated-by: qtim v[0-9][0-9A-Za-z.-]*' .claude/team-charter.md 2>/dev/null | sed 's/.*qtim v//; s/[.-]*$//')

if [ -z "$PLUGIN_V" ] || [ "$CHARTER_V" = "$PLUGIN_V" ]; then
  echo "[qtim] Команда агентов настроена (charter найден). /qtim:feature — хотелка до плана (если настроена PM-дорожка), /qtim:team-up — полная, /qtim:team-lazy — по требованию, /qtim:team-down — свернуть."
else
  echo "[qtim] Команда настроена, но собрана по версии плагина ${CHARTER_V:-«до 1.3.0, штампа нет»}, а установлена v$PLUGIN_V — сгенерированные файлы (charter/агенты/settings) могли разойтись с движком. Запусти /qtim:team-sync: миграция подтянет изменения, не трогая проектную конкретику и memory/. Работать можно и без этого: /qtim:team-up | /qtim:team-lazy | /qtim:team-down (+ /qtim:feature, если настроена PM-дорожка)."
fi

# Свежесть памяти: самый старый `checked: <дата> @ <sha>` среди memory/*.md (без git) -> один
# `git rev-list --count <sha>..HEAD`; второй вызов git — только на путях «нет checked»/«sha не найден»,
# чтобы отличить «не git-репозиторий» (тишина) от реального сигнала. Без сети.
# minimal-diff: калибровка по первым прогонам — порог KB_STALE_AT (коммитов).
KB_STALE_AT=100
KB_TAIL="Справка для пользователя: /qtim:kb-refresh запускает он; сам не запускай и задачу этим не откладывай."
if [ -d memory ] && command -v git >/dev/null 2>&1; then
  # (awk без POSIX-классов и интервалов — работает и в mawk 1.3.3.) Учитываются те же файлы, что и в дельте kb_scan.py: без status: archive и без epic-state.md (рабочий файл эпика) —
  # иначе stamp по сверенным файлам не погасил бы подсказку.
  KB_OLD=$(awk '
    function flush() { if (ck != "" && !arch) print ck }
    FNR == 1 { if (NR > 1) flush(); ck = ""; arch = (FILENAME ~ /epic-state\.md$/); infm = ($0 ~ /^---[ \t]*$/); next }
    infm && /^---[ \t]*$/ { infm = 0; next }
    infm && ck == "" && /^[ \t]*checked:/ { ck = $0 }
    infm && /^[ \t]*status:[ \t]*archive/ { arch = 1 }
    END { flush() }' memory/*.md 2>/dev/null \
    | sed -n 's/^[[:space:]]*checked:[[:space:]]*\([0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]\)[[:space:]]*@[[:space:]]*\([0-9a-fA-F]\{7,40\}\).*/\1 \2/p' \
    | LC_ALL=C sort | head -n 1)
  KB_SHA=${KB_OLD#* }
  if [ -z "$KB_OLD" ]; then
    if ls memory/*.md >/dev/null 2>&1 && git rev-parse --git-dir >/dev/null 2>&1; then
      echo "[qtim] Свежесть памяти неизвестна: ни в одном memory/*.md нет «checked: <дата> @ <sha>». $KB_TAIL"
    fi
  elif KB_N=$(git rev-list --count "$KB_SHA..HEAD" 2>/dev/null); then
    if [ "$KB_N" -ge "$KB_STALE_AT" ]; then
      echo "[qtim] Память отстала на $KB_N коммитов (самый старый checked @ $KB_SHA). $KB_TAIL"
    fi
  elif git rev-parse --git-dir >/dev/null 2>&1; then
    echo "[qtim] checked $KB_SHA не найден в этом клоне (память записана на другой ветке, до rebase, или история обрезана). $KB_TAIL"
  fi
fi

exit 0
