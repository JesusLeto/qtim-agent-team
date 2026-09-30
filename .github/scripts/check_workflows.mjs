#!/usr/bin/env node
// Проверка Workflow-скриптов. Движок Workflow исполняет тело скрипта
// в async-контексте, где top-level return/await легальны — поэтому обычный `node --check`
// (модульный парсинг) здесь даёт ложный fail. Воспроизводим движок: `export const meta`
// заменяем на локальную декларацию и парсим тело конструктором AsyncFunction (без исполнения).
// Дополнительно — лексический запрет источников недетерминизма, ломающих resume
// (Date.now() / Math.random() / безаргументный new Date() в движке бросают исключение)
// и правило «каждый agent() с model-литералом» (без него агент наследует модель сессии,
// а хук окружения пользователя отклоняет скрипт целиком).
import { readFileSync } from "node:fs";

const AsyncFunction = (async () => {}).constructor;

// Без литерала `const meta = {...}`: model в meta.phases не относится к вызовам agent().
// meta — чистый литерал, поэтому хватает скана скобок с учётом кавычек.
function stripMeta(code) {
  const i = code.search(/const\s+meta\s*=/);
  if (i < 0) return code;
  let depth = 0, quote = null;
  for (let j = code.indexOf("{", i); j >= 0 && j < code.length; j++) {
    const c = code[j];
    if (quote) { if (c === "\\") j++; else if (c === quote) quote = null; continue; }
    if (c === "'" || c === '"' || c === "`") quote = c;
    else if (c === "{") depth++;
    else if (c === "}" && --depth === 0) return code.slice(0, i) + code.slice(j + 1);
  }
  return code;
}
const BANNED = /\bDate\.now\s*\(|\bMath\.random\s*\(|\bnew\s+Date\s*\(\s*\)/g;
let failed = false;

for (const file of process.argv.slice(2)) {
  const src = readFileSync(file, "utf8").replace(/^export\s+const\s+meta/m, "const meta");
  const banned = src.match(BANNED);
  if (banned) {
    console.error(`${file}: запрещено (ломает resume Workflow): ${[...new Set(banned.map(s => s.trim()))].join(", ")}`);
    failed = true;
    continue;
  }
  // Модель — строковым литералом прямо в опциях КАЖДОГО agent(): ни обёртки с опциями-переменной,
  // ни тернарника. minimal-diff: сверка по счёту, а не разбором скобок — вызовов agent( и литералов
  // model должно быть поровну (meta вырезана). Равенство, а не «не меньше»: `model:` в тексте промпта
  // иначе замаскировал бы пропуск. Потолок: два литерала в одном вызове при нуле в другом пройдут —
  // писать разбор скобок, когда это реально проедет.
  const code = stripMeta(src.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, ""));
  const calls = (code.match(/(?<![\w$.])agent\s*\(/g) || []).length;
  const pinned = (code.match(/\bmodel\s*:\s*['"](?:opus|sonnet|haiku)['"]/g) || []).length;
  if (calls !== pinned) {
    console.error(`${file}: вызовов agent(): ${calls}, model-литералов: ${pinned} — добавь model: 'opus' | 'sonnet' | 'haiku' в опции КАЖДОГО agent() прямо в вызове (не через переменную и не тернарником); лишний model: вне agent() (например, в тексте промпта) убери`);
    failed = true;
    continue;
  }
  try {
    new AsyncFunction("agent", "parallel", "pipeline", "phase", "log", "args", "budget", "workflow", src);
    console.log(`OK: ${file}`);
  } catch (e) {
    console.error(`${file}: ${e.message}`);
    failed = true;
  }
}

if (process.argv.length <= 2) {
  console.error("Не передано ни одного файла");
  failed = true;
}
if (failed) process.exit(1);
