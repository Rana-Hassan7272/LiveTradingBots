import os
import sys
import time
import json
import datetime
from typing import Dict

# Ensure the BitgetFutures module is importable
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
from utilities.bitget_futures import BitgetFutures

# =============================================================================
# CONFIGURATION
# =============================================================================
# New parameters (for BTC):
#   ENTRY_TRIGGER_OFFSET_USD = 40.0
#   TRAILING_DROP_AMOUNT_USD = 30.0
#   TRAILING1_DROP_AMOUNT_USD = 5.0
#   MIN_PROFIT_FOR_TRAILING_USD = 8.0
#   STOP_LOSS_OFFSET_USD = 2.0
#
# For SOL and XRP, use proportional values.
params: Dict = {
    "symbols": ["BTC/USDT:USDT", "SOL/USDT:USDT", "XRP/USDT:USDT"],
    "default": {
         "entry_trigger_offset": 40.0,
         "trailing_drop_amount": 30.0,
         "trailing1_drop_amount": 5.0,
         "min_profit_for_trailing": 8.0,
         "stop_loss_offset": 2.0,
         "leverage": 2,
         "capital": 100.0,
    },
    "overrides": {
         "SOL/USDT:USDT": {
             "entry_trigger_offset": 0.4,           # Adjusted scale for SOL
             "trailing_drop_amount": 0.3,
             "trailing1_drop_amount": 0.05,
             "min_profit_for_trailing": 0.08,
             "stop_loss_offset": 0.02,
             "leverage": 2,
             "capital": 50.0,
         },
         "XRP/USDT:USDT": {
             "entry_trigger_offset": 0.1,
             "trailing_drop_amount": 0.075,
             "trailing1_drop_amount": 0.025,
             "min_profit_for_trailing": 0.02,
             "stop_loss_offset": 0.01,
             "leverage": 2,
             "capital": 50.0,
         }
    }
}

# API key file and key name – ensure your secret.json is set accordingly.
key_path = "LiveTradingBots/secret.json"
key_name = "envelope"

with open(key_path, "r") as f:
    api_setup = json.load(f)[key_name]

# Initialize BitgetFutures.
bitget = BitgetFutures(api_setup)

