---
name: ACOCOS CRM
description: A precise, quiet instrument for recording sales on a phone — money always a glance away.
colors:
  page: "#F5F6F7"
  surface: "#FFFFFF"
  surface-sunken: "#EDEFF1"
  border: "#E3E6E9"
  border-strong: "#C7CDD2"
  text: "#14181C"
  text-2: "#5B6670"
  text-3: "#8A939C"
  accent: "#2C6FB5"
  accent-bg: "#E8F0F9"
  on-accent: "#FFFFFF"
  paid: "#1D7A4F"
  paid-bg: "#E4F3EB"
  partial: "#8A5300"
  partial-bg: "#FBF0DE"
  debt: "#B3271F"
  debt-bg: "#FAE9E8"
typography:
  display:
    fontFamily: "Inter, -apple-system, Segoe UI, Roboto, sans-serif"
    fontSize: "28px"
    fontWeight: 500
    lineHeight: 1.2
    letterSpacing: "-0.02em"
  headline:
    fontFamily: "Inter, -apple-system, Segoe UI, Roboto, sans-serif"
    fontSize: "20px"
    fontWeight: 500
    lineHeight: 1.3
    letterSpacing: "-0.01em"
  title:
    fontFamily: "Inter, -apple-system, Segoe UI, Roboto, sans-serif"
    fontSize: "16px"
    fontWeight: 500
    lineHeight: 1.4
    letterSpacing: "-0.01em"
  body:
    fontFamily: "Inter, -apple-system, Segoe UI, Roboto, sans-serif"
    fontSize: "15px"
    fontWeight: 400
    lineHeight: 1.5
    letterSpacing: "normal"
  label:
    fontFamily: "Inter, -apple-system, Segoe UI, Roboto, sans-serif"
    fontSize: "12px"
    fontWeight: 500
    lineHeight: 1.4
    letterSpacing: "normal"
  money:
    fontFamily: "Inter, -apple-system, Segoe UI, Roboto, sans-serif"
    fontSize: "22px"
    fontWeight: 500
    lineHeight: 1.2
    fontFeature: "tabular-nums"
rounded:
  control: "8px"
  card: "12px"
  pill: "20px"
spacing:
  space-1: "4px"
  space-2: "8px"
  space-3: "12px"
  space-4: "16px"
  space-6: "24px"
components:
  button-primary:
    backgroundColor: "{colors.accent}"
    textColor: "{colors.on-accent}"
    rounded: "{rounded.control}"
    padding: "10px 16px"
    height: "44px"
  button-ghost:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.accent}"
    rounded: "{rounded.control}"
    padding: "10px 16px"
    height: "44px"
  link-button:
    backgroundColor: "{colors.surface-sunken}"
    textColor: "{colors.accent}"
    rounded: "{rounded.control}"
    padding: "0 12px"
    height: "44px"
  input:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.text}"
    rounded: "{rounded.control}"
    padding: "10px 12px"
    height: "44px"
  metric-card:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.text}"
    rounded: "{rounded.card}"
    padding: "12px"
    height: "92px"
  badge-paid:
    backgroundColor: "{colors.paid-bg}"
    textColor: "{colors.paid}"
    rounded: "{rounded.pill}"
    padding: "2px 10px"
  badge-partial:
    backgroundColor: "{colors.partial-bg}"
    textColor: "{colors.partial}"
    rounded: "{rounded.pill}"
    padding: "2px 10px"
  badge-debt:
    backgroundColor: "{colors.debt-bg}"
    textColor: "{colors.debt}"
    rounded: "{rounded.pill}"
    padding: "2px 10px"
---

# Design System: ACOCOS CRM

## 1. Overview

**Creative North Star: "The Instrument Panel"**

ACOCOS is read the way a pilot reads a cockpit: under time pressure, at a glance, with a client waiting at the counter. It is a tool touched roughly 50× a day on a phone, not a page anyone browses. Every visual decision serves one reading — *what is the money doing right now?* — and everything else recedes to make that reading instant. The signature is the pinned money bar: Итого / Оплачено / Остаток, the largest text on the screen, in fixed-width digits so totals never jitter as they update. That is the whole thesis; the rest of the system is calibrated to stay out of its way.

The palette is deliberately disciplined. A single chalk-blue accent (`#2C6FB5`) does all the work of primary actions and current-selection — borrowed, per the original spec, from the tailor's marking chalk in the atelier, and chosen precisely so it never competes with the reserved payment-status trio (green оплачено / amber частично / red долг). Money carries valence through colour; nothing decorative is allowed to speak in those three hues. Surfaces are near-flat, borders are hairlines (0.5px), and there is exactly one filled button per screen. Depth is conveyed by tone, not by shadow.

