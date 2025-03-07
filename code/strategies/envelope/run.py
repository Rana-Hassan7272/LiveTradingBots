import os
import sys
import time
import json
import datetime
import csv
import threading
from typing import Dict, Optional

# ---------------------- Helper: EMA Calculation ----------------------
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
# Default BTC settings are in USD.
# Overrides for SOL and XRP are scaled relative to BTC.
params: Dict = {
    "symbols": ["BTC/USDT:USDT", "SOL/USDT:USDT", "XRP/USDT:USDT"],
    "default": {
         "entry_trigger_offset": 40.0,       # BTC trigger threshold in USD
         "trailing_drop_amount": 30.0,         # BTC trailing drop amount in USD
         "trailing_stop_trigger": 60.0,        # BTC trailing stop trigger in USD
         "min_profit_for_trailing": 8.0,         # BTC minimum profit for trailing stop in USD
         "stop_loss_offset": 2.0,              # BTC fixed stop loss offset in USD
         "leverage": 2,
         "capital": 50.0,                    # Capital in USD for all coins
         "ema_short_period": 5,
         "ema_long_period": 12,
         "global_stop_roi": -0.1             # Global stop loss threshold (%)
    },
    "overrides": {
         "SOL/USDT:USDT": {
             "entry_trigger_offset": 0.07,       # ~40 * (144.18/85000)
             "trailing_drop_amount": 0.05,         # ~30 * (144.18/85000)
             "min_profit_for_trailing": 0.014,       # ~8 * (144.18/85000)
             "stop_loss_offset": 0.0034,           # ~2 * (144.18/85000)
             "trailing_stop_trigger": 0.10,        # ~60 * (144.18/85000)
             "leverage": 2,
             "capital": 50.0
         },
         "XRP/USDT:USDT": {
             "entry_trigger_offset": 0.0012,      # ~40 * (2.50/85000)
             "trailing_drop_amount": 0.0009,        # ~30 * (2.50/85000)
             "min_profit_for_trailing": 0.00024,    # ~8 * (2.50/85000)
             "stop_loss_offset": 0.00006,           # ~2 * (2.50/85000)
             "trailing_stop_trigger": 0.0018,       # ~60 * (2.50/85000)
             "leverage": 2,
             "capital": 50.0
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
# GRID SCALPING BOT CLASS (Hybrid Exit Approach with Trigger Logic)
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
            # Execute at the reserved price.
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
                # Initial trailing stop set based on the trigger offset.
                self.trailing1_stop = self.reserved_price + self.config["entry_trigger_offset"]
                self.log(f"Stop loss set at {self.stop_loss}")
                self.log(f"Initial trailing stop set at {self.trailing1_stop}")
            else:
                self.stop_loss = self.entry_price + self.config["stop_loss_offset"]
                self.lowest_price = self.entry_price
                self.primary_trailing_stop = None
                self.trailing1_stop = self.reserved_price - self.config["entry_trigger_offset"]
                self.log(f"Stop loss set at {self.stop_loss}")
                self.log(f"Initial trailing stop set at {self.trailing1_stop}")
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
                roi = (current_price - self.entry_price) / self.entry_price * 100
                if roi <= self.config.get("global_stop_roi", -0.1):
                    self.log(f"Global stop triggered: ROI {roi:.2f}% <= {self.config.get('global_stop_roi', -0.1)}%")
                    return "global_stop"
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
                roi = (self.entry_price - current_price) / self.entry_price * 100
                if roi <= self.config.get("global_stop_roi", -0.1):
                    self.log(f"Global stop triggered: ROI {roi:.2f}% <= {self.config.get('global_stop_roi', -0.1)}%")
                    return "global_stop"
            return None
        except Exception as e:
            self.log(f"Error checking exit conditions: {e}")
            return None

    # --------------------- Position Exit (Hybrid Approach) ---------------------
    def exit_position(self, exit_reason: str):
        if not self.in_position:
            return

        exit_side = "sell" if self.position_direction == "long" else "buy"
        try:
            ticker = bitget.fetch_ticker(self.symbol)
            current_market_price = float(ticker['last'])
        except Exception as e:
            self.log(f"Error fetching market price during exit: {e}")
            current_market_price = self.reserved_price

        # Determine exit price based on exit_reason.
        if self.position_direction == "long":
            if exit_reason == "primary_trailing":
                exit_price = self.highest_price
            elif exit_reason in ("trailing1", "global_stop"):
                exit_price = current_market_price
            else:
                exit_price = self.stop_loss
        else:
            if self.position_direction == "short":
                if exit_reason == "primary_trailing":
                    exit_price = self.lowest_price
                elif exit_reason in ("trailing1", "global_stop"):
                    exit_price = current_market_price
                else:
                    exit_price = self.stop_loss
            else:
                exit_price = self.stop_loss

        try:
            order = bitget.place_limit_order(self.symbol, exit_side, self.position_size, exit_price, reduce=True)
            self.log(f"Limit exit order placed at {exit_price} due to {exit_reason} condition")
            time.sleep(3)
            positions = bitget.fetch_open_positions(self.symbol)
            total_contracts = sum(float(pos.get('contracts', 0)) for pos in positions) if positions else 0
            if total_contracts > 0:
                self.log("Limit order not filled; canceling pending orders and falling back to market exit")
                self.cancel_all_orders()
                fallback_order = bitget.place_market_order(self.symbol, exit_side, self.position_size, reduce=True)
                self.log(f"Fallback market exit order placed: {fallback_order}")
            else:
                self.log("Limit order filled successfully; position closed")
        except Exception as e:
            self.log(f"Error during limit exit order: {e}. Falling back to market exit")
            try:
                fallback_order = bitget.place_market_order(self.symbol, exit_side, self.position_size, reduce=True)
                self.log(f"Fallback market exit order placed: {fallback_order}")
            except Exception as e2:
                self.log(f"Market exit fallback also failed: {e2}")
        finally:
            try:
                current_price = float(bitget.fetch_ticker(self.symbol)['last'])
                profit = (current_price - self.entry_price) if self.position_direction == "long" else (self.entry_price - current_price)
                log_csv(self.symbol, self.position_direction, self.entry_price, current_price, profit)
            except Exception as e:
                self.log(f"Error during profit logging: {e}")
            self.cancel_all_orders()
            self.in_position = False
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
        bot.log("Bot initialized and ready.")
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
        log_info("Bot stopped via KeyboardInterrupt.")
    finally:
        csv_file.close()


