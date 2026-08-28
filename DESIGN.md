---
name: SIBILLA
description: Night-flight instrument panel for an autonomous eToro trading desk
colors:
  void: "#05070a"
  panel: "#0a0e13"
  panel-alt: "#0d1218"
  edge: "#1a222b"
  edge-strong: "#242e38"
  ink-dim: "#48545e"
  ink: "#a7b3bc"
  ink-hi: "#e4ecf0"
  signal-green: "#9fef6f"
  signal-green-dim: "#5f8a4a"
  signal-amber: "#ffb84d"
  signal-red: "#ff6b6b"
typography:
  body:
    fontFamily: "ui-monospace, 'JetBrains Mono', 'SF Mono', Menlo, Consolas, 'Liberation Mono', monospace"
    fontSize: "13px"
    lineHeight: 1.5
  label:
    fontFamily: "{typography.body.fontFamily}"
    fontSize: "9.5px"
    fontWeight: 600
    letterSpacing: "0.14em"
    textTransform: "uppercase"
  micro:
    fontFamily: "{typography.body.fontFamily}"
    fontSize: "10px"
    fontWeight: 500
  meta:
    fontFamily: "{typography.body.fontFamily}"
    fontSize: "10.5px"
    fontWeight: 500
  body-secondary:
    fontFamily: "{typography.body.fontFamily}"
    fontSize: "11.5px"
    fontWeight: 500
  table-data:
    fontFamily: "{typography.body.fontFamily}"
    fontSize: "12px"
    fontWeight: 500
  wordmark:
    fontFamily: "{typography.body.fontFamily}"
    fontSize: "15px"
    fontWeight: 700
    letterSpacing: "0.16em"
  gauge-value-secondary:
    fontFamily: "{typography.body.fontFamily}"
    fontSize: "16px"
    fontWeight: 500
  gauge-value:
    fontFamily: "{typography.body.fontFamily}"
    fontSize: "22px"
    fontWeight: 600
    lineHeight: 1.3
rounded:
  none: "0px"
  hairline: "1px"
  container: "2px"
  dot: "50%"
spacing:
  xs: "4px"
  sm: "8px"
  md: "14px"
  lg: "20px"
components:
  gauge-card:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.signal-green}"
    padding: "14px 14px 12px"
  gauge-card-caution:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.signal-amber}"
    padding: "14px 14px 12px"
  feed-line-live:
    backgroundColor: "transparent"
    textColor: "{colors.ink-hi}"
  feed-line-aged:
    backgroundColor: "transparent"
    textColor: "{colors.ink-dim}"
---

## Overview

SIBILLA's dashboard is a read-only instrument panel, not a report: a single stressed operator glances at it over an SSH tunnel to answer one question — is real capital safe right now — and closes the tab within seconds. The world is a night-flight instrument six-pack (luminous gauges a pilot trusts through cloud) fused with CRT-phosphor terminal typography and a demoscene-style depth-ranked activity feed. It refuses the generic dark-mode-SaaS-dashboard default: no card shadows, no gradients, no rounded pill buttons, no chrome for chrome's sake. Every pixel exists to answer "is it alive, is it safe, did it trade."

Mode: **Operate**. Numbers over decoration; the null state (zero positions, zero catalysts found) is the honest, common state of a disciplined system and must never read as broken.

## Colors

Color strategy: **Full palette**, three named functional roles on a near-black instrument-panel ground — this is not a "restrained neutral + one accent" surface because a real instrument panel needs a primary signal and a distinct caution signal to be legible at a glance.

- `void` (`#05070a`) — page ground, deliberately darker than the panels it holds, so cards read as lit instruments floating in a dark cockpit.
- `panel` / `panel-alt` (`#0a0e13` / `#0d1218`) — gauge card and table-adjacent surfaces.
- `edge` / `edge-strong` (`#1a222b` / `#242e38`) — hairline dividers only; never a drawn box-shadow border.
- `ink-dim` / `ink` / `ink-hi` — label, body, and emphasis text on a three-step dim-to-bright scale.
- `signal-green` (`#9fef6f`) — the radium-phosphor primary: live status, positive gauge values, confirmed/ok feed events, qualifying screener rows.
- `signal-amber` (`#ffb84d`) — caution only: position cap reached, key not configured, rejected feed events. Never decorative.
- `signal-red` (`#ff6b6b`) — danger/error only (cycle failures, stop-loss levels). Used sparingly — most of the system's normal life is green or neutral, and red must stay rare to stay meaningful.

## Typography