This system explicitly rejects the landing-page reflexes: no hero, no marketing surface, no persuasion pattern, no gradient text, no glassmorphism, no over-rounded cards, no Bootstrap-default component kit. It also rejects the "dashboard cliché" — the big-number-with-gradient hero metric — even on the owner analytics screen, where the four metric cards stay quiet and equal. Restraint here is not minimalism for its own sake; it is precision. If a mark doesn't help someone record a sale correctly or read the money faster, it isn't drawn.

**Key Characteristics:**
- Money is the loudest element on every screen; everything else is calibrated below it.
- One accent, one filled button, hairline borders, tonal (not shadow) depth.
- Tabular numerals wherever an amount appears — the typographic signature.
- Colour + word for status, never colour alone (sunlight and colour-blindness both exist).
- Mobile-first, thumb-sized (44px minimum), server-rendered, theme-aware with no flash.

## 2. Colors

A restrained neutral field with a single chalk-blue accent, guarded by a sacred three-colour semantic vocabulary for money.

### Primary
- **Chalk Blue** (`#2C6FB5` light / `#6BA8E0` dark): The one accent. Primary buttons, current selection (active period pill, current nav item), chart lines and fills, focus rings, links. Used sparingly — it marks *the* action, not decoration. Its paired tint **Accent Wash** (`#E8F0F9` / `#1B2C3D`) fills the client chip and hover states.

### Secondary
The system has no second accent by design. The categorical chart ramp (`--cat-1..4`) is a **monochrome scale of the accent** mixed toward the surface, not a rainbow — payment methods carry no valence, so a red/green set would falsely collide with the money trio.

### Tertiary
- **Payment Semantics (reserved, never decorative):** **Paid Green** (`#1D7A4F` / `#4FBF8B`), **Partial Amber** (`#8A5300` / `#E5A245` — deliberately darkened from the original `#B26B00` so text clears WCAG AA on white), **Debt Red** (`#B3271F` / `#E86A62`), each with a low-chroma background tint. These three encode оплачено / частично / долг and appear on the money bar, status badges, debt chips, and metric deltas — nowhere else.

### Neutral
- **Ink** (`#14181C` / `#EEF1F4`): Primary text, the darkest available end of the ramp for maximum legibility.
- **Ink-2** (`#5B6670` / `#9AA5B0`): Labels, secondary text, table amounts.
- **Ink-3** (`#8A939C` / `#6B7680`): Captions, hints, placeholders, disabled captions.
- **Page** (`#F5F6F7` / `#14181C`): The app background, one step below surface.
- **Surface** (`#FFFFFF` / `#1C2126`): Cards, panels, bars, the money bar.
- **Surface Sunken** (`#EDEFF1` / `#23292F`): Photo placeholders, chart-bar tracks, segmented-control troughs, table hover.
- **Border** (`#E3E6E9` / `#2C333A`): Hairline dividers and card edges, always 0.5px. **Border Strong** (`#C7CDD2` / `#3C444C`): the one step up, used for secondary-button edges and the chart hover guide line.

### Named Rules
**The Reserved Trio Rule.** Green, amber, and red belong to payment status and nothing else. Any new accent, chart series, or decorative colour is forbidden from those three hues — if the brand competed with them, money would become ambiguous, which is the exact problem this system exists to fix.

**The One Accent Rule.** There is a single accent. Charts extend it as a monochrome ramp toward the surface; they never introduce new hues. If a screen seems to need a second colour, it needs less on the screen instead.

## 3. Typography

**Display / Body / Label / Data Font:** Inter (variable, self-hosted `InterVariable.woff2`), with `-apple-system, Segoe UI, Roboto, sans-serif` fallback.

**Character:** One family, three roles carried by weight and numerics rather than a second typeface. Inter has full Cyrillic, so the Russian-first interface needs no extra font file on a phone connection. The system tops out at weight 500 — never 600+, which reads shouty at UI size — and leans on `tabular-nums` to give money a fixed rhythm.

### Hierarchy
- **Display** (500, 28px, tight `-0.02em`): The ACOCOS wordmark on the login page — the one surface with room for character. Appears nowhere inside /pos/.
- **Headline** (500, 19–20px): Dashboard title, error-page message. The largest text on an app screen apart from the money total.
- **Title** (500, 16px): The /pos/ header brand mark.
- **Body** (400, 15px, line-height 1.5): All UI text, list rows, table cells. Prose capped at a comfortable reading column (720px on read-only pages).
- **Label** (500, 11–12px): Field labels, metric labels, panel titles, table headers. Card section headers (`.card h2`) additionally go uppercase with `0.04em` tracking — the one deliberate uppercase in the system.
- **Money** (500, 22px on the money-bar total, `tabular-nums`): The typographic signature.