# =============================================================================
# GRID SCALPING BOT CLASS
# =============================================================================
class GridScalpingBot:
    def __init__(self, symbol: str, config: Dict):
        self.symbol = symbol
        # Use default parameters then override for this symbol if available.
        self.config = config["default"].copy()
        if symbol in config["overrides"]:
            self.config.update(config["overrides"][symbol])
        # State variables:
        self.reserved_price = None       # Reserved entry price.
        self.in_position = False         # Whether a trade is open.
        self.entry_price = None          # Price at which the trade was executed.
        self.stop_loss = None            # Fixed stop loss level.
        # For dynamic trailing stops (long trade):
        self.highest_price = None        # Highest price reached after entry.
        self.primary_trailing_stop = None  # Dynamic trailing stop (primary).
        self.trailing1_stop = None       # Fixed additional trailing stop.
        # For short trade, we use analogous variables:
        self.lowest_price = None
        # Store trade direction explicitly ("long" or "short").
        self.position_direction = None
        self.last_order_ids = []         # Not used for fixed orders in this version.
        self.position_size = None        # Store the calculated position size.

    def log(self, message: str):
        print(f"[{self.symbol}] {datetime.datetime.now().strftime('%H:%M:%S')}: {message}")

    def reserve_price_method(self):
        """Set the reserved price as the current market 'last' price."""
        try:
            ticker = bitget.fetch_ticker(self.symbol)
            self.reserved_price = float(ticker['last'])
            self.log(f"Reserved price set to {self.reserved_price}")
        except Exception as e:
            self.log(f"Error reserving price: {e}")

    def cancel_all_orders(self):
        """Cancel all open orders for this symbol."""
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

    def enter_position(self, direction: str):
        """
        When the trigger is reached, enter a position.
        For a long trade:
          - Execute at the reserved price.
          - Set stop loss at reserved - STOP_LOSS_OFFSET.
          - Set primary trailing stop (initially inactive) and update highest price.
          - Set Trailing1 stop at (reserved + ENTRY_TRIGGER_OFFSET - TRAILING1_DROP_AMOUNT).
        For a short trade, logic is reversed.
        """
        try:
            order_side = "buy" if direction == "long" else "sell"
            ticker = bitget.fetch_ticker(self.symbol)
            current_price = float(ticker['last'])
            # Execute the trade at the reserved price per client's demand.
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
                self.primary_trailing_stop = None  # Not active until profit >= MIN_PROFIT_FOR_TRAILING.
                self.trailing1_stop = self.reserved_price + self.config["entry_trigger_offset"] - self.config["trailing1_drop_amount"]
            else:
                self.stop_loss = self.entry_price + self.config["stop_loss_offset"]
                self.lowest_price = self.entry_price
                self.primary_trailing_stop = None
                self.trailing1_stop = self.reserved_price - self.config["entry_trigger_offset"] + self.config["trailing1_drop_amount"]

            self.log(f"Stop loss set at {self.stop_loss}")
            if direction == "long":
                self.log(f"Trailing1 stop set at {self.trailing1_stop}")
            else:
                self.log(f"Trailing1 stop set at {self.trailing1_stop}")
        except Exception as e:
            self.log(f"Error entering position: {e}")

    def update_primary_trailing_stop(self):
        """
        Update the primary trailing stop for dynamic profit protection.
        For long:
          - Update highest_price if current price exceeds it.
          - Activate primary trailing stop once (highest - entry) >= MIN_PROFIT_FOR_TRAILING,
            then set trailing stop = highest_price - TRAILING_DROP_AMOUNT.
        For short, analogous logic applies.
        """
        try:
            ticker = bitget.fetch_ticker(self.symbol)
            current_price = float(ticker['last'])
            direction = self.position_direction
            if direction == "long":
                if current_price > self.highest_price:
                    self.highest_price = current_price
                    if (self.highest_price - self.entry_price) >= self.config["min_profit_for_trailing"]:
                        self.primary_trailing_stop = self.highest_price - self.config["trailing_drop_amount"]
                        self.log(f"Updated primary trailing stop to {self.primary_trailing_stop} (highest price: {self.highest_price})")
            else:
                if current_price < self.lowest_price:
                    self.lowest_price = current_price
                    if (self.entry_price - self.lowest_price) >= self.config["min_profit_for_trailing"]:
                        self.primary_trailing_stop = self.lowest_price + self.config["trailing_drop_amount"]
                        self.log(f"Updated primary trailing stop to {self.primary_trailing_stop} (lowest price: {self.lowest_price})")
        except Exception as e:
            self.log(f"Error updating primary trailing stop: {e}")

    def check_exit_conditions(self) -> bool:
        """
        Check if any exit condition is met:
          For a long trade:
            - If current price falls to or below stop loss.
            - If primary trailing stop is active and current price falls to or below it.
            - If current price falls to or below the fixed Trailing1 stop.
          For a short trade, the logic is reversed.
        """
        try:
            ticker = bitget.fetch_ticker(self.symbol)
            current_price = float(ticker['last'])
            direction = self.position_direction

            if direction == "long":
                if current_price <= self.stop_loss:
                    self.log(f"Stop loss hit: {current_price} <= {self.stop_loss}")
                    return True
                if self.primary_trailing_stop is not None and current_price <= self.primary_trailing_stop:
                    self.log(f"Primary trailing stop hit: {current_price} <= {self.primary_trailing_stop}")
                    return True
                if current_price <= self.trailing1_stop:
                    self.log(f"Trailing1 stop hit: {current_price} <= {self.trailing1_stop}")
                    return True
            else:
                if current_price >= self.stop_loss:
                    self.log(f"Stop loss hit: {current_price} >= {self.stop_loss}")
                    return True
                if self.primary_trailing_stop is not None and current_price >= self.primary_trailing_stop:
                    self.log(f"Primary trailing stop hit: {current_price} >= {self.primary_trailing_stop}")
                    return True
                if current_price >= self.trailing1_stop:
                    self.log(f"Trailing1 stop hit: {current_price} >= {self.trailing1_stop}")
                    return True

            return False
        except Exception as e:
            self.log(f"Error checking exit conditions: {e}")
            return False

    def exit_position(self):
        """
        Exit the current position using a limit order at the stop loss price,
        instead of a market order. This helps to reduce slippage so that losses are minimal.
        """
        if not self.in_position:
            return
        try:
            exit_side = "sell" if self.position_direction == "long" else "buy"
            # Place a limit order at the stop loss price.
            exit_order = bitget.place_limit_order(self.symbol, exit_side, self.position_size, self.stop_loss, reduce=True)
            self.log(f"Placed exit limit order at {self.stop_loss}")
            self.log("Exited position via limit order")
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

    def check_for_trigger(self):
        """
        If not in position, check if the market price has moved enough from the reserved price to trigger an entry.
          For long: if current price >= reserved + ENTRY_TRIGGER_OFFSET, then trigger long entry.
          For short: if current price <= reserved - ENTRY_TRIGGER_OFFSET, then trigger short entry.
        """
        if self.in_position or self.reserved_price is None:
            return
        try:
            ticker = bitget.fetch_ticker(self.symbol)
            current_price = float(ticker['last'])
            trigger_offset = self.config["entry_trigger_offset"]
            if current_price >= self.reserved_price + trigger_offset:
                self.log(f"Trigger reached for LONG: {current_price} >= {self.reserved_price} + {trigger_offset}")
                self.cancel_all_orders()
                self.enter_position("long")
            elif current_price <= self.reserved_price - trigger_offset:
                self.log(f"Trigger reached for SHORT: {current_price} <= {self.reserved_price} - {trigger_offset}")
                self.cancel_all_orders()
                self.enter_position("short")
        except Exception as e:
            self.log(f"Error checking for trigger: {e}")

    def run_cycle(self):
        """
        Run one cycle:
          - If not in position, check for trigger.
          - If in position, update primary trailing stop and check exit conditions.
        """
        if not self.in_position:
            self.check_for_trigger()
        else:
            self.update_primary_trailing_stop()
            if self.check_exit_conditions():
                self.exit_position()
                # After exit, reset the reserved price.
                self.reserve_price_method()

# =============================================================================
# MAIN LOOP
# =============================================================================
def main():
    bots = {}
    for symbol in params["symbols"]:
        bot = GridScalpingBot(symbol, params)
        bot.reserve_price_method()
        bots[symbol] = bot

    while True:
        for symbol, bot in bots.items():
            try:
                bot.run_cycle()
            except Exception as e:
                bot.log(f"Error in run_cycle: {e}")
        time.sleep(5)

if __name__ == "__main__":
    main()


