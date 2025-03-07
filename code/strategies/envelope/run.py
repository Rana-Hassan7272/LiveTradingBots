#!/usr/bin/env python3
"""
Final Hedged Bot Code for Grid Scalping with Sell Trigger

This bot reserves the current market price and immediately places both a long and a short order at the reserved price.
For example, if the reserved price is 89,000 USD, it enters:
  - A long position at 89,000 USD with:
      • Stop loss = reserved - STOP_LOSS_OFFSET (e.g., 89,000 - 2 = 88,998)
      • Fixed Trailing1 stop = reserved + ENTRY_TRIGGER_OFFSET (e.g., 89,000 + 40 = 89,040)
  - A short position at 89,000 USD with:
      • Stop loss = reserved + STOP_LOSS_OFFSET (e.g., 89,000 + 2 = 89,002)
      • Fixed Trailing1 stop = reserved - ENTRY_TRIGGER_OFFSET (e.g., 89,000 - 40 = 88,960)
Once in trade, the bot continuously monitors each side:
  - If the market moves favorably for one side (e.g. long moves to 89,040), that side remains active while the opposite side is canceled.
  - Exit conditions (stop loss, dynamic trailing stop, fixed Trailing1 stop, and a global stop) are applied per side.
A limit order is used for exit and, if not filled, a forced market exit is attempted until the position is fully closed.
This hedged design guarantees a safe trade in both directions while ensuring that if one side becomes profitable, the other is canceled.
"""

import os
import sys
import time
import json
import datetime
import csv
import threading
from typing import Dict, Optional, Tuple

# ---------------------- Helper: EMA Calculation (Unused) ----------------------
def calculate_ema(prices: list, period: int) -> float:
    if not prices or len(prices) == 0:
        return 0.0
    ema = prices[0]
    alpha = 2 / (period + 1)
    for price in prices:
        ema = alpha * price + (1 - alpha) * ema
    return ema
# ---------------------- End of Helper ----------------------

# Extend path so BitgetFutures is importable.
sys.path.append(os.path.join(os.path.dirname(__file__), '..', '..'))
from utilities.bitget_futures import BitgetFutures

# =============================================================================
# CONFIGURATION & GLOBAL CONSTANTS
# =============================================================================
# Note: global_stop_percent is the percentage loss that will trigger an immediate exit.
params: Dict = {
    "symbols": ["BTC/USDT:USDT", "SOL/USDT:USDT", "XRP/USDT:USDT"],
    "default": {
         "entry_trigger_offset": 40.0,       # Trigger offset (used to define fixed stop for each side)
         "trailing_drop_amount": 30.0,         # Primary trailing stop drop amount in USD
         "trailing_stop_trigger": 60.0,        # If the favorable price falls by this amount, force exit
         "min_profit_for_trailing": 8.0,         # Minimum profit to activate dynamic trailing stop
         "stop_loss_offset": 2.0,              # Stop loss offset in USD
         "global_stop_percent": 0.5,           # Global stop if loss >= 0.5%
         "leverage": 2,
         "capital": 100.0,                   # Capital allocated per side (100 USD)
         "ema_short_period": 5,              # (EMA parameters remain for reference but are not used)
         "ema_long_period": 12,
    },
    "overrides": {
         "SOL/USDT:USDT": {
             "entry_trigger_offset": 400,
             "trailing_drop_amount": 0.3,
             "min_profit_for_trailing": 0.08,
             "stop_loss_offset": 0.02,
             "global_stop_percent": 0.5,
             "leverage": 2,
             "capital": 50.0,
             "trailing_stop_trigger": 60,
         },
         "XRP/USDT:USDT": {
             "entry_trigger_offset": 500,
             "trailing_drop_amount": 0.075,
             "min_profit_for_trailing": 0.02,
             "stop_loss_offset": 0.01,
             "global_stop_percent": 0.5,
             "leverage": 2,
             "capital": 50.0,
             "trailing_stop_trigger": 60,
         }
    }
}

