# AH Performance — Refinement Log
### Record of the Product Refinement Programme (Stages 1 → 2)
Coach-facing web application · ahperformance.ie · July 2026

This log records what was **implemented and shipped** during the refinement phase. It is the companion to `AH-Performance-UX-Review-Phase1.md` (the plan/roadmap) — this file is the record of what actually changed. All changes are presentation- and workflow-layer only: no database schema, business logic, or visual-identity changes; existing workflows preserved throughout.

---

## Stage 1 — Consistency & Trust *(shipped)*

1. **Unified "Requires Coach Attention".** The Dashboard now renders from the same `computeAttentionAlerts()` engine as the Athletes page, so the two can never disagree. The Dashboard's former inline rules (programme-not-assigned, wellbeing-high-stress with its "Reviewed" dismiss) were folded into that single engine. Fixed a latent bug where the dismiss called a non-existent `renderPTDashboard()`.
2. **Honest analytics chart.** "Compliance Trend" (a smooth line implying a time series) became **"Compliance by Athlete"** as a bar chart. `renderChart()` gained an optional `type` parameter defaulting to `'line'`, so no other chart changed. Calculations untouched.
3. **Header / branding consistency.** The embedded Programme Builder and Exercise Library no longer show their own "AH Performance" branding when embedded — they read as sections, not separate apps. Standalone use unaffected.
4. **Consistent status colour.** One `complianceColour()` helper (≥85 green · ≥70 amber · below red) used in both the roster row and the client-detail tile (the tile previously lacked the red tier).
5. **Informative empty states** added to Nutrition and Wellbeing.

*Deferred from Stage 1:* global timestamp normalisation (touches many render paths; higher risk than the stage warranted).

## Stage 1.1 — Theme Consistency *(shipped)*

- **One global theme, single source of truth** (`body.light-mode` + `localStorage['ah_theme']`). The coach gained access to the theme toggle (previously only on the client dashboard) via a shared `.ah-theme-toggle` class, so both roles drive one control.
- **Embedded tools inherit the parent theme.** The app broadcasts the active theme to both iframes (push on toggle, pull on load, re-push on page show); the Programme Builder received a light-palette variable override (it had none); the Exercise Library's independent toggle is hidden when embedded. Iframes reflect the parent and never persist their own theme — no duplicate state.
- Root cause verified: earlier "inversion" readings were a `getComputedStyle` artifact during the Library's 0.3s background transition, disproven by clean reload — not a code bug.

## Stage 2A — Desktop Experience *(shipped)*

Implemented in risk order (smallest first), one screen at a time.

1. **Messages** — the two-pane layout now fills the viewport: list scrolls left, thread scrolls centre, composer pinned at the bottom. The former `max-height:450px` cap that stranded the module in the top third is gone. Pure layout; no workflow change.
2. **Wellbeing** — the full-width card stack became a responsive tile grid (`auto-fill, minmax(340px,1fr)`), so the whole squad's check-ins are scannable at a glance, high-stress tiles still flagged. Same card, container layout only.
3. **Nutrition** — master-detail: a slim athlete list (the selector) on the left, the existing editor on the right, each row showing "Plan set" / "No plan yet" at a glance. The dropdown workflow is preserved — the `<select>` remains hidden as the value source `loadNutritionClient()` reads; the list drives it. Editor internals untouched.

All three: scoped `<style>` blocks + a container class + a media-query collapse to a single column on mobile.

## Stage 2B — Workflow Refinement *(shipped)*

Theme: **act on the athlete from where you already are** — remove re-navigation and lost context.

1. **"Message" opens the athlete's thread directly.** New `openClientConversation(clientId)` finds the athlete's conversation (or creates it, reusing the existing creation shape — never a duplicate, matched by clientId or name) and opens it in the Stage-2A Messages view. Wired to the roster row's message icon and the profile's "Message" button, which previously only navigated to the Messages page. The most-repeated coaching action drops from ~3 steps to 1, with no lost context.
2. **Dashboard "Recent Activity" rows open the athlete**, matching the attention panel beside them. Clickable only when the item resolves to a real client (System rows stay inert).

