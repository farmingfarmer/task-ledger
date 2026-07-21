# Dashboard assets

Drop-in directory for brand and supporting art. `dashboard_keck.html` references
these paths; every reference degrades gracefully (hidden, not broken) when a
file is absent, so the page works before assets arrive.

## Official marks -- source, do not generate

| File | Used as | Source |
|---|---|---|
| `keck-usc-logo.svg` | Header brandmark (30px tall, white/reversed variant on cardinal) | USC brand portal / Keck internal communications, with permission |
| `mark.svg` | Favicon | Same, or the neutral ledger mark below if official use is not cleared |

USC and Keck Medicine logos, the shield, and Trojan iconography are trademarks.
For an internal tool, get the real files and a nod from communications; never
generate lookalikes. The reversed (white) logo variant is the one that reads
on the cardinal header band.

## Generated supporting art -- specs

Shared language: line art, 2px strokes, muted taupe `#6B615C` on transparent,
gold `#FFC72C` permitted as ONE small accent per piece (the gold-means-source
contract applies even to illustrations), no text, no logos, no medical
imagery cliches (no caduceus, no crosses).

| File | Size | Concept | Shown when |
|---|---|---|---|
| `empty-board.svg` | 160x120 | an open, unmarked ledger page | a board has no visible tasks |
| `empty-deps.svg` | 160x120 | a clear, unobstructed path | nobody is waiting on this board |
| `empty-shares.svg` | 160x120 | a key resting beside an open hand | no access granted yet |
| `readme-hero.png` | 1280x400 | the pipeline as a stylized rail: transcript -> signals -> judged match -> review card -> ledger fact, cardinal chrome, gold only on the evidence hop | repo README |

Wiring the three empty-state images into the panels is a small edit
(an `<img>` above each `.empty` string with alt text and `onerror` hiding);
do it when the files exist rather than shipping dead references.

## Palette reference

Cardinal `#990000` (brand chrome only) - Gold `#FFC72C` (evidence/source
affordances only) - Ink `#2A2320` - Muted `#6B615C` - Surface `#F5F3F0` -
statuses unchanged (pine `#2E6E63`, brick `#B3402F` for blocked/overdue,
slate, quiet). Blocked-red and brand-cardinal are deliberately different
hues so alarms never read as branding.
