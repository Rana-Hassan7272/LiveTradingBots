#!/usr/bin/env python3
"""
Final Bot Code for Grid Scalping with Sell Trigger

This bot reserves the current market price and triggers an entry only if:
  - For a long trade, the market rises to (reserved + ENTRY_TRIGGER_OFFSET_USD).
  - For a short trade, the market falls to (reserved - ENTRY_TRIGGER_OFFSET_USD).

On entry, the bot:
  - Executes the trade at the reserved price.
  - Sets a fixed stop loss at (entry - STOP_LOSS_OFFSET_USD) for longs.
  - Sets a fixed Trailing1 stop at (entry + ENTRY_TRIGGER_OFFSET_USD) for longs 
    (and for shorts, (entry - ENTRY_TRIGGER_OFFSET_USD)), so that the profit locked equals the trigger offset.
  - Monitors the highest (or lowest) price and, once profit exceeds MIN_PROFIT_FOR_TRAILING_USD,
    activates a dynamic primary trailing stop at (highest – TRAILING_DROP_AMOUNT_USD) for longs.
  - Additionally, if the highest (or lowest) price falls by a configurable amount (TRAILING_STOP_TRIGGER_USD),
    the bot will trigger an exit using the highest (or lowest) price.
    
Exit is triggered if the current price falls to or below:
  - The stop loss,
  - The dynamic primary trailing stop (if active),
  - The fixed Trailing1 stop, or
  - The global stop – if the trade loses more than the configured percentage.

A limit order is used for exit to reduce slippage. If the limit order is not filled within 1 second,
the bot repeatedly forces an exit via market order (using place_market_order() as a forced exit)
for up to 5 seconds until no open contracts remain.

This code is designed for continuous operation.
"""

import os
import sys
import time
import json
import datetime
import csv
import threading
from typing import Dict, Optional, Tuple