Roster action icons also gained hover tooltips ("Message …", "Open profile") for unambiguous meaning.

## Stage 3A — Trust & Data Integrity *(shipped)*

A data-integrity phase, not a feature phase. Full findings in `AH-Performance-Stage3-Investigation.md`.

**1. Compliance denominator corrected.** `_updateClientCompliance()` derived its denominator from `CLIENT_PROGRAMMES[id].weeks` — a property that has never existed (the object is keyed directly by week number). The lookup failed silently and fell through to a hardcoded "assume 4 sessions/week" for *every* athlete, inflating compliance for anyone training more than 4× per week. Now reads the real programme shape via `_programmedSessionsPerWeek()`, with a legacy `.weeks` guard retained for safety.

**2. Compliance staleness fixed.** The figure was only recalculated when an *athlete* logged a session, so anyone who stopped training kept their last-known score indefinitely (Amy displayed 25% after 41 days without training). `refreshAllCompliance()` now recomputes every athlete when the coach opens the app, before alerts, KPIs or roster colours are rendered. It deliberately touches compliance only — not `lastActivity`.

**3. Analytics made honest.** All four KPI tiles were hardcoded markup that `renderPTAnalytics()` never touched. Replaced with values computed from live data; metrics with no data source were removed rather than faked.

**Verified impact on production data** (before → after):

| Athlete | Displayed | Corrected | Reality |
|---|---|---|---|
| Adam Heayberd | 100% | **88%** | 7 of 8 programmed sessions |
| Amy Treanor | 25% | **0%** | 0 sessions in last 7 days |
| Brian Ulliott | 75% | **0%** | 0 sessions in last 7 days |
| Luka / Ellie | 0% | 0% | unchanged |
| **Roster average** | 40% | **18%** | Analytics previously claimed **89%** |

*Expected consequence:* the attention list will grow, because the `< 60%` adherence alert now fires on accurate figures. This is the correction working.

---

## Metric Definitions — source of truth

| Metric | Definition | Source of truth | Where shown |
|---|---|---|---|
| **Compliance / Adherence** | Sessions completed in the last 7 days ÷ sessions programmed for the athlete's current week, capped at 100%. If no programme is assigned, falls back to a nominal 4-session week. | `_computeCompliance()` — the **only** place compliance is calculated | Roster row, client-detail tile, attention alerts (`<60%`), Analytics KPI + chart |
| **Programmed sessions/week** | Session count of the athlete's highest-numbered programme week | `_programmedSessionsPerWeek()`, reading `CLIENT_PROGRAMMES[id][weekNumber].sessions` | Feeds compliance only |
| **Avg Compliance** (Analytics) | Mean compliance across athletes with status ≠ inactive | `renderPTAnalytics()` | Analytics KPI |
| **Avg Time Coached** (Analytics) | Mean months since `client.joined`, active athletes with a parseable join date | `renderPTAnalytics()` | Analytics KPI |
| **Compliance by Athlete** (chart) | Per-athlete compliance, same population as the Avg Compliance KPI | `renderPTAnalytics()` | Analytics chart |
| **Client Retention** | **Removed** — no retention history is stored | — | — |
| **Monthly Revenue** | **Removed** — no pricing or fee data is stored on clients | — | — |

Principle established: **compliance is derived data, never stored truth.** It is recomputed on read, not persisted as a fact.

---

## Lessons Learned

Principles that emerged during the refinement programme — these become part of the evolving AH Performance design language.

