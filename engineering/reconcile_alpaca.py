"""
Read-only reconciliation between the local trade_logs DB and the live Alpaca account.

Diffs what the DATABASE believes it holds (rows with reward_pct IS NULL = "open")
against what ALPACA actually holds (get_all_positions). Surfaces:
  - HELD but NOT open in DB   -> a real position the DB never recorded (e.g. a manual entry)
  - OPEN in DB but NOT held   -> a phantom: DB missed the exit fill
  - HELD and open in DB       -> compares qty / avg entry for drift

Makes NO changes: no orders are placed, nothing is written to the DB.
(Excerpt from the private repo, where it runs from the project root as tools/reconcile_alpaca.py.)
"""
import os, sys, io, sqlite3
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from dotenv import load_dotenv
load_dotenv()

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetOrdersRequest
from alpaca.trading.enums import QueryOrderStatus

DB_PATH = "scratch/trading_memory.db"
API_KEY    = os.environ["ALPACA_API_KEY"]
SECRET_KEY = os.environ["ALPACA_SECRET_KEY"]
PAPER      = os.environ.get("ALPACA_PAPER", "true").lower() == "true"

client = TradingClient(API_KEY, SECRET_KEY, paper=PAPER)


def f(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def load_db_state():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    rows = c.execute("""
        SELECT ticker,
               COUNT(*) AS total,
               SUM(CASE WHEN reward_pct IS NULL THEN 1 ELSE 0 END) AS open_rows,
               SUM(CASE WHEN reward_pct IS NULL THEN qty ELSE 0 END) AS open_qty
        FROM trade_logs GROUP BY ticker
    """).fetchall()
    conn.close()
    state = {}
    for tk, total, open_rows, open_qty in rows:
        state[tk] = {"total": total, "open_rows": open_rows or 0, "open_qty": open_qty or 0.0}
    return state


def orders_for(symbol):
    try:
        req = GetOrdersRequest(status=QueryOrderStatus.ALL, symbols=[symbol], limit=100)
        return client.get_orders(filter=req)
    except Exception as e:
        print(f"    (could not fetch orders for {symbol}: {e})")
        return []


def main():
    print(f"Account mode: {'PAPER' if PAPER else 'LIVE'}\n")

    positions = client.get_all_positions()
    pos = {}
    for p in positions:
        pos[p.symbol] = {
            "qty": f(p.qty),
            "avg_entry": f(p.avg_entry_price),
            "cost_basis": f(p.cost_basis),
            "market_value": f(p.market_value),
            "upl": f(p.unrealized_pl),
            "uplpc": f(p.unrealized_plpc) * 100,
        }

    db = load_db_state()
    db_open = {tk: v for tk, v in db.items() if v["open_rows"] > 0}

    print(f"Alpaca open positions ...... {len(pos)}")
    print(f"DB tickers with open rows .. {len(db_open)}")
    print(f"DB tickers total ........... {len(db)}\n")

    held = set(pos)
    dbopen = set(db_open)

    held_not_in_db = sorted(held - dbopen)
    open_not_held  = sorted(dbopen - held)
    both           = sorted(held & dbopen)

    print("=" * 78)
    print(f"[1] HELD ON ALPACA but NO OPEN ROW IN DB  ({len(held_not_in_db)})")
    print("    -> real money the DB doesn't know it's holding")
    print("=" * 78)
    for tk in held_not_in_db:
        p = pos[tk]
        d = db.get(tk, {"total": 0, "closed": 0})
        print(f"\n  {tk}: {p['qty']:g} sh @ ${p['avg_entry']:.4f}  cost=${p['cost_basis']:.2f}  "
              f"uPL={p['uplpc']:+.2f}% (${p['upl']:+.2f})")
        print(f"    DB has {d['total']} row(s) for {tk}, all marked CLOSED (0 open).")
        os_ = orders_for(tk)
        fills = [o for o in os_ if str(getattr(o, 'status', '')).endswith('FILLED') or getattr(o, 'filled_at', None)]
        print(f"    Alpaca order history for {tk}: {len(os_)} order(s), {len(fills)} with fills:")
        for o in os_[:12]:
            side = getattr(o.side, 'value', o.side)
            st   = getattr(o.status, 'value', o.status)
            fq   = f(getattr(o, 'filled_qty', 0))
            fap  = getattr(o, 'filled_avg_price', None)
            fat  = getattr(o, 'filled_at', None)
            sub  = getattr(o, 'submitted_at', None)
            when = fat or sub
            print(f"      {str(when)[:19]}  {side:<4} qty={f(o.qty):g} filled={fq:g} "
                  f"@ {('$'+format(f(fap),'.4f')) if fap else '--':<9} [{st}]")

    print("\n" + "=" * 78)
    print(f"[2] OPEN IN DB but NOT HELD ON ALPACA  ({len(open_not_held)})")
    print("    -> phantom positions: DB missed the exit fill (bot still thinks it holds)")
    print("=" * 78)
    for tk in open_not_held:
        d = db_open[tk]
        print(f"  {tk}: DB shows {d['open_rows']} open row(s), ~{d['open_qty']:g} sh — but Alpaca holds NONE.")

    print("\n" + "=" * 78)
    print(f"[3] HELD AND OPEN IN DB — qty drift check  ({len(both)})")
    print("=" * 78)
    for tk in both:
        p = pos[tk]; d = db_open[tk]
        drift = "" if abs(p["qty"] - d["open_qty"]) < 1 else "  <-- MISMATCH"
        print(f"  {tk}: Alpaca {p['qty']:g} sh vs DB open {d['open_qty']:g} sh "
              f"({d['open_rows']} row(s)){drift}")

    # Portfolio-level sanity
    tot_cost = sum(p["cost_basis"] for p in pos.values())
    tot_mv   = sum(p["market_value"] for p in pos.values())
    tot_upl  = sum(p["upl"] for p in pos.values())
    print("\n" + "=" * 78)
    print("PORTFOLIO TOTALS (Alpaca)")
    print("=" * 78)
    print(f"  positions={len(pos)}  cost_basis=${tot_cost:,.2f}  market_value=${tot_mv:,.2f}  "
          f"unrealized_PL=${tot_upl:,.2f}")
    print("\n(Read-only reconciliation complete. No orders placed, DB unchanged.)")


if __name__ == "__main__":
    main()