One family throughout: a monospace workhorse stack (`ui-monospace, JetBrains Mono, SF Mono, Menlo, Consolas, monospace`) — no external font fetch, so the page loads instantly over a tunnel on a phone connection (Product Principle #3). This is a deliberate Operate-mode choice, not a placeholder: the terminal/CRT world this dashboard inhabits is inherently monospace, and system-stack monospace is a legitimate, distinctive choice for this register (per the skill's own guidance, Operate surfaces are well served by workhorse faces — the training-data-default warning targets Persuade/Experience surfaces reaching for a "point of view" serif or display face, not this).

Scale spans 9.5px (micro labels, letter-spaced, uppercase) to 22px (gauge hero numbers), eight named steps in between (`micro` 10px, `meta` 10.5px, `body-secondary` 11.5px, `table-data` 12px, `body` 13px, `wordmark` 15px, `gauge-value-secondary` 16px) — a dense terminal genuinely needs this many close small-text steps to separate label / meta / secondary-body / tabular-data registers at a glance; it is a real hierarchy carried by weight and color as much as size, not a flat wall of near-identical text.

## Layout

Fixed top instrument row (`.gauges`, CSS grid) of six gauge cards: Equity, Disponibile, Posizioni, Mercato, Universo, Ultimo scan — the first-viewport thesis. Collapses 6→3→2 columns at 980px/560px breakpoints; content never truncates by hiding, only by reflow.

Below: three full-width sections in reading order — Posizioni aperte (a real table, tabular data), Attività recente (a boxless feed, not a table), Calcoli screener (a dense numeric table). Section headers are a small tracked-caps label plus a hairline rule filling the remaining width — no card chrome around sections.

## Elevation & Depth

No drop shadows, no card elevation. Depth is expressed two ways instead:

1. **Luminosity** — gauge values and live-status dots carry a soft, tightly-scoped glow (`text-shadow` / `box-shadow`, 5–14px blur, ≤0.7 alpha) that reads as the panel's own light source, not a UI shadow. This is the direction's literal material (radium dial glow, CRT phosphor) and is confirmed intentional — scoped only to live indicators and gauge numerals, never applied to borders, buttons, or panel chrome.
2. **Opacity-as-recency** — the activity feed has no boxes or row dividers; each line's opacity steps down by age (newest ≈1.0, floor 0.32), so the eye reads "how long ago" without a timestamp column doing all the work.

## Shapes

Square corners everywhere except the two live-status dots and the wordmark bezel ring, which are circles (`border-radius: 50%`) — the one deliberate curved element, reserved for "this thing is alive/watching." Two flat radii scale with element size: `hairline` (1px) on the thin tick bars, `container` (2px) on the larger gauges grid frame — never more than 2px, never a "friendly rounded card" radius. Hairline 1px borders only; no thick strokes, no double borders.

## Components

- **Gauge card**: label (dim, tracked caps) → hero value (green, or amber when the state is a caution) → secondary sub-label → a 2px tick bar whose fill width (`transform: scaleX()`, never animated `width`) encodes a ratio (e.g. disponibile/equity, posizioni/max).
- **Wordmark**: a small bezel-ring mark with a pulsing green center dot, plus "SIBILLA" in tracked-caps mono, plus a dim tagline. This is the closest thing to a logo the project has — deliberately typographic/CSS, no raster asset (no image generation was available for this build).
- **Feed line**: caret dot (bright green on the newest line, dim gray on the rest) · timestamp (fixed-width, dim) · label (colored by event class: ok=green, warn/neutral=amber or ink, err=red) · detail (dim, truncated). No row background, no border.
- **Table** (positions, calculations): hairline row dividers only, no header background fill beyond the label color, monospace throughout; a qualifying screener row gets a 2px inset green box-shadow on its first cell as the only "selected" cue.

## Do's and Don'ts

- **Do** keep every glow scoped to something genuinely live or luminous in the data (a gauge value, a status dot) — never decorative background glow.
- **Do** let the null/empty state read as calm and normal (`.empty` rows), never as a broken or loading page.
- **Do** animate `transform`/`opacity` only; never animate `width`/`height`/`margin`/`padding`.
- **Don't** add card drop-shadows, rounded pill buttons, gradient backgrounds, or any consumer-fintech softness — this register is a trading-desk terminal, not a retail brokerage app.
- **Don't** introduce a second typeface or an external font/script fetch; the one-file, zero-dependency, instant-load-over-a-tunnel constraint is a product principle, not a style preference.
- **Don't** enclose the activity feed in table borders or cards — depth-by-recency is the system's signature move for that surface and must survive future edits.