- **Preserve context wherever possible.** Once a coach has selected or begun working with an athlete, every subsequent action should maintain that context unless there is a compelling reason not to. Reducing context switching is one of the highest-value workflow refinements.
- **Friction lives in the seams, not the screens.** The biggest daily wins came from the *transitions between* pages — the coach losing context and re-navigating — not from any single screen. Follow the journey, not the screen list.
- **Every reference to an athlete should be a doorway to that athlete.** A name or activity line the coach can see but not act on is friction. Making these consistently clickable is a cheap, repeatable pattern.
- **Consistency is itself a feature.** When one panel is clickable and its neighbour isn't, the coach hesitates. Matching behaviours across similar elements reduces cognitive load more than a new capability.
- **The strongest refinements reduce steps without adding pixels.** The best changes were invisible — same screen, fewer clicks. "Can this be reached in one fewer step?" beats "what can we add?"
- **One source of truth per concept.** Divergence (two attention feeds, two theme states) is where trust breaks. Consolidate to one engine and have every view render it.
- **Design for the desktop canvas; don't stretch mobile.** Master-detail and full-height panels use the space deliberately; a lone control on an empty page reads as unfinished.
- **Deliberate restraint is part of the craft.** Documenting *considered-but-declined* changes keeps the interface quiet and leaves an intentional backlog rather than silently expanding scope.
- **Verify against rendered reality, not just measurements.** A `getComputedStyle` reading mid-transition can lie; a clean reload and a screenshot are the ground truth.
- **Trust before intelligence.** A dashboard is a presentation of data. Presenting wrong numbers more beautifully makes wrong decisions faster and with more confidence. Establish data accuracy before layering insight on top of it.
- **Derived data must be recomputed on read, never trusted as stored truth.** Compliance was frozen at whatever it was when the athlete last logged a session. Any metric that describes "recent" behaviour decays the moment it is written down.
- **A silent fallback is more dangerous than an error.** `if (!x) x = 4` masked a broken lookup for the single most consequential number in the product. Defaults should be loud, or absent.
- **Never display a number you cannot source.** If the data does not exist, remove the metric rather than fill it with a plausible placeholder. Placeholder figures survive into production and get trusted.
- **The same bug class recurs — check for its siblings.** The compliance defect was the *same* failed `.weeks` lookup found in Stage 1's programme loader. When a defect is found, search for the pattern, not just the instance.

---

## Deferred Ideas (future consideration)

Reviewed and intentionally **not** implemented in Stages 1–2, to respect "smallest safe change" and the Quiet Interface. Candidates for future stages:

- **Attention/activity deep-linking to the specific tab** — e.g. a check-in alert opening the athlete's Check-Ins tab, a wellbeing alert opening Wellbeing. Adds alert-type→tab mapping; a clear Momentum win for a later stage.
- **Roster programme quick-action** — a one-click "open programme" on roster rows. Deferred to avoid a third icon per row (visual weight); the profile is one click away with "Edit Programme".
- **Persistent current-athlete context across tools** — carry the selected athlete from profile into Nutrition/Programmes/Messages without re-selecting. The largest Momentum win and the natural next step for the context-preservation principle; larger effort, warrants its own stage.
- **Wellbeing "latest-per-client" view** — show one current tile per athlete (plus a squad summary) rather than all historical entries; calmer at scale.
- **Global timestamp normalisation** — consistent relative time ("2h ago") across lists; broad change, handle as a focused pass with its own testing.
- **Messages iframe/transition polish** — e.g. dropping the Library's 0.3s background transition for instant theme switches inside embedded tools.
- **Triage-first "Coach's Day" dashboard** (roadmap Stage 4) — the ambitious landing-page rework; deliberately last.

---

*Status: Stage 1, 1.1, 2A and 2B shipped and verified live. Stage 2 complete.*

---

## Stage 4 — Coaching-Use Refinements (10 Aug 2026)

Four independent friction points surfaced by real coaching use. Investigate-first, smallest-safe-change, no redesign.

### 4.1 Progress photo week ordering — BUG (presentation)

**Root cause.** `renderCPPhotos()` and `renderDetailPhotos()` ordered check-ins with
`(a.date||'').localeCompare(b.date||'')`. But `ci.date` is written by `submitCheckin()` as a
human-readable string (`"Mon 5 Aug"`), not an ISO date. String comparison therefore sorted by
**weekday name** first — Fri < Mon < Sat < Sun < Thu < Tue — producing an apparently random
week order. Reproduced exactly: 13 weekly check-ins from Mon 5 Jan sorted to `2, 7, 11, 3, 5, 9, 8, 12, 4, 13, 1, 6, 10`.

**Fix.** New shared `_sortCheckinsByWeek()` sorts on the numeric `ci.week` value (the
authoritative sequence field), falling back to the creation timestamp encoded in `ci.id` only for
legacy records with no week. Presentation only — no stored data touched.