key_path = "LiveTradingBots/secret.json"
key_name = "envelope"
with open(key_path, "r") as f:
    api_setup = json.load(f)[key_name]

bitget = BitgetFutures(api_setup)

# =============================================================================
# LOGGING & CSV TRADE LOGGING
# =============================================================================
LOG_DIR = "logs"
if not os.path.exists(LOG_DIR):
    os.makedirs(LOG_DIR)
LOG_FILE = os.path.join(LOG_DIR, "bot_log.txt")
CSV_FILE = os.path.join(LOG_DIR, "trades_log.csv")
csv_lock = threading.Lock()

def init_csv_log(file_path: str):
    is_new = not os.path.exists(file_path) or os.stat(file_path).st_size == 0
    csv_file = open(file_path, mode="a", newline="", encoding="utf-8-sig")
    csv_writer = csv.writer(csv_file, delimiter=';')
    if is_new:
        csv_writer.writerow(["Time", "Date", "Symbol", "Trade Type", "Entry Price", "Exit Price", "Profit"])
        csv_file.flush()
    return csv_file, csv_writer

csv_file, csv_writer = init_csv_log(CSV_FILE)

def log_csv(symbol: str, trade_type: str, entry_price: float, exit_price: float, profit: float):
    now = datetime.datetime.now()
    with csv_lock:
        csv_writer.writerow([now.strftime("%H:%M:%S"), now.strftime("%Y-%m-%d"), symbol, trade_type,
                              f"{entry_price:.2f}", f"{exit_price:.2f}", f"{profit:.2f}"])
        csv_file.flush()

def log_info(msg: str):
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[INFO] {timestamp} - {msg}")
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"[INFO] {timestamp} - {msg}\n")

def log_error(msg: str):
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[ERROR] {timestamp} - {msg}")
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"[ERROR] {timestamp} - {msg}\n")

# =============================================================================
# GRID SCALPING BOT CLASS (Hedged Mode: Both Long & Short Orders)
# =============================================================================