# ---------------------- Helper: EMA Calculation ----------------------
def calculate_ema(prices: list, period: int) -> float:
    # (EMA calculation is present for reference; not used in this version.)
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
# For testing, ensure that only BTC is used if desired.
params: Dict = {
    "symbols": ["BTC/USDT:USDT"],
    "default": {
         "entry_trigger_offset": 40.0,       # Trigger threshold in USD
         "trailing_drop_amount": 30.0,         # Dynamic trailing stop drop amount in USD
         "trailing_stop_trigger": 60.0,        # If highest price falls by this amount, trigger exit
         "min_profit_for_trailing": 8.0,         # Minimum profit to activate trailing stop
         "stop_loss_offset": 2.0,              # Stop loss offset (e.g., 2 USD below entry for long)
         "global_stop_percent": 0.1,           # Global stop if loss reaches 0.1%
         "leverage": 2,
         "capital": 100.0,                   # Capital is 100 USD
         "ema_short_period": 5,              # (EMA parameters exist but are not used here)
         "ema_long_period": 12,
    },
    "overrides": {
         "SOL/USDT:USDT": {
             "entry_trigger_offset": 400,
             "trailing_drop_amount": 0.3,
             "min_profit_for_trailing": 0.08,
             "stop_loss_offset": 0.02,
             "global_stop_percent": 0.1,
             "leverage": 2,
             "capital": 50.0,
             "trailing_stop_trigger": 60,
         },
         "XRP/USDT:USDT": {
             "entry_trigger_offset": 500,
             "trailing_drop_amount": 0.075,
             "min_profit_for_trailing": 0.02,
             "stop_loss_offset": 0.01,
             "global_stop_percent": 0.1,
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
# GRID SCALPING BOT CLASS (Without EMA Trigger; Immediate Entry Based on Price)
# =============================================================================

class GridScalpingBot:
    def __init__(self, symbol: str, config: Dict):
        self.symbol = symbol
        self.config = config["default"].copy()
        if symbol in config["overrides"]:
            self.config.update(config["overrides"][symbol])
        self.reserved_price: Optional[float] = None
        self.in_position: bool = False
        self.entry_price: Optional[float] = None
        self.stop_loss: Optional[float] = None
        self.highest_price: Optional[float] = None
        self.primary_trailing_stop: Optional[float] = None
        self.trailing1_stop: Optional[float] = None
        self.lowest_price: Optional[float] = None
        self.position_direction: Optional[str] = None
        self.position_size: Optional[float] = None
        self.last_order_ids = []

    # --------------------- Logging ---------------------
    def log(self, message: str):
        print(f"[{self.symbol}] {datetime.datetime.now().strftime('%H:%M:%S')}: {message}")
        log_info(f"{self.symbol} - {message}")

    # --------------------- Balance Check ---------------------
    def get_available_balance(self) -> float:
        try:
            bal = bitget.fetch_balance()
            return float(bal['USDT']['free'])
        except Exception as e:
            self.log(f"Error fetching balance: {e}")
            return 0.0

    # --------------------- Reserve Price ---------------------
    def reserve_price_method(self):
        try:
            ticker = bitget.fetch_ticker(self.symbol)
            self.reserved_price = float(ticker['last'])
            self.log(f"Reserved price set to {self.reserved_price}")
        except Exception as e:
            self.log(f"Error reserving price: {e}")

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
        self.last_order_ids = []

    # --------------------- Position Entry ---------------------
    def enter_position(self, direction: str):
        if self.in_position:
            self.log("Already in a position, skipping entry.")
            return
        available = self.get_available_balance()
        if available < self.config["capital"]:
            self.log(f"Insufficient balance: Available {available} < Required {self.config['capital']}. Waiting for current position to close.")
            return
        try:
            order_side = "buy" if direction == "long" else "sell"
            # For this version, entry is at the reserved price.
            self.entry_price = self.reserved_price
            position_size = (self.config["capital"] * self.config["leverage"]) / self.entry_price
            position_size = float(bitget.amount_to_precision(self.symbol, position_size))
            self.position_size = position_size
            order = bitget.place_market_order(self.symbol, order_side, position_size)
            self.log(f"Entered {direction.upper()} position at {self.entry_price} with size {position_size}")
            self.in_position = True
            self.position_direction = direction
            if direction == "long":
                self.stop_loss = self.entry_price - self.config["stop_loss_offset"]
                self.highest_price = self.entry_price
                self.primary_trailing_stop = None
                self.trailing1_stop = self.reserved_price + self.config["entry_trigger_offset"]
                self.log(f"Stop loss set at {self.stop_loss}")
                self.log(f"Trailing1 fixed stop set at {self.trailing1_stop}")
            else:
                self.stop_loss = self.entry_price + self.config["stop_loss_offset"]
                self.lowest_price = self.entry_price
                self.primary_trailing_stop = None
                self.trailing1_stop = self.reserved_price - self.config["entry_trigger_offset"]
                self.log(f"Stop loss set at {self.stop_loss}")
                self.log(f"Trailing1 fixed stop set at {self.trailing1_stop}")
        except Exception as e:
            self.log(f"Error entering position: {e}")

    # --------------------- Primary Trailing Stop Update ---------------------
    def update_primary_trailing_stop(self):
        try:
            ticker = bitget.fetch_ticker(self.symbol)
            current_price = float(ticker['last'])
            direction = self.position_direction
            if direction == "long":
                if current_price > self.highest_price:
                    self.highest_price = current_price
                    if self.highest_price >= self.entry_price + self.config["entry_trigger_offset"] and \
                       (self.highest_price - self.entry_price) >= self.config["min_profit_for_trailing"]:
                        self.primary_trailing_stop = self.highest_price - self.config["trailing_drop_amount"]
                        self.log(f"Primary trailing stop updated to {self.primary_trailing_stop} (highest: {self.highest_price})")
                trailing_trigger = self.config.get("trailing_stop_trigger", 60.0)
                if self.highest_price is not None and (self.highest_price - current_price) >= trailing_trigger:
                    self.log(f"Primary trailing trigger hit: {self.highest_price} - {current_price} >= {trailing_trigger}")
                    self.primary_trailing_stop = self.highest_price
            else:
                if current_price < self.lowest_price:
                    self.lowest_price = current_price
                    if self.lowest_price <= self.entry_price - self.config["entry_trigger_offset"] and \
                       (self.entry_price - self.lowest_price) >= self.config["min_profit_for_trailing"]:
                        self.primary_trailing_stop = self.lowest_price + self.config["trailing_drop_amount"]
                        self.log(f"Primary trailing stop updated to {self.primary_trailing_stop} (lowest: {self.lowest_price})")
                trailing_trigger = self.config.get("trailing_stop_trigger", 60.0)
                if self.lowest_price is not None and (current_price - self.lowest_price) >= trailing_trigger:
                    self.log(f"Primary trailing trigger hit: {current_price} - {self.lowest_price} >= {trailing_trigger}")
                    self.primary_trailing_stop = self.lowest_price
        except Exception as e:
            self.log(f"Error updating primary trailing stop: {e}")

    # --------------------- Exit Conditions Check ---------------------
    def check_exit_conditions(self) -> Optional[str]:
        try:
            ticker = bitget.fetch_ticker(self.symbol)
            current_price = float(ticker['last'])
            direction = self.position_direction

            # Global stop check:
            if direction == "long":
                loss_percent = ((self.entry_price - current_price) / self.entry_price) * 100
                if loss_percent >= self.config.get("global_stop_percent", 0.1):
                    self.log(f"Global stop hit: Loss {loss_percent:.2f}% >= {self.config.get('global_stop_percent', 0.1)}%")
                    return "global_stop"
            else:
                loss_percent = ((current_price - self.entry_price) / self.entry_price) * 100
                if loss_percent >= self.config.get("global_stop_percent", 0.1):
                    self.log(f"Global stop hit: Loss {loss_percent:.2f}% >= {self.config.get('global_stop_percent', 0.1)}%")
                    return "global_stop"

            if direction == "long":
                if current_price <= self.stop_loss:
                    self.log(f"Stop loss hit: {current_price} <= {self.stop_loss}")
                    return "stop_loss"
                if self.primary_trailing_stop is not None and current_price <= self.primary_trailing_stop:
                    if self.highest_price >= self.entry_price + self.config["entry_trigger_offset"]:
                        self.log(f"Primary trailing stop hit: {current_price} <= {self.primary_trailing_stop}")
                        return "primary_trailing"
                if current_price <= self.trailing1_stop:
                    self.log(f"Trailing1 stop hit: {current_price} <= {self.trailing1_stop}")
                    return "trailing1"
            else:
                if current_price >= self.stop_loss:
                    self.log(f"Stop loss hit: {current_price} >= {self.stop_loss}")
                    return "stop_loss"
                if self.primary_trailing_stop is not None and current_price >= self.primary_trailing_stop:
                    if self.lowest_price <= self.entry_price - self.config["entry_trigger_offset"]:
                        self.log(f"Primary trailing stop hit: {current_price} >= {self.primary_trailing_stop}")
                        return "primary_trailing"
                if current_price >= self.trailing1_stop:
                    self.log(f"Trailing1 stop hit: {current_price} >= {self.trailing1_stop}")
                    return "trailing1"
            return None
        except Exception as e:
            self.log(f"Error checking exit conditions: {e}")
            return None

    # --------------------- Position Exit ---------------------
    def exit_position(self, exit_reason: str):
        if not self.in_position:
            return
        try:
            exit_side = "sell" if self.position_direction == "long" else "buy"
            if self.position_direction == "long":
                if exit_reason in ["primary_trailing", "trailing1"]:
                    exit_price = self.highest_price if exit_reason == "primary_trailing" else self.trailing1_stop
                elif exit_reason == "global_stop":
                    ticker = bitget.fetch_ticker(self.symbol)
                    exit_price = float(ticker['last'])
                else:
                    exit_price = self.stop_loss
            else:
                if self.position_direction == "short":
                    if exit_reason in ["primary_trailing", "trailing1"]:
                        exit_price = self.lowest_price if exit_reason == "primary_trailing" else self.trailing1_stop
                    elif exit_reason == "global_stop":
                        ticker = bitget.fetch_ticker(self.symbol)
                        exit_price = float(ticker['last'])
                    else:
                        exit_price = self.stop_loss
                else:
                    exit_price = self.stop_loss

            order = bitget.place_limit_order(self.symbol, exit_side, self.position_size, exit_price, reduce=True)
            self.log(f"Placed exit limit order at {exit_price} due to {exit_reason} condition")
            # Wait 1 second for limit order to fill.
            time.sleep(1)
            # Forced exit loop: check for open positions for up to 5 seconds.
            force_exit_start = time.time()
            while time.time() - force_exit_start < 5:
                positions = bitget.fetch_open_positions(self.symbol)
                total_contracts = sum(float(pos.get('contracts', 0)) for pos in positions) if positions else 0
                if total_contracts == 0:
                    break
                self.log("Limit order not filled; forcing exit via market order.")
                try:
                    result = bitget.place_market_order(self.symbol, exit_side, self.position_size, reduce=True)
                    self.log(f"Forced exit result: {result}")
                except Exception as ex:
                    self.log(f"Error during forced exit: {ex}")
                time.sleep(1)
            profit = (exit_price - self.entry_price) if self.position_direction == "long" else (self.entry_price - exit_price)
            log_csv(self.symbol, self.position_direction, self.entry_price, exit_price, profit)
            self.cancel_all_orders()
        except Exception as e:
            self.log(f"Error exiting position: {e}")
        finally:
            self.in_position = False
            self.position = None
            self.entry_price = None
            self.stop_loss = None
            self.primary_trailing_stop = None
            self.trailing1_stop = None
            self.highest_price = None
            self.lowest_price = None
            self.position_direction = None
            self.position_size = None
            self.last_order_ids = []

    # --------------------- Trigger Check ---------------------
    def check_for_trigger(self):
        if self.in_position or self.reserved_price is None:
            return
        try:
            ticker = bitget.fetch_ticker(self.symbol)
            current_price = float(ticker['last'])
            trigger_offset = self.config["entry_trigger_offset"]
            if current_price >= self.reserved_price + trigger_offset:
                self.log(f"Long trigger reached: {current_price} >= {self.reserved_price} + {trigger_offset}")
                self.cancel_all_orders()
                self.enter_position("long")
            elif current_price <= self.reserved_price - trigger_offset:
                self.log(f"Short trigger reached: {current_price} <= {self.reserved_price} - {trigger_offset}")
                self.cancel_all_orders()
                self.enter_position("short")
        except Exception as e:
            self.log(f"Error checking for trigger: {e}")

    # --------------------- Main Cycle ---------------------
    def run_cycle(self):
        if not self.in_position:
            self.check_for_trigger()
        else:
            self.update_primary_trailing_stop()
            exit_reason = self.check_exit_conditions()
            if exit_reason is not None:
                self.exit_position(exit_reason)
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
        bot.log("Bot initialisiert und bereit.")
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



