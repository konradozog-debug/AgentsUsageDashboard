# Design System — Agent Stats

## 1. Design Tokens

### Semantic Colors

Functional colors, not decorative — depend on metric value:

| Token | When | Meaning |
|---|---|---|
| `status-ok` | <40% usage | Metric within normal range |
| `status-warn` | 40–70% usage | Approaching limit |
| `status-critical` | >70% usage | Near/at limit |
| `status-offline` | No data | Agent not responding |
| `status-stale` | Data older than 2× refresh interval | Data may be outdated |

### Agent Identity Colors

Each agent has an assigned color in three variants:

| Token | Variants |
|---|---|
| `agent-codex` | solid, muted (badge background), dimmed (offline) |
| `agent-kimi` | solid, muted, dimmed |
| `agent-claude` | solid, muted, dimmed |
| `agent-zai` | solid, muted, dimmed |

### Surfaces

| Token | Usage |
|---|---|
| `surface-base` | Application background |
| `surface-card` | Card background |
| `surface-card-hover` | Card hover state |
| `surface-elevated` | Tooltip, popup |
| `surface-bar-track` | Progress bar background |
| `border-default` | Card borders |
| `border-subtle` | Separators |

### Text

| Token | Usage |
|---|---|
| `text-primary` | Main text |
| `text-secondary` | Labels, descriptions |
| `text-muted` | Timestamps, metadata |
| `text-disabled` | Inactive elements |

### Typography

| Token | Usage |
|---|---|
| `font-mono` | Numeric values, percentages, timestamps |
| `font-body` | Descriptions, labels |

Sizes: headline (summary card value), label, body, caption, micro (Stream Deck).

### Spacing

Base unit 4px, scale: xs, sm, md, lg, xl. Must work at very small sizes (Stream Deck 72×72px).

### Breakpoints

| Token | Size | Target |
|---|---|---|
| `deck-icon` | 72×72px | Single Stream Deck button |
| `deck-lcd` | 800×100px | Stream Deck+ LCD strip |
| `widget` | ~300×200px | Overlay/widget |
| `mobile` | 375px+ | Phone |
| `desktop` | 1024px+ | Browser |

## 2. Data States

Each agent can be in one of these states — the design must handle each:

| State | When | Visual |
|---|---|---|
| `loading` | First fetch after start | Skeleton/placeholder |
| `ok` | Fresh data, no errors | Normal presentation |
| `stale` | Data older than 2× interval | Data visible but marked as outdated |
| `warning` | Usage 40–70% | Metric color change |
| `critical` | Usage >70% or limit_reached | Color change + emphasis |
| `error` | Auth failed / timeout | Error message instead of data |
| `offline` | No cookies / not logged in | CTA to log in |
| `expired` | Session expired (401) | CTA to re-login |

## 3. Components

### AgentDot
Circle identifying an agent (color + state). Variants: normal, pulsing (live), dimmed (offline). Sizes: micro (Stream Deck), sm, md.

### UsageBar
Label + percentage value + progress bar + optional reset timer. Fill color depends on value (ok/warn/critical). Must work at 72px width (Stream Deck: bar only, no label).

### UsageValue
Standalone percentage value, large, mono. Color depends on threshold. On Stream Deck this is the only element: "73%" in color.

### AgentCard
Header: dot + name + badge (plan/tier). Body: list of UsageBars. Footer: additional info (credits, plan). States: ok, error, offline, loading. On Stream Deck: does not exist — replaced by AgentDot + UsageValue.

### SummaryCard
Label (caps, muted) + large value + sublabel. Four cards: session avg, weekly avg, health, active count.

### StatusIndicator
Row of AgentDots with names — quick overview of what's working. On Stream Deck LCD strip: AgentDots + UsageValues in a row.

### ResetTimer
Countdown to window reset. Format: "in 3h 21m" / "in 14m" / "expired". On Stream Deck: optional, below percentage value.

### SetupBanner
Visible when one or more agents are offline. Link to Firefox GUI + instructions. Does not exist on Stream Deck.

### Chart (BarChart)
Codex daily breakdown, last 14 days. Web only — no space on Stream Deck.

## 4. Layouts per Target

### Desktop (1024px+)
```
[Header: logo + name + StatusIndicator + refresh btn]
[SummaryCard × 4 in a row]
[AgentCard × 4 in a row]
[Chart]
[Footer]
```

### Mobile (375px+)
```
[Header]
[SummaryCard × 2 + 2]
[AgentCard stacked vertically]
[Chart]
```

### Stream Deck icon (72×72px)
```
Per agent, separate button:
[AgentDot centered]
[UsageValue — main metric, e.g. session %]
[Background color = status]
```

### Stream Deck LCD strip (800×100px)
```
Four agents in a row:
[Dot + "Codex" + 42%] [Dot + "Kimi" + 73%] [Dot + "Claude" + 17%] [Dot + "Z" + 5%]
Color of each value = status
Optionally below: reset timers
```

## 5. Iconography

Each agent needs an icon/glyph (not a logo — licensing). Status icons: checkmark (ok), warning triangle (warn), X (error), clock (stale). Must work at 16×16px (Stream Deck micro).

## 6. Animations / Transitions

Progress bar fill: ease-out ~500ms on value change. Card fade-in on first render. Pulsing dot during live refresh. On Stream Deck: no animations, static rendering.
