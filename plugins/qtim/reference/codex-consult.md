# Codex second-opinion — оркестрация (team-lead)

> Generic reference плагина agent-team. Проектные инварианты и пути живут в charter проекта
> (генерируется setup); здесь — переносимая механика.

> **Читает только team-lead.** Здесь то, что роли не нужно и не должно попадать в их промпты:
> каналы вызова (включая слэш-команды), dual-adversary как форма оркестрации, и **execution
> lane** — отдельная полоса, где Codex пишет код.
>
> **Сам протокол консультации — скил [`qtim:codex-consult`](../skills/codex-consult/SKILL.md).**
> Его вызывает через `Skill` любая роль на своей gate-точке: там принципы (advisory, read-only,
> fail-soft, инвариант > совет), правила и структура вызова, гейты по ролям, режим для
> money/security-кода и порядок верификации findings. Не дублируй это здесь — при расхождении
> верен скил.
>
> Codex имеет **две полосы**: second-opinion (read-only, скил) и execution lane (write, секция
> в конце) — не смешивать.

## Каналы вызова (плагин `codex@openai-codex`)

| Канал | Кто использует | Что даёт |
|---|---|---|
| **Raw CLI** `codex exec ... -s read-only` | Роли на своих gate-точках — по скилу [`qtim:codex-consult`](../skills/codex-consult/SKILL.md) | Стабильный путь без зависимости от плагин-окружения |
| **Slash-команды плагина** `/codex:review`, `/codex:adversarial-review` (+`--background`, `/codex:status`, `/codex:result`, `/codex:cancel`) | **Только team-lead** (слэш-команды недоступны субагентам) | Background-механика: запустить ревью параллельно с QA-gate и забрать результат позже |
| **Агент `codex:codex-rescue`** (`Agent`/`agent({agentType})`) | Team-lead и Workflow-скрипты — **только для execution lane** | Делегирование задачи Codex как агенту. Для consult этот канал не используй: у форвардера дефолт `--write` → `workspace-write`, а навязать плагинному субагенту `permissionMode` нельзя (поле игнорируется). Read-only держался бы только тем, что стороннее звено прочитает фразу в промпте — для гейта этого мало |

Правила выбора: роль на gate-точке → raw CLI по скилу. Team-lead перед мержем крупного эпика →
`/codex:adversarial-review --background` со своим focus-текстом. Workflow dual-adversary →
`codex exec -s read-only` прямо из скрипта, **не** через `codex:codex-rescue`: песочница задаётся
флагом, а не доверием к промпту.

**Money/security-critical (биллинг, модель доступа, платёжные вебхуки) — исключение
(request-safety):** не использовать `/codex:review` / `/codex:adversarial-review` — они форсят
verbatim-ретрансляцию вывода Codex в тред Claude, и request-классификатор режет
платёжно-security findings как false positive. Вместо них — raw
`codex exec -s read-only … -o <каталог>/out.md` (каталог из `mktemp -d`, промпт файлом через stdin) с **нейтральным defect-review** промптом (без
«attack / сломай / exploit» — формулировать как обычное ревью на корректность, не как
weaponized-задачу); файл читать out-of-band, в тред Claude вносить только выжимку issue → fix.

**Stop-review-gate плагина (`/codex:setup --enable-review-gate`) НЕ включать:** это хук
на каждый Stop главного треда (длинный timeout) — в мультиагентных сессиях даст
постоянные блокировки и сожжёт лимиты; его роль уже закрывают `reviewer` + QA-gate.

## Гейты, шаблоны и интеграция findings

Переехали в скил [`qtim:codex-consult`](../skills/codex-consult/SKILL.md): таблица гейтов по
ролям, структура промпта, шаблоны вызова и порядок верификации findings. Роль вызывает скил
сама на своей gate-точке — в промпт спавна и в шаблоны ролей это не дублируется.

