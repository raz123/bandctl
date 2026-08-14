# web/index.html regression harness (dev-only)

A lightweight jsdom harness for the a11y/focus/keyboard fixes shipped on the
`fix/webui-a11y` branch. It extracts the single inline `<script>` from
`web/index.html`, evaluates it in jsdom against a fake `fetch`, and asserts the
fixed behaviors. The module itself is pure Python + a static page — this
harness is **not** part of the shipped module; it only lives in the repo for
regression testing.

## Run

Requires Node >= 18.

```sh
npm install        # installs jsdom (devDependency only)
npm test           # == node tests/js/run.js
```

`npm test` exits non-zero on any failure.

## How it works

`tests/js/run.js`:

1. Reads `web/index.html` and extracts the inline `<style>` (CSS checks) and
   the inline `<script>`.
2. Creates a fresh jsdom page per scenario (real DOM, real localStorage, real
   event dispatch, real async fetch) with a stubbed `fetch` that returns canned
   API responses, and stubs for `scrollTo`/canvas `getContext` (headless
   no-ops).
3. Evaluates the app script, lets `init()` settle, then drives it with
   dispatched `click`/`keydown` events and asserts observable DOM/state
   outcomes.

Because the app's top-level declarations must be reachable from the harness,
the `'use strict'` directive is stripped before the script is evaluated in the
page's global scope. This does not change behavior of the code under test.

## Automated coverage

| Finding | Assertion |
| --- | --- |
| A-031 / A-107 | `#status` and `#toast` carry `role="status"`; `#signal-canvas` carries `role="img"` + `aria-label`; `#signal-summary` is `.sr-only` and its text updates from the signal poll. |
| A-040 | `openModal` moves focus to the first focusable; Tab/Shift+Tab wrap inside the open modal; Escape/close restores focus to the trigger. |
| A-027 | `@media (prefers-reduced-motion: reduce)` keeps opacity/color transitions but drops `transform` and collapses animations (`animation-duration: 0.01ms`, `animation-iteration-count: 1`, `.tab-panel.active { animation: none }`). |
| A-086 | Tabset has `role=tab`/`role=tabpanel`; roving `tabindex` (`0` on the selected tab, `-1` elsewhere); ArrowLeft/Right/Home/End switch tabs and move focus. |
| A-094 | `logEntry` persists to `localStorage['bandController.history.v1']`; a fresh page seeded with stored history restores it on `init()`. |
| A-121 | Clicking or Enter on a band tile keeps keyboard focus on the re-rendered tile (`aria-pressed` follows the toggle). |
| A-191 | Token input starts `type=password`; Show/Hide toggles type and label; saving clears the field, re-masks it, and shows a masked display (`abc123…`). |
| A-192 | Delete opens the in-page confirm modal (nothing deleted yet); confirm deletes + parks focus on the replacement row; cancel keeps the preset and restores focus. |
| A-081 | Registration transitions (data/roaming/operator) are logged to event history. |
| A-029 / A-162 / A-079 | Contrast tokens `--accent: #067a8d`, `--success: #16793a`, `--danger: #c22f28`, `--warn: #96620a` are in the skin, and the computed WCAG contrast ratio is >= 4.5:1 for the fixed pairs (white on accent, accent on white / active fills, success/danger/warn on their surfaces). |
| A-005 / A-083 / A-095 | `body > .app { overflow: clip }` (no accidental scroll container), `.app-header` sticky + `env(safe-area-inset-top)`, tabbar sticky below `--header-h`, toast + modal-overlay honor `env(safe-area-inset-bottom)`. |
| A-076 / A-172 | 44px+ touch targets asserted as CSS rules: `.btn` 48px, `.btn.small`/`.icon-btn`/`#server-banner button`/`.modal input[type="text"]`/`#lan-token-input` 44px, `.switch` 52x44, `.modem-reset` 52px, band tiles 58px, skin tabbar buttons 54px. |
| A-018 / A-077 | `--ease` and `--header-h: 90px` tokens are defined in the white skin. |

## Manual-verify (not headless-assertable)

These depend on a real browser/UA or physical rendering and are confirmed
visually:

- **Reduced motion end-to-end**: actual animation/timing behavior when the OS
  "reduce motion" preference is on (the harness only checks the CSS source).
- **Safe-area insets**: `env(safe-area-inset-*)` resolution on a notched
  device — header top padding, modal bottom padding, toast position.
- **Sticky shell**: `.app-header`/`.tabbar` staying pinned while scrolling, and
  `overflow: clip` rounded-corner clipping without a nested scroll container.
- **Touch targets**: physical hit-testing of the 44px targets on a real device.
- **Canvas rendering**: `drawGraph()` output, A-006 dBm label clear of the top
  tick, A-111 outage gap markers, and the sr-only summary matching the drawn
  line — the canvas is stubbed headless.
- **Full-page Tab order / focus ring visibility** in a real browser.
- **A-055** sub-320px viewport reflow and **A-003** chip-value wrapping.