class GridScalpingBot:
    def __init__(self, symbol: str, config: Dict):
        self.symbol = symbol
        self.config = config["default"].copy()
        if symbol in config["overrides"]:
            self.config.update(config["overrides"][symbol])
        # Instead of a single position, we maintain two:
        self.long_position: Optional[Dict] = None  # Will hold keys: entry_price, stop_loss, highest_price, trailing1_stop, position_size
        self.short_position: Optional[Dict] = None  # Will hold keys: entry_price, stop_loss, lowest_price, trailing1_stop, position_size

    # --------------------- Logging ---------------------
    def log(self, message: str):
        print(f"[{self.symbol}] {datetime.datetime.now().strftime('%H:%M:%S')}: {message}")
        log_info(f"{self.symbol} - {message}")

    # --------------------- Reserve Price ---------------------
    def reserve_price_method(self):
        try:
            ticker = bitget.fetch_ticker(self.symbol)
            reserved = float(ticker['last'])
            self.log(f"Reserved price set to {reserved}")
            return reserved
        except Exception as e:
            self.log(f"Error reserving price: {e}")
            return None

    # --------------------- Cancel All Orders ---------------------
    def cancel_all_orders(self):
        try:
            orders = bitget.fetch_open_orders(self.symbol)
            for order in orders:
                try:
                    bitget.cancel_order(order['id'], self.symbol)
                    self.log(f"Cancelled order {order['id']}")
                except Exception as e:
                    self.log(f"Error cancelling order {order['id']}: {e}")
        except Exception as e:
            self.log(f"Error fetching open orders: {e}")

    # --------------------- Enter Both Positions ---------------------
    def enter_hedged_positions(self):
        reserved = self.reserve_price_method()
        if reserved is None:
            return
        # Calculate position size for each side.
        pos_size = (self.config["capital"] * self.config["leverage"]) / reserved
        pos_size = float(bitget.amount_to_precision(self.symbol, pos_size))
        # Long position details.
        self.long_position = {
            "entry_price": reserved,
            "stop_loss": reserved - self.config["stop_loss_offset"],
            "highest_price": reserved,
            "trailing1_stop": reserved + self.config["entry_trigger_offset"],
            "position_size": pos_size
        }
        # Short position details.
        self.short_position = {
            "entry_price": reserved,
            "stop_loss": reserved + self.config["stop_loss_offset"],
            "lowest_price": reserved,
            "trailing1_stop": reserved - self.config["entry_trigger_offset"],
            "position_size": pos_size
        }
        # Place market orders for both sides.
        try:
            long_order = bitget.place_market_order(self.symbol, "buy", pos_size)
            short_order = bitget.place_market_order(self.symbol, "sell", pos_size)
            self.log(f"Entered LONG position at {reserved} with size {pos_size}")
            self.log(f"Entered SHORT position at {reserved} with size {pos_size}")
        except Exception as e:
            self.log(f"Error entering hedged positions: {e}")
            # If error occurs, cancel any orders and reset positions.
            self.long_position = None
            self.short_position = None

    # --------------------- Update Positions ---------------------
    def update_positions(self):
        try:
            ticker = bitget.fetch_ticker(self.symbol)
            current_price = float(ticker['last'])
            # Update long position highest price.
            if self.long_position:
                if current_price > self.long_position["highest_price"]:
                    self.long_position["highest_price"] = current_price
            # Update short position lowest price.
            if self.short_position:
                if current_price < self.short_position["lowest_price"]:
                    self.short_position["lowest_price"] = current_price
        except Exception as e:
            self.log(f"Error updating positions: {e}")

    # --------------------- Check Exit Conditions for a Side ---------------------
    def check_exit_for_side(self, side: str) -> Optional[str]:
        # side: "long" or "short"
        try:
            ticker = bitget.fetch_ticker(self.symbol)
            current_price = float(ticker['last'])
            config = self.config
            pos = self.long_position if side == "long" else self.short_position
            if not pos:
                return None
            # Global stop check (loss percentage)
            if side == "long":
                loss_percent = ((pos["entry_price"] - current_price) / pos["entry_price"]) * 100
                if loss_percent >= config.get("global_stop_percent", 0.5):
                    self.log(f"Global stop hit for LONG: Loss {loss_percent:.2f}%")
                    return "global_stop"
            else:
                loss_percent = ((current_price - pos["entry_price"]) / pos["entry_price"]) * 100
                if loss_percent >= config.get("global_stop_percent", 0.5):
                    self.log(f"Global stop hit for SHORT: Loss {loss_percent:.2f}%")
                    return "global_stop"
            # For LONG:
            if side == "long":
                if current_price <= pos["stop_loss"]:
                    self.log(f"LONG stop loss hit: {current_price} <= {pos['stop_loss']}")
                    return "stop_loss"
                if current_price <= pos["trailing1_stop"]:
                    self.log(f"LONG Trailing1 stop hit: {current_price} <= {pos['trailing1_stop']}")
                    return "trailing1"
            else:
                if current_price >= pos["stop_loss"]:
                    self.log(f"SHORT stop loss hit: {current_price} >= {pos['stop_loss']}")
                    return "stop_loss"
                if current_price >= pos["trailing1_stop"]:
                    self.log(f"SHORT Trailing1 stop hit: {current_price} >= {pos['trailing1_stop']}")
                    return "trailing1"
            return None
        except Exception as e:
            self.log(f"Error checking exit conditions for {side}: {e}")
            return None

    # --------------------- Exit a Given Side ---------------------
    def exit_side(self, side: str, exit_reason: str):
        try:
            config = self.config
            ticker = bitget.fetch_ticker(self.symbol)
            current_price = float(ticker['last'])
            pos = self.long_position if side == "long" else self.short_position
            if not pos:
                return
            # Determine exit price.
            if exit_reason == "global_stop":
                exit_price = current_price
            elif exit_reason in ["trailing1", "stop_loss"]:
                exit_price = pos["stop_loss"] if exit_reason == "stop_loss" else pos["trailing1_stop"]
            else:
                # For dynamic trailing exit we can use highest (or lowest) price.
                exit_price = pos["highest_price"] if side == "long" else pos["lowest_price"]
            exit_side = "sell" if side == "long" else "buy"
            order = bitget.place_limit_order(self.symbol, exit_side, pos["position_size"], exit_price, reduce=True)
            self.log(f"Placed {side.upper()} exit limit order at {exit_price} due to {exit_reason} condition")
            time.sleep(3)
            # Forced exit loop.
            force_exit_start = time.time()
            while time.time() - force_exit_start < 15:
                positions = bitget.fetch_open_positions(self.symbol)
                total_contracts = sum(float(p.get('contracts', 0)) for p in positions) if positions else 0
                if total_contracts == 0:
                    break
                self.log(f"{side.upper()} limit order not filled; forcing exit via market order.")
                try:
                    result = bitget.flash_close_position(self.symbol)
                    self.log(f"Forced exit result for {side.upper()}: {result}")
                except Exception as ex:
                    self.log(f"Error during forced exit for {side.upper()}: {ex}")
                time.sleep(1)
            profit = (exit_price - pos["entry_price"]) if side == "long" else (pos["entry_price"] - exit_price)
            log_csv(self.symbol, side, pos["entry_price"], exit_price, profit)
            self.log(f"{side.upper()} position exited with profit: {profit}")
            # Clear the position.
            if side == "long":
                self.long_position = None
            else:
                self.short_position = None
        except Exception as e:
            self.log(f"Error exiting {side.upper()} position: {e}")

    # --------------------- Hedge Management ---------------------
    def manage_hedge(self):
        """
        Update positions and, if one side's trigger condition is met,
        exit that side and cancel the opposite side.
        """
        self.update_positions()
        long_exit = self.check_exit_for_side("long") if self.long_position else None
        short_exit = self.check_exit_for_side("short") if self.short_position else None

        # If one side hits exit, cancel the opposite side.
        if long_exit and self.long_position:
            self.exit_side("long", long_exit)
            if self.short_position:
                self.log("Cancelling SHORT position due to LONG exit.")
                self.cancel_all_orders()
                self.short_position = None
        if short_exit and self.short_position:
            self.exit_side("short", short_exit)
            if self.long_position:
                self.log("Cancelling LONG position due to SHORT exit.")
                self.cancel_all_orders()
                self.long_position = None

    # --------------------- Trigger Check (Hedged Entry) ---------------------
    def check_for_trigger(self):
        # In hedged mode, if no positions are open, enter both orders.
        if self.long_position is None and self.short_position is None:
            try:
                ticker = bitget.fetch_ticker(self.symbol)
                current_price = float(ticker['last'])
                reserved = self.reserve_price_method()
                if reserved is None:
                    return
                # Once reserved is set, immediately place both orders.
                self.log("Placing hedged orders for both LONG and SHORT.")
                self.enter_hedged_positions()
            except Exception as e:
                self.log(f"Error checking for trigger in hedged mode: {e}")

    # --------------------- Main Cycle ---------------------
    def run_cycle(self):
        if self.long_position is None and self.short_position is None:
            self.check_for_trigger()
        else:
            self.manage_hedge()
            # If both positions are closed, update reserved price.
            if self.long_position is None and self.short_position is None:
                self.reserve_price_method()

# =============================================================================
# MAIN LOOP
# =============================================================================

def main():
    bots: Dict[str, GridScalpingBot] = {}
    for symbol in params["symbols"]:
        bot = GridScalpingBot(symbol, params)
        bot.reserve_price_method()
        bots[symbol] = bot
        bot.log("Bot initialisiert und bereit (hedged mode).")
    while True:
        for symbol, bot in bots.items():
            try:
                bot.run_cycle()
            except Exception as e:
                bot.log(f"Error in run_cycle: {e}")
        time.sleep(1)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log_info("Bot per KeyboardInterrupt gestoppt.")
    finally:
        csv_file.close()