### Named Rules
**The Tabular Money Rule.** `font-variant-numeric: tabular-nums` is applied everywhere an amount appears — the money bar, line totals, tile prices, metric values, table numbers, the debt chip — and *only* there. Fixed-width digits stop totals from jittering as they recompute.

**The 500 Ceiling Rule.** No weight above 500 anywhere. Emphasis comes from size, colour, and tabular alignment, never from bold-heavy type.

## 4. Elevation

Near-flat by doctrine. Depth is built from **tone** — page → surface → surface-sunken is a three-step lightness ladder that separates layers without a single drop shadow doing structural work. The only shadow in the vocabulary is a whisper-soft ambient lift (`--shadow`), used to float genuinely-detached surfaces: cards, the pinned money bar, and the chart tooltip. It is never paired with a prominent border as decoration.

### Shadow Vocabulary
- **Ambient Lift** (`box-shadow: 0 1px 3px rgba(20,24,28,0.08)` light / `rgba(0,0,0,0.4)` dark): Cards, the money bar, the chart tooltip. The single shadow token; 1px offset, 3px blur, nothing heavier.

### Named Rules
**The Hairline Rule.** Structure is drawn with 0.5px borders, not shadows and not 1px boxes. Borders separate; shadows only lift things that are truly floating.

**The Tonal Depth Rule.** Layering is conveyed by the page/surface/sunken lightness ladder. If two surfaces need separating, step the tone — don't reach for a shadow.

## 5. Components

### Buttons
- **Shape:** 8px radius (`--radius-control`); pill (20px) only for badges and chips.
- **Primary:** Chalk-blue fill, white text, full-width, 44px min height. **Exactly one per screen** — on /pos/ it is «Подтвердить продажу» below the money bar; it is the only filled button in view.
- **Ghost / Secondary:** Transparent (or sunken) fill, accent text, hairline border. Used for undo, cancel, and secondary actions.
- **Link-button** (`.top__link`): Sunken fill with a 1px border-strong edge so it reads as a control on both the lighter header bar and the darker dashboard page. Header nav and download links.
- **States:** `:active` presses to `scale(0.96)` at 120ms; disabled drops to 0.5 opacity. Hover (pointer devices only) shifts border to accent and background to accent-wash.

### Chips / Badges
- **Style:** Pill (20px), tinted background + matching semantic text, 11px/500. `badge--paid/partial/debt/neutral` and table `chip--partial/debt`.
- **Rule:** The word is always present — the colour never carries the meaning alone.

### Cards / Panels
- **Corner Style:** 12px (`--radius-card`).
- **Background:** Surface, on the page field.
- **Border + lift:** 0.5px hairline plus a soft, hue-tinted shadow (`0 1px 2px` + `0 4px 12px`, theme-aware) so dashboard cards read as objects on the field rather than flat cells. Deliberately *not* the ghost-card pattern (a hairline + a shallow ≤12px shadow, never a 1px border + wide 16px+ shadow). /pos/ keeps its lighter Ambient Lift.
- **Internal Padding:** 16px on cards/panels, 12px on the denser metric cards.
- **Metric card:** A flex row — a soft accent-tinted **icon badge** (34px, `accent-bg` fill, accent glyph) on the left for instant wayfinding, then a body of three lines: label (11px ink-2) / value (19px tabular) / one contextual sub (11px ink-3: «за месяц» for period-scoped metrics, «маржа X%» for profit, «N клиент(ов) · сейчас» for the running debt balance). Icon-left, never an absolute corner badge (which would collide with a long value). `min-height: 96px` so count-up never shifts layout. No period-over-period delta: a red «↓ 100%» on a quiet day reads as failure, not information, so it's gone.

### Iconography
Wayfinding icons from **one family** (Phosphor, MIT), vendored as an inline SVG sprite (`_icons.svg`) — CSP-safe, no CDN, `fill: currentColor` so CSS drives colour. Metric badges use the accent; panel-title and empty-state glyphs are muted (ink-3); header/nav/download glyphs inherit their link colour. Icons *support* text, never replace it. The theme toggle uses real sun/moon glyphs, not emoji.

### Product thumbnails
The Топ товаров and Залежавшийся товар tables lead each row with a 38px product thumbnail (`Product.thumbnail`/`photo`), falling back to a sunken placeholder tile with a muted glyph when a product has no photo — the same pattern as the /pos/ product grid. Turns an abstract text row into something recognised at a glance.

