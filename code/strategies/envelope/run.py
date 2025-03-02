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
# Define parameters for grid scalping.
# For BTC, the trigger threshold, grid profit distance, trailing stop (for locking profit)
# and the new trailing stop offsets for loss protection are defined in USD.
# For other coins these values are overridden to reflect their price scales.
params: Dict = {
    "symbols": ["BTC/USDT:USDT", "SOL/USDT:USDT", "XRP/USDT:USDT"],
    "default": {
         "trigger_threshold": 30.0,         # Price must move 30 USD from reserved price (BTC base)
         "trailing_stop_drop": 30.0,          # Profit trailing stop drop for locking gains
         "down_trailing_offset": 5.0,         # Loss trailing stop offset (for long positions)
         "up_trailing_offset": 5.0,           # Loss trailing stop offset (for short positions)
         "leverage": 2,
         "capital": 100.0,
    },
    "overrides": {
         "SOL/USDT:USDT": {
             "trigger_threshold": 0.3,        # Adjusted for SOL's price scale
             "trailing_stop_drop": 0.3,
             "down_trailing_offset": 0.1,
             "up_trailing_offset": 0.1,
             "leverage": 2,
             "capital": 50.0,
         },
         "XRP/USDT:USDT": {
             "trigger_threshold": 0.1,
             "trailing_stop_drop": 0.1,
             "down_trailing_offset": 0.05,
             "up_trailing_offset": 0.05,
             "leverage": 2,
             "capital": 50.0,
         }
    }
}

# API key file and key name (ensure your secret.json is set up accordingly)
key_path = "LiveTradingBots/secret.json"
key_name = "envelope"

with open(key_path, "r") as f:
    api_setup = json.load(f)[key_name]

# Initialize BitgetFutures
bitget = BitgetFutures(api_setup)