### 4.2 Two-week photo comparison — NEW (UX)

Coach/athlete can pick an earlier week, a later week and a pose, then crossfade between them
in a single frame. Reuses the existing `ci.photos[pose]` data URLs; no new storage, no image
processing, nothing written.

**Crossfade chosen over a wipe.** Progress photos are free-hand phone photos at whatever
framing the athlete used — they are not registered. A wipe divider would break the body at the
seam whenever framing differs, reading as change that isn't there. A crossfade degrades
honestly and gives the same A/B flick coaches actually use.

**Missing photos are never substituted.** One pose missing → the available image is shown, the
slider disables, and the UI states which week lacks it. Both missing → an explicit message.

### 4.3 Outdoor run prescribed time landing in Distance — BUG (data mapping)

**Root cause.** Cardio sets use the convention `weight = TIME`, `reps = DISTANCE`. The
pre-populate step in `startWorkout()` sent every prescription to `reps` unless it literally
contained a colon, so `"1 × 30 min"` surfaced to the athlete under **Distance**. Compounding it,
`parseSetRep()`'s `(\S+)` capture strips the unit — `"1 × 30 min"` yields `reps: "30"` — so the
unit needed to decide the field was already gone by the time the decision was made.

**Fix.** New `_classifyCardioPrescription(repsToken, rawSr)` re-reads the raw `sr` string,
recovers the unit, and routes to the correct column. Time units normalise to `mm:ss` (which is
exactly what the existing save-time aggregator parses). Distance units and **unitless values keep
their previous destination**, so `"3 × 400"` behaves as it always has. Display/mapping only —
no historical workout migrated.

### 4.4 Nutrition action controls — UX (hierarchy)

Scan Label / Custom Food / My Recipes sat *below* Recent Foods, reading as part of the history.
Moved above the list into a grouped `.fd-action-bar` — subtly raised surface, small uppercase
"Add Something New" label, 3-across icon+label grid. Compact by design: ~64px tall, 3 columns
at every width, so it does not dominate on small screens. No functional change; the same three
handlers fire.

### Data integrity

No stored data, database record or schema was changed. `app.py`, `ah-sync-data.json` and all
persisted structures are untouched. The only write introduced is into `wlSetData`, the transient
in-memory scratch state for a workout in progress — exactly where it was before.

### Lesson added

- **A human-readable string is not a sort key.** `ci.date` was formatted for display and then
  reused for ordering. Any field intended for the eye will eventually be sorted by the machine —
  keep the sortable value (here, `week`) as the ordering source of truth and let the pretty string
  stay pretty.

### Deferred (found, not fixed)

- **`ci.date` has no ISO counterpart.** Check-ins store only `"Mon 5 Aug"`; the matching
  `PROGRESS_DATA` measurement entry does store ISO. Adding an ISO `dateISO` field to *new*
  check-ins would make date-based ordering safe everywhere without migrating history. Out of
  scope here — week ordering is now correct regardless.
- **Other check-in list views** (`renderCheckins`, coach check-in feed) were not re-ordered.
  They present newest-first deliberately, which is correct for a feed. Flagged only so the
  divergence is a recorded decision rather than an oversight.

### Pre-deploy baseline (verified 10 Aug 2026)

Live `ahperformance.ie` was captured before deployment:

| | Live (pre-deploy) | Local pre-refinement backup |
|---|---|---|
| SHA-256 (first 16) | `2a3d8c5d83570efd` | `2a3d8c5d83570efd` |
| Lines | 15,228 | 15,228 |
| sw.js cache | `ah-performance-v126` | `ah-performance-v126` |

**The two are byte-identical.** The main working folder had no undeployed drift, so this
release ships exactly the Stage 4 changes and the `sw.js` bump to `v127` — nothing else.
Live confirmed to contain none of the new markers (`_sortCheckinsByWeek`,
`_classifyCardioPrescription`, `_pcCompareHtml`, `fd-action-bar`) and to still carry the old
`date.localeCompare` sort.

*Status: Stage 4 implemented and pre-deploy verified. Deployment is a manual push by Adam —
no git working tree is reachable from the assistant's environment. Post-deploy live
verification outstanding.*

