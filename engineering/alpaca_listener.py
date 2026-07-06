"""
Real-time fill listener — excerpt from the private ai-trader repo.

Subscribes to Alpaca's trade-update WebSocket and, on every SELL fill, records the exit
(price, realized return, reason) via `database_manager.log_trade_exit`. The `database_manager`
module (SQLite schema + write helpers) is part of the private system and is not published here.
In production this runs as a `Restart=always` systemd service (see deploy/).
"""
import os
from datetime import datetime
from dotenv import load_dotenv

from alpaca.trading.stream import TradingStream
from alpaca.trading.models import TradeUpdate
from alpaca.trading.enums import TradeEvent, OrderSide

import database_manager

load_dotenv()
ALPACA_API_KEY = os.environ.get('ALPACA_API_KEY')
ALPACA_SECRET_KEY = os.environ.get('ALPACA_SECRET_KEY')

# Removed CSV process_reward logic in favor of unified SQLite logging via database_manager

async def trade_update_handler(data: TradeUpdate):
    """
    Handles incoming trade events from the Alpaca WebSocket.
    """
    event = data.event
    order = data.order
    
    # We only care about fill events for SELL orders (Bracket Orders closing out)
    if event == TradeEvent.FILL and order.side == OrderSide.SELL:
        ticker = order.symbol
        # data.price is the execution price of this specific fill
        sell_price = float(data.price) if data.price else 0.0
        
        if sell_price > 0:
            print(f"[{datetime.now().isoformat()}] Received SELL fill for {ticker} @ ${sell_price}")
            # The database manager does the math and updates the row automatically
            exit_reason = str(order.order_type.value) if hasattr(order, 'order_type') and order.order_type else "unknown"
            database_manager.log_trade_exit(ticker, sell_price, exit_reason=exit_reason)

def main():
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        print("ERROR: Alpaca API keys not found in environment.")
        return

    print("Starting Alpaca WebSocket fill listener — logging SELL exits to trade_logs...")
    print("Listening for trade fills...")
    
    stream = TradingStream(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=True)
    stream.subscribe_trade_updates(trade_update_handler)
    
    try:
        stream.run()
    except KeyboardInterrupt:
        print("\nStopping listener...")

if __name__ == "__main__":
    main()
