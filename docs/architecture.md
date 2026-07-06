# Oracle VM System Architecture Blueprint

> Blueprint of the private implementation. Selected infrastructure excerpts are published
> under [`engineering/`](../engineering/); agent prompts and strategy logic are not.

## 1. System Overview
The Oracle VM is an asymmetric, AI-driven algorithmic trading system designed for individual equities. It leverages Gemini 2.5 Flash for both high-velocity live market evaluation and offline structural forensics. The system is defensive by design, strictly prioritizing capital preservation, fixed risk parameters, and automated triage.

## 2. Core Execution Loop (`ai_trader.py`)
This is the live "Satellite" loop that executes during market hours.
*   **Holiday & Calendar Defense:** Execution is gated by an `is_market_open()` check via the Alpaca Clock API to prevent off-hours or holiday order queues.
*   **Ticker Masking Protocol:** Raw tickers (e.g., `NVDA`) are hashed to generic tokens (e.g., `ASSET_1`) before LLM ingestion to strip pre-trained brand bias, ensuring decisions are purely technical.
*   **Critic Agent (Gemini 2.5 Flash):** Evaluates the masked technical payload and returns a deterministic execution signal based strictly on structural setup.
*   **Order Execution:** Orders are sent to Alpaca exclusively as Bracket Orders.

## 3. Hardcoded Risk Parameters (Immutable)
To prevent LLM hallucination from risking capital, all risk management is hardcoded and unchangeable by the AI agents. Exact values are private; the structure is:
*   **Position Sizing:** Fixed dollar allocation per trade (parity sizing).
*   **Hard Stop-Loss:** Fixed percentage below the entry fill price.
*   **Take Profit:** Fixed percentage above the entry fill price.
*   **Breakeven Ratchet:** Once a position clears a set profit threshold, the stop-loss leg is dynamically dragged up to breakeven.
*   **Asset Restrictions:** Individual equities only; ETFs excluded.

## 4. The Defense & Filter Pipeline
Before any asset is sent to the Critic Agent, it must survive the triage filters:
*   **Earnings Blackout:** Any ticker reporting earnings inside a short blackout window is instantly rejected.
*   **Live Position Guard:** Tickers already held on the Alpaca account are removed from the candidate set before evaluation (`get_current_positions()`), so the bot does not re-enter an open name.
*   **Re-entry Cooldown / Anti-Stacking:** `database_manager.is_on_cooldown()` blocks a ticker that already has *any* recent entry — or a still-open position — in the same name. (A prior version only blocked after a *realized* loss; while the first position was still open its `reward_pct` was NULL, so nothing matched and the bot could stack a second entry into the same name at ~2× intended exposure — the "revenge order" bug. The current check keys on the entry timestamp and open-position state, and has held new stacking to zero.)
*   **Liquidity Gate:** Rejects any candidate whose average dollar-volume is below a calibrated floor (`MIN_DOLLAR_VOL`), computed from the same daily bars the equilibrium engine already fetches. Rationale: on thin micro-caps the hard stop is a stop-*market* order that gaps through and fills well below the trigger; fill-history analysis attributed the dominant share of realized losses to this slippage. A threshold sweep against the reconciled ledger (with a time-split robustness check) showed a single volume floor cleanly separates the offenders — a volatility/ATR gate was tested and rejected because it forfeited liquid winners. Fails closed: if dollar-volume can't be computed, the ticker is skipped.
*   **Telemetry Logging:** Every rejected ticker and the reason for rejection is logged to `triage_history.csv` for weekend analysis.

## 5. Real-Time Fill Listener (`alpaca_listener.py`)
A persistent WebSocket listener subscribed to Alpaca trade updates, closing the loop between entries and exits.
*   **Function:** On every SELL fill it calls `database_manager.log_trade_exit()`, which stamps the exit price, realized `reward_pct`, and exit reason onto the matching open row in `trade_logs`. Entries are logged by `ai_trader.py`; exits are logged here.
*   **Hosting:** Runs as the `ai-trader-listener` systemd service (`Restart=always`, enabled on boot, unbuffered output to `listener.log`). It records exits **going forward only** — it does not backfill historical fills.

## 6. Offline Forensic Engine (`weekend_analysis.py`)
The weekend audit loop runs offline, strictly separated from live capital.
*   **Auditor Agent (Gemini 2.5 Flash):** Configured with a low temperature to prevent creative hallucination. (Switched from 2.5 Pro, which the API's free tier disallows.)
*   **Data Ingestion:** Reads the broker-reconciled ledger `trades_reconciled` (real round-trips with true realized P/L) — falling back to `trade_outcomes_clean`, then raw `trade_logs`, if the ledger has not been rebuilt — alongside `triage_history.csv` (filter efficiencies).
*   **Output:** Generates a structured markdown report (`weekend_audit_report.md`) detailing exactly 3 high-conviction parameter adjustments for the upcoming week based solely on empirical data.

## 7. Data Integrity & Reconciliation
Because `trade_logs` is self-reported by the bot, it can drift from the broker (missed exit fills, unlogged bracket/manual entries). Two read-only tools maintain an authoritative view, and neither mutates `trade_logs`:
*   **`engineering/reconcile_alpaca.py`:** Diffs live Alpaca positions against `trade_logs`, bucketing discrepancies as held-but-unlogged, phantom-open (DB thinks it holds, broker doesn't), and quantity-mismatch.
*   **`engineering/build_reconciled_ledger.py`:** Rebuilds table `trades_reconciled` by FIFO-matching every real Alpaca fill into round-trip trades (realized P/L) plus still-open lots, validated against live positions (0 quantity mismatch). This is the source of truth for backtesting, win-rate, and ML.
*   **Derived views:** `reconciled_closed` / `reconciled_open` are created by the published ledger builder; the self-log de-stacking views (`trade_logs_flagged` / `trade_logs_clean` / `trade_outcomes_clean`) are created by the private schema module.

## 8. Infrastructure & Hosting
*   **Environment:** Headless Oracle Linux VM.
*   **Scheduling:** `cron` runs `ai_trader.py` every 30 minutes during market hours (`*/30 9-16 * * 1-5`) and `weekend_analysis.py` on Sundays at 09:00; `alpaca_listener.py` runs continuously under systemd.
*   **Broker Integration:** Alpaca Trading API (currently the paper account).
*   **Database:** Local SQLite at `scratch/trading_memory.db` — primary table `trade_logs`, plus the `trades_reconciled` table and the derived views above. WAL mode is enabled for concurrent listener/trader read-write.