> Гейт `architect` — часть более широкого шага «stress-test ADR независимым оппонентом»
> ([`intake-protocol.md`](intake-protocol.md), фаза Design). Codex здесь предпочтителен как
> другое семейство моделей; если он в проекте не настроен (Q5=No), шаг не пропадает —
> team-lead поднимает claude-adversary со свежим контекстом. Оппонент может смениться,
> отсутствие оппонента — нет.

## Dual-adversary (паттерн 5 — claude + codex)

> Расширение этого протокола из single-opponent (advisory second-opinion) в
> **dual-adversary**: к исполнителю подключаются ДВА независимых оппонента разных моделей —
> `claude-adversary` (через `agent()`) + `codex-adversary` (этот протокол). Каталог и скелет —
> [`orchestration-patterns.md`](orchestration-patterns.md) § 5. Применяется для
> security-critical выводов (миграции модели доступа, триггеры, helper-функции, платёжные пути).

- Оба рецензента независимо **ищут дефекты и нарушения инвариантов** в выводе (стартовая
  презумпция: считать некорректным, пока корректность не подтверждена), не видя находок друг друга.
- **Голосование:** оба подтвердили P0/P1 → блок до фикса. Расходятся → team-lead арбитр.
  Все правила выше в силе: исполнитель верифицирует каждый finding сам; **инвариант важнее
  обоих оппонентов**; codex `-s read-only`.
- **codex-ветка fail-soft.** Codex недоступен → паттерн деградирует до
  **single-adversary (claude)** — запись `codex-consult skipped: <reason>`, эпик не
  блокируется. Перед skip — реально попробовать вызов (не скипать по памяти о старой поломке
  credentials).
- В Workflow codex-оппонента удобнее дёргать через `agent({ agentType: 'codex:codex-rescue' })`
  с явным «read-only, не правь код» в промпте (см. «Каналы вызова»), чем через
  general-purpose + ручной CLI-вызов.
- В отчёте: «adversarial: claude N / codex M findings, K подтверждено» или «codex skipped».

## Execution lane — Codex как эскалационный исполнитель

> Отдельная полоса, НЕ second-opinion. Здесь Codex **пишет код** (`codex:codex-rescue`
> форвардит в companion-runtime с `--write`).

**Когда разрешено (ровно два триггера):**
1. Исполнитель (`front`/`db`/team-lead) **застрял**: 2+ неудачные итерации на одной проблеме
   (фикс не сходится, root cause не найден). Решение об эскалации принимает team-lead.
2. **Пользователь явно попросил** отдать задачу Codex («отдай codex», `/codex:rescue`).

**Обязательные правила:**
- Любой код Codex проходит штатный пайплайн: `tester` (real-browser для UI) → `reviewer`
  (полный чеклист). Никаких прямых мержей результата rescue.
- Перед запуском team-lead формулирует задачу со ссылками на инварианты (Codex прочитает
  `AGENTS.md` сам, но конкретный инвариант задачи — в промпт).
- Долгие задачи — `--background`, прогресс через `/codex:status`, результат `/codex:result`.
- Резюме прогона (что делал, какие файлы тронул, вердикт reviewer'а) — в `memory/` по
  обычным правилам эпика.
- Second-opinion gate-точки это НЕ заменяет: на ревью кода, написанного Codex,
  codex-consult не зовётся (нет смысла спрашивать автора) — хватает claude-reviewer.

## Anti-patterns

> Уровня оркестрации. Ошибки самой консультации (слепое применение совета, промпт без скоупа,
> weaponized-формулировки, право записи) — в скиле
> [`qtim:codex-consult`](../skills/codex-consult/SKILL.md).

- ❌ Блокировать эпик из-за недоступности codex — fail-soft действует и на уровне оркестрации.
- ❌ Смешивать полосы: consult-гейт закрывать вызовом с правом записи, а rescue-результат
  проводить мимо `tester`/`reviewer`.
- ❌ Слать money/security-скоуп через слэш-команды с verbatim-ретрансляцией вывода.
- ❌ Включать stop-review-gate в мультиагентной сессии.
- ❌ Звать codex на ревью кода, который сам Codex и написал.
