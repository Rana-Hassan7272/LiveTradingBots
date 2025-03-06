#!/usr/bin/env python3
"""
Final Bot Code for Grid Scalping with Sell Trigger & EMA Trend Confirmation

This bot reserves the current market price and triggers an entry only if:
  - For a long trade, the market rises to (reserved + ENTRY_TRIGGER_OFFSET_USD)
    and the short-term EMA (calculated from recent data) is above the long-term EMA.
  - For a short trade, the market falls to (reserved - ENTRY_TRIGGER_OFFSET_USD)
    and the short-term EMA is below the long-term EMA.
  
On entry, the bot:
  - Executes the trade at the reserved price.
  - Sets a fixed stop loss at (entry - STOP_LOSS_OFFSET_USD) for longs.
  - Sets a fixed Trailing1 stop at (entry + ENTRY_TRIGGER_OFFSET_USD – TRAILING1_DROP_AMOUNT_USD).
  - Monitors the highest (or lowest) price and, once profit exceeds MIN_PROFIT_FOR_TRAILING_USD,
    activates a dynamic primary trailing stop at (highest – TRAILING_DROP_AMOUNT_USD) for longs.
    
Exit is triggered if the current price falls to or below:
  - The stop loss,
  - The dynamic primary trailing stop (if active and if the highest price remains in the profit zone), or
  - The fixed Trailing1 stop.
  
A limit order is used for exit to reduce slippage.
  
Additional trend confirmation is added via EMA:
  - EMA_SHORT_PERIOD and EMA_LONG_PERIOD are computed from recent 1m candles.
  - A long trade is triggered only if short EMA > long EMA; for short trades, the reverse applies.
  
This code is designed for continuous operation (e.g. on an AWS server).
"""

import os
import sys
import time
import json
import datetime
import csv
import threading
from typing import Dict, Optional,Tuple

# ---------------------- Helper: EMA Calculation ----------------------
def calculate_ema(prices: list, period: int) -> float:
    """
    Calculate the Exponential Moving Average (EMA) for a list of prices.
    Simple iterative calculation: EMA = alpha * price + (1 - alpha) * previous EMA.
    """
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
# Parameters (for BTC; adjust for other coins via overrides)
params: Dict = {
    "symbols": ["BTC/USDT:USDT", "SOL/USDT:USDT", "XRP/USDT:USDT"],
    "default": {
         "entry_trigger_offset": 40.0,       # Trade trigger threshold (e.g. 40 USD)
         "trailing_drop_amount": 30.0,         # Primary trailing stop drop amount (e.g. 30 USD)
         "trailing1_drop_amount": 5.0,         # Fixed Trailing1 stop drop amount (e.g. 5 USD)
         "min_profit_for_trailing": 8.0,         # Minimum profit required to activate trailing stop (e.g. 8 USD)
         "stop_loss_offset": 2.0,              # Fixed stop loss offset (e.g. 2 USD below entry for long)
         "leverage": 2,
         "capital": 80.0,
         "ema_short_period": 5,              # Short-term EMA period for trend confirmation.
         "ema_long_period": 12,              # Long-term EMA period.
    },
    "overrides": {
         "SOL/USDT:USDT": {
             "entry_trigger_offset": 400,       # Adjusted for SOL scale.
             "trailing_drop_amount": 0.3,
             "trailing1_drop_amount": 0.05,
             "min_profit_for_trailing": 0.08,
             "stop_loss_offset": 0.02,
             "leverage": 2,
             "capital": 50.0,
             "ema_short_period": 5,
             "ema_long_period": 12,
         },
         "XRP/USDT:USDT": {
             "entry_trigger_offset": 500,
             "trailing_drop_amount": 0.075,
             "trailing1_drop_amount": 0.025,
             "min_profit_for_trailing": 0.02,
             "stop_loss_offset": 0.01,
             "leverage": 2,
             "capital": 50.0,
             "ema_short_period": 5,
             "ema_long_period": 12,
         }
    }
}

# API key file and key name – ensure your secret.json is configured.
key_path = "LiveTradingBots/secret.json"
key_name = "envelope"
with open(key_path, "r") as f:
    api_setup = json.load(f)[key_name]

# Initialize BitgetFutures instance.
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
# GRID SCALPING BOT CLASS WITH SELL TRIGGER & EMA TREND CONFIRMATION
# =============================================================================

