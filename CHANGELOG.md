# Strategy Changelog

How the trading strategy itself has evolved — entry/exit policy, risk
gates, and the reasoning behind each change. Engineering work (refactors,
infrastructure, UI) is deliberately out of scope here; this file answers
one question: *what does the system believe about trading, and when did
that belief change?*

Dates are US-style (MM/DD/YYYY).

---

## 08/07/2026 — News confluence: one position per market, many opinions

Until now each news item was judged alone. A second headline on a market we
already held was skipped (`fill:position_exists`) and forgotten, and a headline
arguing the *other* side opened a second position — which on Polymarket, where
YES + NO settle to exactly $1, is a locked loss of both spreads with no upside.
That second case was a plain bug, not a hedge.

Both now attach to the position that blocked them, as an append-only
`position_signal` ledger. The belief this encodes: **a repeat headline in the
same direction is evidence the move is real and the position deserves room to
breathe; a headline in the opposite direction is evidence the move is about to
revert and the position deserves a tighter leash.**

A new `ConfluenceExitV0` exit impl picks its peak-drawdown threshold from that
ledger — `solo` 0.30, `reinforced` off by default, `contested` 0.10 — with an
optional `contested_close_after` that exits outright once the thesis has been
contradicted N times. `stop_loss_pct` is deliberately *not* state-keyed:
confluence widens how much banked gain a position may give back, never how much
capital it may lose.

Two asymmetries were chosen on purpose. Support **decays** on a TTL (default
2h), so one duplicate headline in the first minute can't hold drawdown
protection off for the rest of the position's life; contradiction **latches**,
because a disputed thesis stays disputed no matter what agrees later. The
baseline `ThresholdExitV0` is untouched — this is a canvas-selectable variant,
and the backtest harness can A/B the two over real history before it goes near
a live run.

Follow-up fix the same day: `side_lock`, `heat_cap_usd`, the A4 kill switches,
and `same_market_cooldown_minutes` / `same_market_lifetime_lockout` were all
returning `skip` inside the entry section *before* an `OrderIntent` was ever
built — so on an account with any of those configured (heat_cap in
particular, since it trips exactly when a strategy has the most open risk),
repeat news on an already-held market never reached the executor and the
confluence ledger silently never grew. None of those gates represent new
capital risk when a position on that market already exists — the executor's
own duplicate/opposite-side check guarantees no fill happens regardless — so
they now defer to it instead.

## 06/01/2026 — The strategy canvas becomes the operating surface

The canvas page was promoted from a configuration sketchpad to the actual
control plane: a working **Run / Pause** for the pipeline, a readiness bar
that shows which sections block a start, and a calm **Paper | Live**
toggle. No change to trade logic — the change is that every strategy
parameter edit now happens where its effect is visible, and hot-reloads
into the running pipeline without a restart.

## 05/25/2026 — Kill switch: three independent brakes

Added a circuit-breaker layer in front of entry with three independently
configurable trips: `kill_max_consecutive_losses`, `kill_daily_loss_usd`,
and a drawdown brake. Rationale: at grain-scale stakes the realistic
worst case isn't one bad trade, it's a *bad afternoon* — a news regime
the model misreads repeatedly. The brakes are deliberately dumb counters,
not model-driven: when the system is wrong in a correlated way, the last
thing to trust is the same model's opinion about whether to keep going.

## 05/25/2026 — Settlement as a first-class exit

Resolved markets now settle positions automatically at the 0/1 outcome
price. Before this, a position whose market resolved just sat there.
Settlement joins the closed set of exit paths — the strategy's exit
philosophy is that a position can leave the book in exactly four ways:
**settlement, circuit-breaker, the strategy's own exit rules, or a manual
click**. There is intentionally no fifth "the system reconsidered the
thesis" path (see 05/24).

## 05/24/2026 — Live execution model: integer shares, resting limit orders

First live-capable execution policy: orders go out as limit orders at the
touch, quantized to **whole shares** so the notional always lands on
clean cents (the venue rejects sub-cent maker amounts), with a minimum
notional floor above the venue's $1 minimum. Paper and live share the
same code path through a dispatcher, so paper results stay an honest
rehearsal of live behavior.

## 05/24/2026 — Anti-churn gates on entry

Three new optional gates, all motivated by the same observation in paper
trading — the model loves re-entering markets it just lost in:

- `same_market_cooldown_minutes` — after a stop-loss, the market is
  off-limits for a window. Blocks the stop→re-enter→stop whipsaw loop.
- `same_market_lifetime_lockout` — optionally, one shot per market, ever.
- `heat_cap_usd` — a cap on total open exposure, so a burst of correlated
  news (one geopolitical narrative spawning five related markets) can't
  stack the whole book onto a single thesis.

## 05/24/2026 — Exit policy v1: three thresholds, strict precedence

The exit section settled on three rules evaluated every tick against the
held side's bid, with precedence **stop-loss → peak-drawdown →
take-profit**:

- `stop_loss_pct` (default −15%) — the absolute loss circuit, checked first.
- `peak_drawdown_pct` (default 12% retrace from the peak) — locks in
  gains, but only once the peak gain is *meaningful* (a floor in both USD
  and % of cost), so noise around entry can't trigger it.
- `take_profit_pct` (default +20%) — the absolute ceiling, checked last.

Two things were considered and **deliberately rejected**: a trailing-stop
variant with cost-adjusted peaks (more parameters than the data can
justify at this scale), and asking the LLM to reconsider open positions
("position review"). The latter is a philosophical line: the model gets
exactly one decision per thesis, at entry. Letting it re-litigate open
positions converts every losing trade into a conversation.

## 05/23/2026 — Price-move veto: don't buy news the market already priced

Entry gained an optional veto: if the market has already moved more than
`veto_move_threshold` (default 10%) within `veto_window_min` (default
60 min) of the news item, the trade is skipped. The edge model compares
model probability against the *current* price, but a price that already
jumped is evidence the news is stale or consensus — exactly the trades
where a freshness-based edge is an illusion.

## 05/22/2026 — Edge-threshold entry

The entry decision became a single inequality: trade only when the
analyzer's probability estimate diverges from the market price by at
least `min_edge` (default 5¢), with guards for `max_spread` (illiquid
books overstate edge) and `slippage_tolerance`. Fixed `order_size_usd`
per trade — position sizing is intentionally flat until there's enough
fill history to justify anything cleverer (Kelly-style sizing on a
20-trade sample is noise worship).

## 05/19–05/21/2026 — News → market matching, then an LLM with one job

The signal chain took its current shape:

- An **embedding filter** (MiniLM, cosine similarity) ranks the live
  market catalog against each news item and passes the `top_k` survivors
  above `similarity_threshold`. Cheap, local, and it means the expensive
  step only ever sees plausibly-related markets.
- An **LLM analyzer** reads the news plus the candidate markets and emits
  a probability (`p_yes`), a confidence grade, and a written rationale.
  A `min_confidence` gate drops low-conviction calls before they reach
  entry. The prompt is deliberately skeptical — stale news (>2h),
  ambiguous resolution criteria, or a direction mismatch all force a
  downgrade.

## Mid-May 2026 — Founding decision: sections, not strategies

openPoly didn't port its predecessor's strategy. Instead, the rules that
had accumulated inside a monolithic strategy were **atomized into typed,
swappable pipeline sections** (news source → embedding filter → analyzer
→ entry → exit), each with a declared config schema and its own
observability. The bet: at experimental scale, the ability to measure and
swap one rule at a time is worth more than any individual rule. Every
entry above is a consequence of that choice.
