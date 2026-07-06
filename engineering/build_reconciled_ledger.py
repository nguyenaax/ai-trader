"""
Builds an AUTHORITATIVE trade ledger from Alpaca's real fill history, into a NEW table
`trades_reconciled` in scratch/trading_memory.db. Read-only against Alpaca; writes ONLY to
the new table. `trade_logs` is never touched.

Method: pull every filled order (paginated), sort by fill time, and FIFO-match sells against
buys per symbol to produce exact round-trip trades (realized PnL) plus still-open lots.
Validates open-lot quantities against live positions.

(Excerpt from the private repo, where it runs from the project root as tools/build_reconciled_ledger.py.)
"""
import os, sys, io, sqlite3
from collections import defaultdict, deque
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
from dotenv import load_dotenv
load_dotenv()
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetOrdersRequest
from alpaca.trading.enums import QueryOrderStatus

DB_PATH = "scratch/trading_memory.db"
API_KEY = os.environ["ALPACA_API_KEY"]; SECRET_KEY = os.environ["ALPACA_SECRET_KEY"]
PAPER = os.environ.get("ALPACA_PAPER", "true").lower() == "true"
client = TradingClient(API_KEY, SECRET_KEY, paper=PAPER)


def ff(x, d=0.0):
    try: return float(x)
    except (TypeError, ValueError): return d


def fetch_all_filled_orders():
    """Paginate through ALL orders (desc by submitted_at), dedupe by id, keep filled ones."""
    seen, fills = {}, []
    until = None
    for _ in range(40):  # safety cap: 40 * 500 = 20k orders
        req = GetOrdersRequest(status=QueryOrderStatus.ALL, limit=500, until=until, nested=False)
        batch = client.get_orders(filter=req)
        if not batch:
            break
        new = 0
        oldest = None
        for o in batch:
            oid = str(o.id)
            sub = getattr(o, "submitted_at", None) or getattr(o, "created_at", None)
            if oldest is None or (sub and sub < oldest):
                oldest = sub
            if oid in seen:
                continue
            seen[oid] = True
            new += 1
            fq = ff(getattr(o, "filled_qty", 0))
            fat = getattr(o, "filled_at", None)
            if fq > 0 and fat is not None:
                fills.append({
                    "symbol": o.symbol,
                    "side": getattr(o.side, "value", str(o.side)),
                    "qty": fq,
                    "price": ff(getattr(o, "filled_avg_price", 0)),
                    "time": fat,
                })
        if new == 0 or oldest is None:
            break
        until = oldest  # next page: older than the oldest we've seen
    return fills, len(seen)


def fifo_ledger(fills):
    """FIFO-match sells against buys per symbol -> (closed_trades, open_lots)."""
    by_symbol = defaultdict(list)
    for f in fills:
        by_symbol[f["symbol"]].append(f)

    closed, open_lots, warnings = [], [], []
    for sym, evs in by_symbol.items():
        evs.sort(key=lambda e: e["time"])
        lots = deque()  # (qty_remaining, price, time)
        for e in evs:
            if e["side"] == "buy":
                lots.append([e["qty"], e["price"], e["time"]])
            else:  # sell -> consume FIFO
                remaining = e["qty"]
                while remaining > 1e-9 and lots:
                    lot = lots[0]
                    take = min(remaining, lot[0])
                    pnl = (e["price"] - lot[1]) * take
                    pct = ((e["price"] / lot[1]) - 1.0) * 100 if lot[1] else 0.0
                    closed.append({
                        "symbol": sym, "qty": take,
                        "entry_time": lot[2].isoformat(), "entry_price": lot[1],
                        "exit_time": e["time"].isoformat(), "exit_price": e["price"],
                        "realized_pnl": pnl, "realized_pct": pct,
                    })
                    lot[0] -= take
                    remaining -= take
                    if lot[0] <= 1e-9:
                        lots.popleft()
                if remaining > 1e-9:
                    warnings.append(f"{sym}: sell of {remaining:g} sh with no matching buy (data gap)")
        for lot in lots:
            open_lots.append({
                "symbol": sym, "qty": lot[0],
                "entry_time": lot[2].isoformat(), "entry_price": lot[1],
            })
    return closed, open_lots, warnings