# =============================================================================
# GRID SCALPING BOT CLASS
# =============================================================================
class GridScalpingBot:
    def __init__(self, symbol: str, config: Dict):
        self.symbol = symbol
        # Use default parameters then override if specific values exist for this symbol
        self.config = config["default"].copy()
        if symbol in config["overrides"]:
            self.config.update(config["overrides"][symbol])
        # State variables
        self.reserved_price = None      # The base price when starting/resetting the bot
        self.position = None            # Information about the active position
        self.entry_price = None         # Price at which the position was entered
        self.in_position = False        # Whether we currently hold a position
        # Trailing stops:
        # profit_trailing_stop is used to lock in profit if the market reverses from an upward move (for long)
        # loss_trailing_stop is used to control the exit when the market moves against the position.
        self.profit_trailing_stop = None
        self.loss_trailing_stop = None
        self.last_order_ids = []        # (No longer used for fixed orders in this version)

    def log(self, message: str):
        print(f"[{self.symbol}] {datetime.datetime.now().strftime('%H:%M:%S')}: {message}")

    def reserve_price(self):
        """Reserve the current market price (do not trade immediately)."""
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
        Enter a market position in the specified direction.
        'long' for upward trigger and 'short' for downward trigger.
        Fixed stop loss and take profit orders are removed.
        Instead, we initialize two trailing stops:
         - A profit trailing stop (to lock in gains)
         - A loss trailing stop that tracks adverse moves
        """
        try:
            order_side = "buy" if direction == "long" else "sell"
            ticker = bitget.fetch_ticker(self.symbol)
            current_price = float(ticker['last'])
            self.entry_price = current_price
            # Calculate position size based on capital, leverage and current price
            position_size = (self.config["capital"] * self.config["leverage"]) / current_price
            position_size = float(bitget.amount_to_precision(self.symbol, position_size))
            # Place market order to enter position
            order = bitget.place_market_order(self.symbol, order_side, position_size)
            self.log(f"Entered {direction.upper()} position at {current_price} with size {position_size}")
            self.in_position = True
            self.position = order  # Store order info

            # Initialize profit trailing stop:
            if direction == "long":
                self.profit_trailing_stop = {"peak": self.entry_price, "stop": self.entry_price - self.config["trailing_stop_drop"]}
                # Initialize loss trailing stop for a long trade (set just below the entry)
                self.loss_trailing_stop = self.entry_price - self.config["down_trailing_offset"]
            else:
                self.profit_trailing_stop = {"peak": self.entry_price, "stop": self.entry_price + self.config["trailing_stop_drop"]}
                # Initialize loss trailing stop for a short trade (set just above the entry)
                self.loss_trailing_stop = self.entry_price + self.config["up_trailing_offset"]

            self.log(f"Initialized profit trailing stop: {self.profit_trailing_stop}")
            self.log(f"Initialized loss trailing stop: {self.loss_trailing_stop}")
        except Exception as e:
            self.log(f"Error entering position: {e}")

    def update_profit_trailing_stop(self):
        """Update the profit trailing stop if the market moves favorably."""
        if not self.in_position or self.profit_trailing_stop is None:
            return
        try:
            ticker = bitget.fetch_ticker(self.symbol)
            current_price = float(ticker['last'])
            # Determine direction based on position side
            direction = "long" if self.position["side"].lower() == "buy" else "short"
            if direction == "long":
                if current_price > self.profit_trailing_stop["peak"]:
                    self.profit_trailing_stop["peak"] = current_price
                    self.profit_trailing_stop["stop"] = current_price - self.config["trailing_stop_drop"]
                    self.log(f"Updated profit trailing stop to {self.profit_trailing_stop}")
            else:
                if current_price < self.profit_trailing_stop["peak"]:
                    self.profit_trailing_stop["peak"] = current_price
                    self.profit_trailing_stop["stop"] = current_price + self.config["trailing_stop_drop"]
                    self.log(f"Updated profit trailing stop to {self.profit_trailing_stop}")
        except Exception as e:
            self.log(f"Error updating profit trailing stop: {e}")

    def update_loss_trailing_stop(self):
        """Update the loss trailing stop if the market moves against the position."""
        if not self.in_position or self.loss_trailing_stop is None:
            return
        try:
            ticker = bitget.fetch_ticker(self.symbol)
            current_price = float(ticker['last'])
            direction = "long" if self.position["side"].lower() == "buy" else "short"
            if direction == "long":
                # Only update if market is below entry (i.e. in loss territory)
                if current_price < self.entry_price:
                    new_stop = current_price - self.config["down_trailing_offset"]
                    # For a long, we want the stop to follow downward (i.e., become lower) if the price falls further
                    if new_stop < self.loss_trailing_stop:
                        self.loss_trailing_stop = new_stop
                        self.log(f"Updated loss trailing stop to {self.loss_trailing_stop}")
            else:
                # For a short, update if market is above entry
                if current_price > self.entry_price:
                    new_stop = current_price + self.config["up_trailing_offset"]
                    if new_stop > self.loss_trailing_stop:
                        self.loss_trailing_stop = new_stop
                        self.log(f"Updated loss trailing stop to {self.loss_trailing_stop}")
        except Exception as e:
            self.log(f"Error updating loss trailing stop: {e}")

    def check_exit_conditions(self) -> bool:
        """
        Check if exit conditions are met.
        For a long position:
          - Profit scenario: if current price falls below the profit trailing stop's stop level.
          - Loss scenario: if, while in loss territory (current price below entry), the price rebounds to or above the loss trailing stop.
        For a short position, the logic is reversed.
        """
        if not self.in_position or self.profit_trailing_stop is None or self.loss_trailing_stop is None:
            return False
        try:
            ticker = bitget.fetch_ticker(self.symbol)
            current_price = float(ticker['last'])
            direction = "long" if self.position["side"].lower() == "buy" else "short"

            if direction == "long":
                profit_triggered = current_price <= self.profit_trailing_stop["stop"]
                loss_triggered = (current_price >= self.loss_trailing_stop) and (current_price < self.entry_price)
                if profit_triggered:
                    self.log(f"Profit trailing stop hit: current price {current_price} <= stop {self.profit_trailing_stop['stop']}")
                    return True
                if loss_triggered:
                    self.log(f"Loss trailing stop hit: current price {current_price} >= loss stop {self.loss_trailing_stop}")
                    return True
            else:
                profit_triggered = current_price >= self.profit_trailing_stop["stop"]
                loss_triggered = (current_price <= self.loss_trailing_stop) and (current_price > self.entry_price)
                if profit_triggered:
                    self.log(f"Profit trailing stop hit: current price {current_price} >= stop {self.profit_trailing_stop['stop']}")
                    return True
                if loss_triggered:
                    self.log(f"Loss trailing stop hit: current price {current_price} <= loss stop {self.loss_trailing_stop}")
                    return True

            return False
        except Exception as e:
            self.log(f"Error checking exit conditions: {e}")
            return False

    def exit_position(self):
        """Exit the current position by issuing a market order and cancel pending orders."""
        if not self.in_position:
            return
        try:
            bitget.flash_close_position(self.symbol)
            self.log("Exited position via market order")
            self.cancel_all_orders()
        except Exception as e:
            self.log(f"Error exiting position: {e}")
        finally:
            self.in_position = False
            self.position = None
            self.profit_trailing_stop = None
            self.loss_trailing_stop = None
            self.last_order_ids = []

    def check_for_trigger(self):
        """
        If not in position, check if the current market price has moved enough
        (up or down) from the reserved price to trigger an entry.
        """
        if self.in_position or self.reserved_price is None:
            return
        try:
            ticker = bitget.fetch_ticker(self.symbol)
            current_price = float(ticker['last'])
            trigger = self.config["trigger_threshold"]
            if current_price >= self.reserved_price + trigger:
                self.log(f"Trigger reached for LONG: {current_price} >= {self.reserved_price} + {trigger}")
                self.cancel_all_orders()
                self.enter_position("long")
            elif current_price <= self.reserved_price - trigger:
                self.log(f"Trigger reached for SHORT: {current_price} <= {self.reserved_price} - {trigger}")
                self.cancel_all_orders()
                self.enter_position("short")
        except Exception as e:
            self.log(f"Error checking for trigger: {e}")

    def run_cycle(self):
        """
        Run one cycle:
          - If not in position, check for trigger.
          - If in position, update both profit and loss trailing stops and then check exit conditions.
        """
        if not self.in_position:
            self.check_for_trigger()
        else:
            self.update_profit_trailing_stop()
            self.update_loss_trailing_stop()
            if self.check_exit_conditions():
                self.exit_position()
                # After exit, reserve a new base price
                self.reserve_price()

# =============================================================================
# MAIN LOOP
# =============================================================================
def main():
    # Create a bot instance for each symbol and reserve the starting price.
    bots = {}
    for symbol in params["symbols"]:
        bot = GridScalpingBot(symbol, params)
        bot.reserve_price()
        bots[symbol] = bot

    # Run continuously (24/7 mode)
    while True:
        for symbol, bot in bots.items():
            try:
                bot.run_cycle()
            except Exception as e:
                bot.log(f"Error in run_cycle: {e}")
        time.sleep(5)  # Adjust the sleep interval as needed

if __name__ == "__main__":
    main()

