# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Stack

Existing codebase: single self-contained HTML file (inline CSS/JS, no framework, no build step), served as a Jinja-free static string by a FastAPI route. This surface preserves that — a redesign of the same file, not a framework migration.

## Users

*Inferred from repo/session evidence, not confirmed by interview.* A single operator (the desk's owner) checking, from a phone or a tunneled browser tab, whether the autonomous trading engine is actually doing something real right now — under stress, wanting the state of the world in one glance: is it alive, is money at risk, did anything just happen. Not a multi-tenant product; no other viewer exists.

## Product Purpose

SIBILLA is an autonomous equities trading desk (eToro Public API, penny/small-cap momentum + LLM news-catalyst gate). This surface is its read-only cockpit: live account equity, open positions, a chronological activity feed (started/stopped, catalyst accepted/rejected, orders), and a raw table of every screener calculation this cycle (gap %, relative volume, pass/fail) — the evidence trail for "why hasn't it traded," not just a summary.

## Positioning

*Inferred.* Not a retail brokerage dashboard (no marketing chrome, no onboarding, no upsell) and not a generic admin panel — it reads as a professional trading-desk terminal: dense, numeric, unapologetically technical, built to be trusted by someone who already knows what a gap % and a relative-volume multiple mean.

## Operating Context

Reached only via SSH tunnel to `127.0.0.1:8000` on the desk's VM — never public, no login screen (physical/network access is the only gate). Viewed in short, frequent checks during US market hours, often on a phone screen over a tunnel, sometimes left open on a desktop tab polling in the background. Data source is entirely server-side polling (JSON endpoints below) refreshed by the page every ~15s; there is no websocket and no client-side state that must survive a refresh.

## Capabilities and Constraints

- Read-only: no controls, no mutation, no auth flow to design.
- Existing JSON endpoints, unchanged by this redesign: `/api/etoro/status` (mode, price ceiling, key configured), `/api/etoro/balance` (equity/available/currency), `/api/etoro/positions` (open positions with stop/target), `/api/etoro/feed` (curated log-derived event stream), `/api/etoro/calculations` (every screener evaluation this cycle, qualifying or not).
- Must degrade honestly to explicit empty states (no positions, no feed yet, no calculations yet) — these are frequent, normal states, not errors.
- No client framework, no external JS/CSS dependencies beyond what the artifact/runtime CDN allowlist would permit — currently zero external dependencies, and that should stay true (one file, no build step, no network fetch besides the same-origin JSON polling).

## Brand Commitments

Name is fixed: **SIBILLA** (never rename, never translate). Origin: the Cumaean Sibyl, who sold her prophecies at ever-increasing prices — the README already leans on this. No existing logo file; a wordmark/identity built from typography and a simple mark is in scope for this redesign, not a request for an externally supplied logo asset.

## Evidence on Hand

- `README.md` (just rewritten) is the authoritative product description.
- Existing incumbent implementation at `src/dashboard/templates/etoro_dashboard.html` — dark terminal palette, hairline-bordered strip layout, monospace-first typography. Treated as evidence/anti-reference for this redesign per the brief ("bella estetica", full redo), not preserved verbatim.
- No real screenshots, testimonials, or marketing assets exist or should be fabricated.

## Product Principles

1. Numbers over decoration — every pixel should help someone under time pressure answer "is it working, is it safe, did it trade."
2. Never hide the null state — zero positions, zero catalysts, zero trades is the normal, honest state of a disciplined system, and the UI must say so plainly, not look broken.
3. One file, zero dependencies, instant load over a tunnel on a phone connection.
4. Dark, dense, numeric register throughout — never soften into consumer-fintech friendliness.

## Accessibility & Inclusion

No specific requirement established; sole known user has no stated accessibility need. Standard contrast/readability care applies as baseline craft, not a documented mandate.