def write_table(closed, open_lots):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DROP TABLE IF EXISTS trades_reconciled")  # fully derived -> safe to rebuild
    c.execute("""
        CREATE TABLE trades_reconciled (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT, qty REAL,
            entry_time TEXT, entry_price REAL,
            exit_time TEXT, exit_price REAL,
            realized_pnl REAL, realized_pct REAL,
            status TEXT
        )
    """)
    for t in closed:
        c.execute("""INSERT INTO trades_reconciled
            (symbol,qty,entry_time,entry_price,exit_time,exit_price,realized_pnl,realized_pct,status)
            VALUES (?,?,?,?,?,?,?,?, 'closed')""",
            (t["symbol"], t["qty"], t["entry_time"], t["entry_price"],
             t["exit_time"], t["exit_price"], round(t["realized_pnl"], 4), round(t["realized_pct"], 4)))
    for t in open_lots:
        c.execute("""INSERT INTO trades_reconciled
            (symbol,qty,entry_time,entry_price,exit_time,exit_price,realized_pnl,realized_pct,status)
            VALUES (?,?,?,?,NULL,NULL,NULL,NULL, 'open')""",
            (t["symbol"], t["qty"], t["entry_time"], t["entry_price"]))
    # Convenience views for offline consumers (backtests / audit / ML)
    c.execute("DROP VIEW IF EXISTS reconciled_closed")
    c.execute("DROP VIEW IF EXISTS reconciled_open")
    c.execute("CREATE VIEW reconciled_closed AS SELECT * FROM trades_reconciled WHERE status='closed'")
    c.execute("CREATE VIEW reconciled_open   AS SELECT * FROM trades_reconciled WHERE status='open'")
    conn.commit()
    conn.close()


def main():
    print(f"mode: {'PAPER' if PAPER else 'LIVE'}  (read-only against Alpaca)\n")
    fills, total_orders = fetch_all_filled_orders()
    print(f"scanned {total_orders} orders -> {len(fills)} fills")

    closed, open_lots, warnings = fifo_ledger(fills)
    write_table(closed, open_lots)

    n_closed = len(closed)
    wins = sum(1 for t in closed if t["realized_pnl"] > 0)
    losses = sum(1 for t in closed if t["realized_pnl"] < 0)
    realized = sum(t["realized_pnl"] for t in closed)
    print(f"\nwrote trades_reconciled: {n_closed} closed lots, {len(open_lots)} open lots")
    print(f"  realized P/L (FIFO) . ${realized:,.2f}")
    print(f"  win rate ............ {wins}/{n_closed} = {(100*wins/n_closed if n_closed else 0):.1f}%  "
          f"(losses {losses})")

    # Validate open lots vs live positions
    pos = {p.symbol: ff(p.qty) for p in client.get_all_positions()}
    led_open = defaultdict(float)
    for l in open_lots:
        led_open[l["symbol"]] += l["qty"]
    syms = sorted(set(pos) | set(led_open))
    mism = [s for s in syms if abs(pos.get(s, 0) - led_open.get(s, 0)) >= 1]
    print(f"\nvalidation vs live positions: {len(syms)} symbols, {len(mism)} qty mismatches "
          f"(should be ~0 if fill history is complete)")
    for s in mism[:15]:
        print(f"  {s}: ledger open {led_open.get(s,0):g} vs Alpaca {pos.get(s,0):g}")

    if warnings:
        print(f"\n{len(warnings)} FIFO warnings (unmatched sells):")
        for w in warnings[:15]:
            print("  " + w)
    print("\n(trade_logs untouched; only trades_reconciled written.)")


if __name__ == "__main__":
    main()
