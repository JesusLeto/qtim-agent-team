# acme

SaaS для управления проектными сметами. Nuxt 3 + Supabase, строгий TypeScript.

Команды: `pnpm dev` · `pnpm typecheck` · `pnpm build` · `pnpm test` · `npx playwright`.
Миграции — `supabase/migrations/`, применение `supabase db push`.

## Команда агентов

Контракт команды импортирован строкой ниже — так он приезжает каждой роли в стартовом
контексте, а не остаётся указателем, по которому роль может не сходить.

@.claude/team-charter.md

- `/qtim:team-up` — поднять полную команду под эпик, `/qtim:team-lazy` — роли по требованию,
  `/qtim:team-down` — свернуть.
- `/qtim:team-sync` — после обновления плагина qtim (SessionStart-hook подскажет при дрейфе).
- `/qtim:doctor` — если что-то не работает.

Доменные требования, привязанные к путям, лежат в `.claude/rules/` и подмешиваются рантаймом
тому, кто открыл совпавший файл через `Read`.
