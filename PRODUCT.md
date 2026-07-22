# Product

## Register

product

## Platform

web

## Users

The primary user is the ACOCOS manager: one person, on a phone, often with a client waiting at the counter or a WhatsApp chat open. Their context is a small dress-and-costume atelier, working fast and one-handed, sometimes in bright daylight. Their job is to record a sale — client, item lines, amount paid — correctly and in under a minute, then pull today's numbers or look up a client's debt. This is a tool touched roughly 50× a day, not something browsed.

The secondary user is the Owner (Django superuser), who works mostly in the `/panel/` admin: managing the catalog and reference data, reviewing day-end payments, pulling reports, and running marketing campaigns. The Owner sees cost prices, profit, and the Система group; the manager and viewers never do. There are only 2–4 trusted users total, no public audience — the whole system lives behind one login and 2FA.

## Product Purpose

ACOCOS CRM is the internal system for a business that makes and sells women's dresses, costumes, and custom items. It tracks stock, sales, payments, clients, and debts, produces a daily Russian report, and drives Telegram/WhatsApp bots and marketing broadcasts. Everything flows from one action: recording a sale. Confirming a sale atomically decrements stock, counts revenue, updates debt, and writes history — so stock, revenue, and debt are never typed by hand, they are consequences. Success is a full sale plus payment entered in `/pos/` in under a minute with nothing hand-typed, a reload that never loses the basket, a double-tap that sells once, and an oversell that fails safely in Russian rather than corrupting the ledger.

## Positioning

The one screen where money is never more than a glance away: a sale is the only thing you record, and stock, revenue, and debt fall out of it as consequences — never entered, never ambiguous.

## Brand Personality

Precise, quiet, trustworthy. The voice is plain, active Russian: the button says what happens («Подтвердить продажу», never «Отправить»), errors state what went wrong and what to do with no apology or vagueness, and empty states invite rather than dead-end. Sentence case everywhere; no ALL CAPS, no exclamation marks, no emoji. Restraint is the design — but restraint means *precise*, not default. The interface should feel like a well-made tool that disappears behind the task, earning trust by making money unambiguous: the payment-status trio (оплачено / частично / долг) is sacred, and colour is always paired with the word, never carrying meaning alone.

## Anti-references

Not a landing page — there is no marketing surface anywhere inside `/pos/`, and no hero, scroll-section, or persuasion pattern belongs here. Not Bootstrap defaults or any generic component-kit look; every choice is derived from a real constraint. Not a native app or JS SPA — a responsive server-rendered page needs no app store, API/token layer, or build step. Nothing decorative that competes with money: the accent is a chalk blue precisely so it stays out of the semantic green/amber/red reserved for payment status. No shouty weights (never 600+ at UI size), no boxy heavy borders (hairlines, not boxes), no motion for its own sake.

## Design Principles

Everything flows from the sale — the manager records one thing, and stock, revenue, and debt are derived consequences, computed through `services.py`, never re-typed or re-implemented in a view. Money is never more than a glance away — the pinned money bar is the signature element and the whole thesis; tabular numerals wherever an amount appears so totals never jitter. Colour plus the word, never colour alone — payment status reads in bright sunlight and to colour-blind eyes because green/amber/red are reserved and always labelled. Fail safe, in Russian — an oversell or double-submit keeps the basket and shows a plain message, never a 500, never a double-sell. Restraint as precision — one font, hairline borders, a single filled button per screen; quiet because the task, not the chrome, is the point.

## Accessibility & Inclusion

WCAG AA contrast for every text/background pair in both light and dark themes (including the amber payment tint, the easiest one to fail). Dark/light follows the OS with a manual header override stored in a cookie and rendered server-side, so no flash of the wrong theme at 7am. Minimum 44×44px touch targets throughout — this is a one-thumb phone interface. Visible keyboard focus (2px accent outline). `@media (prefers-reduced-motion: reduce)` disables transitions. Correct mobile input modes (`inputmode` numeric/decimal, `type="tel"`). Status is never conveyed by colour alone; the word always accompanies it. Full EN/RU i18n for every UI string; reports are always Russian with Cyrillic intact.
