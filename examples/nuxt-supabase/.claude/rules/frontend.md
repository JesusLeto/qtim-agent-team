---
paths:
  - "pages/**/*.vue"
  - "components/**/*.vue"
  - "layouts/**/*.vue"
  - "composables/**/*.ts"
  - "middleware/**/*.ts"
---

# Клиентский код (acme)

- Типы строк БД — только из `types/database.ts`. Локальных дублей row-типов нет; изменилась
  схема → сначала типы, потом код.
- Zero `any`. Props и emits — через `defineProps` / `defineEmits` с типами.
- Никаких hardcode URL и ключей — только `runtimeConfig` / env.
- Scope-зависимый кэш (workspace) — три шага: уникальный ключ состояния, ключ зарегистрирован
  в reset-on-workspace-change, `watch` смены workspace для рефетча. Пропущен любой — данные
  протекут между workspace.
- Realtime — только через синглтон-канал проекта, без параллельных подписок на ту же таблицу.
- У каждого экрана с данными обработаны loading / empty / error. Тексты — на русском.
- `data-testid` на интерактивных элементах: от них зависит tester.
- Fetch и state — в composable; страница оркеструет composable и компоненты, без прямых
  запросов в шаблоне страницы.