### Inputs / Fields
- **Style:** Surface fill, 0.5px hairline border, 8px radius, 44px min height, **16px font** (prevents iOS auto-zoom on focus).
- **Focus:** Border shifts to accent; the global `:focus-visible` adds a 2px accent outline at 2px offset.
- **Rule:** Inputs and selects keep a visible resting border in *both* themes — the known dark-mode defect is fields vanishing without one.

### Navigation
- **/pos/ shell:** Sticky top bar (brand + theme toggle + owner-only Админпанель link) and a fixed 56px bottom nav (Продажа / Сегодня / Клиенты), active item in accent with `aria-current="page"`. Thumb-reachable.
- **Dashboard:** Two segmented controls on one row (`.controls`) — a **period** control (Сегодня…Год) and a **view-currency** control (сом / $ / ₽). Pills sit in a sunken trough, the active one filled accent with `aria-pressed="true"`, driven by HTMX `?period=&cur=` swaps into `#dash-panel`. Both controls render *inside* the swapped panel, so each swap re-emits them with correct cross-links and active state (no client-side syncing). The currency control is **view-only**: money is always stored and computed in сом; USD/RUB are an «≈» convenience converted per request at today's NBKR rate, flagged with an `.approx-note` disclaimer, and never touch the exports (always сом).

### The Money Bar (signature component)
Sticky to the bottom of the viewport above the bottom nav, always visible above the keyboard. Three rows — Итого / Оплачено / then a divider and the large Остаток. The Остаток label and number take the live payment-status colour and read «Остаток · оплачено / частично / долг». The total is 22px/500 tabular — the largest thing on the screen. One primary button sits below it. On desktop (`:has(#sale-body)`) the whole cart, money bar included, becomes a sticky right rail instead of a floating bar.

### Data Visualization (dashboard)
Server-rendered, no charting library. Revenue is a **calendar heat-map** — CSS-grid month cards (Mon-first, weekday header), one cell per day tinted by revenue (accent mixed into the surface across 5 levels; empty days stay faint, out-of-period days dim for context, today outlined). It scales by period: 1 month card for Сегодня/Месяц up to 13 for Год, wrapping in a flex row. Click any day and an **inline detail panel** below the calendar shows that day's revenue / sales count / units (a small external-JS handler, CSP-safe); the most recent day with sales auto-opens so the panel is never empty. A calendar is the honest shape for daily takings and reads far better than a chart on sparse data. Ranked **channel bars** and a **payment-method donut** that share the same monochrome `--cat-1..4` accent ramp (rank 1 / largest = darkest) so the two side-by-side panels read as one system. The bar's colour only reinforces its length and printed value — meaning never rides on colour alone. Motion draws the line in and grows the bars once on load (the dashboard is opened once or twice a day, so choreography is affordable here in a way it never is on /pos/).

**Semantic emphasis.** Beyond status badges, the two semantic tokens carry meaning into data: a **loss** (negative profit, a written-off count) reads `--debt`, always with the minus sign present; a **low-stock** count that needs action reads `--partial`. This mirrors how metric deltas already colour good/bad direction — it does not expand the reserved trio's meaning, and colour is never the only signal.

## 6. Do's and Don'ts

### Do:
- **Do** keep money the loudest element on every screen — 22px tabular total, semantic colour, pinned in view.
- **Do** apply `font-variant-numeric: tabular-nums` to every amount, and only to amounts.
- **Do** pair every status colour with its word (оплачено / частично / долг), so it reads in sunlight and to colour-blind eyes.
- **Do** draw structure with 0.5px hairline borders and the page/surface/sunken tonal ladder.
- **Do** keep exactly one filled (accent) button per screen; everything else is ghost, link, or text.
- **Do** read every colour, space, and radius from `tokens.css` as a `var()`; the token file is the only place a literal value is allowed.
- **Do** give inputs and selects a visible resting border in both light and dark themes.
- **Do** hold weights at 500 and below; get emphasis from size, colour, and alignment.

### Don't:
- **Don't** use green, amber, or red for anything but payment status — no chart series, accent, or decoration may borrow the reserved trio.
- **Don't** introduce a second accent or a rainbow chart palette; extend the one accent as a monochrome ramp instead.
- **Don't** pair a 1px border with a soft wide drop shadow (the ghost-card pattern), or float cards on shadows where a hairline and tone will do.
- **Don't** over-round: cards top out at 12px, controls at 8px; the pill (20px) is for badges and chips only.
- **Don't** use gradient text, glassmorphism, decorative motion, or any weight above 500.
- **Don't** treat this like a landing page — no hero, no marketing surface, no big-number-with-gradient hero-metric on the dashboard.
- **Don't** draw in, stagger, or count up anything on /pos/ — a client is waiting; save choreography for the once-a-day dashboard.
- **Don't** hardcode a hex or px value outside `tokens.css`.