class GridScalpingBot:
    def __init__(self, symbol: str, config: Dict):
        self.symbol = symbol
        self.config = config["default"].copy()
        if symbol in config["overrides"]:
            self.config.update(config["overrides"][symbol])
        # Initialize state variables.
        self.reserved_price: Optional[float] = None   # Reserved entry price.
        self.in_position: bool = False                # Whether a trade is open.
        self.entry_price: Optional[float] = None        # Execution price at entry.
        self.stop_loss: Optional[float] = None          # Fixed stop loss level.
        self.highest_price: Optional[float] = None      # Highest price reached (for long).
        self.primary_trailing_stop: Optional[float] = None  # Dynamic trailing stop.
        self.trailing1_stop: Optional[float] = None     # Fixed Trailing1 stop.
        self.lowest_price: Optional[float] = None       # Lowest price (for short).
        self.position_direction: Optional[str] = None   # "long" or "short".
        self.position_size: Optional[float] = None        # Calculated order size.
        self.last_order_ids = []                         # (Not used in this implementation).

    # --------------------- Logging ---------------------
    def log(self, message: str):
        print(f"[{self.symbol}] {datetime.datetime.now().strftime('%H:%M:%S')}: {message}")
        log_info(f"{self.symbol} - {message}")

    # --------------------- Reserve Price ---------------------
    def reserve_price_method(self):
        """Set the reserved price using the current market 'last' price."""
        try:
            ticker = bitget.fetch_ticker(self.symbol)
            self.reserved_price = float(ticker['last'])
            self.log(f"Reserved price set to {self.reserved_price}")
        except Exception as e:
            self.log(f"Error reserving price: {e}")

    # --------------------- Cancel All Orders ---------------------
    def cancel_all_orders(self):
        """Cancel all open orders for the symbol."""
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

    # --------------------- EMA Trend Calculation ---------------------
    def get_ema_trend(self) -> Tuple[Optional[float], Optional[float]]:
        """
        Fetch recent OHLCV data and calculate the short-term and long-term EMAs.
        Returns (short_ema, long_ema). If data is insufficient, returns (None, None).
        """
        try:
            # Use 1m timeframe; adjust limit as needed.
            ohlcv = bitget.fetch_recent_ohlcv(self.symbol, "1m", limit=50)
            close_prices = ohlcv["close"].tolist()
            if not close_prices:
                return None, None
            short_period = self.config.get("ema_short_period", 5)
            long_period = self.config.get("ema_long_period", 12)
            short_ema = calculate_ema(close_prices, short_period)
            long_ema = calculate_ema(close_prices, long_period)
            self.log(f"EMA calculated: short={short_ema:.2f}, long={long_ema:.2f}")
            return short_ema, long_ema
        except Exception as e:
            self.log(f"Error calculating EMAs: {e}")
            return None, None

    # --------------------- Position Entry ---------------------
    def enter_position(self, direction: str):
        """
        Enter a trade when triggered.
        For long trades:
          - Execute at the reserved price.
          - Set stop loss at (reserved - STOP_LOSS_OFFSET).
          - Set fixed Trailing1 stop at (reserved + ENTRY_TRIGGER_OFFSET - TRAILING1_DROP_AMOUNT).
          - Initialize highest_price = entry_price.
        For short trades, analogous logic applies.
        """
        try:
            order_side = "buy" if direction == "long" else "sell"
            self.entry_price = self.reserved_price  # Execute at reserved price.
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
                self.trailing1_stop = self.reserved_price + self.config["entry_trigger_offset"] - self.config["trailing1_drop_amount"]
                self.log(f"Stop loss set at {self.stop_loss}")
                self.log(f"Trailing1 fixed stop set at {self.trailing1_stop}")
            else:
                self.stop_loss = self.entry_price + self.config["stop_loss_offset"]
                self.lowest_price = self.entry_price
                self.primary_trailing_stop = None
                self.trailing1_stop = self.reserved_price - self.config["entry_trigger_offset"] + self.config["trailing1_drop_amount"]
                self.log(f"Stop loss set at {self.stop_loss}")
                self.log(f"Trailing1 fixed stop set at {self.trailing1_stop}")
        except Exception as e:
            self.log(f"Error entering position: {e}")

    # --------------------- Primary Trailing Stop Update ---------------------
    def update_primary_trailing_stop(self):
        """
        Update the dynamic trailing stop.
        For long trades:
          - Update highest_price if current price exceeds it.
          - Once highest_price >= (entry + ENTRY_TRIGGER_OFFSET) and profit >= MIN_PROFIT_FOR_TRAILING,
            set primary trailing stop = highest_price - TRAILING_DROP_AMOUNT.
        For short trades, apply reversed logic.
        """
        try:
            ticker = bitget.fetch_ticker(self.symbol)
            current_price = float(ticker['last'])
            direction = self.position_direction
            if direction == "long":
                if current_price > self.highest_price:
                    self.highest_price = current_price
                    if self.highest_price >= self.entry_price + self.config["entry_trigger_offset"]:
                        if (self.highest_price - self.entry_price) >= self.config["min_profit_for_trailing"]:
                            self.primary_trailing_stop = self.highest_price - self.config["trailing_drop_amount"]
                            self.log(f"Primary trailing stop updated to {self.primary_trailing_stop} (highest: {self.highest_price})")
            else:
                if current_price < self.lowest_price:
                    self.lowest_price = current_price
                    if self.lowest_price <= self.entry_price - self.config["entry_trigger_offset"]:
                        if (self.entry_price - self.lowest_price) >= self.config["min_profit_for_trailing"]:
                            self.primary_trailing_stop = self.lowest_price + self.config["trailing_drop_amount"]
                            self.log(f"Primary trailing stop updated to {self.primary_trailing_stop} (lowest: {self.lowest_price})")
        except Exception as e:
            self.log(f"Error updating primary trailing stop: {e}")

    # --------------------- Exit Conditions Check ---------------------
    def check_exit_conditions(self) -> Optional[str]:
        """
        Check exit conditions and return which condition triggers the exit:
          For long trades:
            - "stop_loss": if current price <= stop_loss.
            - "primary_trailing": if primary trailing stop is active and current price <= it.
            - "trailing1": if current price <= trailing1_stop.
          For short trades, reversed logic applies.
        Returns a string indicating the condition or None if no exit condition is met.
        """
        try:
            ticker = bitget.fetch_ticker(self.symbol)
            current_price = float(ticker['last'])
            direction = self.position_direction

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
        """
        Exit the current trade using a limit order at the designated exit price.
        For long trades:
          - If exit_reason is "primary_trailing", exit at highest_price.
          - If "trailing1", exit at trailing1_stop.
          - Otherwise, exit at stop_loss.
        For short trades, analogous logic applies.
        """
        if not self.in_position:
            return
        try:
            exit_side = "sell" if self.position_direction == "long" else "buy"
            if self.position_direction == "long":
                if exit_reason == "primary_trailing":
                    exit_price = self.highest_price
                elif exit_reason == "trailing1":
                    exit_price = self.trailing1_stop
                else:
                    exit_price = self.stop_loss
            else:
                if exit_reason == "primary_trailing":
                    exit_price = self.lowest_price
                elif exit_reason == "trailing1":
                    exit_price = self.trailing1_stop
                else:
                    exit_price = self.stop_loss
            order = bitget.place_limit_order(self.symbol, exit_side, self.position_size, exit_price, reduce=True)
            self.log(f"Placed exit limit order at {exit_price} due to {exit_reason} condition")
            self.log("Exited position via limit order")
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

    # --------------------- Trigger Check with EMA Confirmation ---------------------
    def check_for_trigger(self):
        """
        If no trade is open, check if the market price has moved enough from the reserved price
        to trigger an entry AND if the EMA trend confirms the direction.
          For long: trigger if current price >= (reserved + ENTRY_TRIGGER_OFFSET) AND short EMA > long EMA.
          For short: trigger if current price <= (reserved - ENTRY_TRIGGER_OFFSET) AND short EMA < long EMA.
        """
        if self.in_position or self.reserved_price is None:
            return
        try:
            ticker = bitget.fetch_ticker(self.symbol)
            current_price = float(ticker['last'])
            trigger_offset = self.config["entry_trigger_offset"]
            short_ema, long_ema = self.get_ema_trend()
            if short_ema is None or long_ema is None:
                self.log("EMA trend data insufficient; trigger check übersprungen.")
                return
            if current_price >= self.reserved_price + trigger_offset:
                # For long trades, require short EMA > long EMA.
                if short_ema > long_ema:
                    self.log(f"Long trigger reached: {current_price} >= {self.reserved_price} + {trigger_offset} and EMA confirmed (short: {short_ema:.2f} > long: {long_ema:.2f})")
                    self.cancel_all_orders()
                    self.enter_position("long")
                else:
                    self.log(f"Long trigger reached but EMA condition not met (short: {short_ema:.2f} <= long: {long_ema:.2f}).")
            elif current_price <= self.reserved_price - trigger_offset:
                # For short trades, require short EMA < long EMA.
                if short_ema < long_ema:
                    self.log(f"Short trigger reached: {current_price} <= {self.reserved_price} - {trigger_offset} and EMA confirmed (short: {short_ema:.2f} < long: {long_ema:.2f})")
                    self.cancel_all_orders()
                    self.enter_position("short")
                else:
                    self.log(f"Short trigger reached but EMA condition not met (short: {short_ema:.2f} >= long: {long_ema:.2f}).")
        except Exception as e:
            self.log(f"Error checking for trigger: {e}")

    # --------------------- Main Cycle ---------------------
    def run_cycle(self):
        """
        Run one cycle:
          - If no trade is open, check for trigger (with EMA confirmation).
          - If a trade is open, update the primary trailing stop and check exit conditions.
          - If an exit condition is met, exit the trade and reset the reserved price.
        """
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
        time.sleep(5)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log_info("Bot per KeyboardInterrupt gestoppt.")
    finally:
        csv_file.close()
