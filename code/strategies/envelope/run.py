import os
import sys
import time
import pandas as pd
import json
import ta
from datetime import datetime
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
from utilities.bitget_futures import BitgetFutures

# =============================================================================
# CONFIGURATION
# =============================================================================
# Define parameters for grid scalping. For BTC, the trigger threshold, grid profit 
# distance, fixed stop loss, and trailing stop drop are defined in USD.
# For other coins (e.g. SOL, XRP) these values are overridden to reflect their price scales.
params: Dict = {
    "symbols": ["BTC/USDT:USDT", "SOL/USDT:USDT", "XRP/USDT:USDT"],
    "default": {
         "trigger_threshold": 30.0,         # Price must move 30 USD from reserved price (BTC base)
         "grid_profit_distance": 20.0,        # Place take profit order 20 USD away from entry
         "fixed_stop_loss": 5.0,              # Fixed stop loss distance in USD
         "trailing_stop_drop": 30.0,          # Trailing stop will trigger if price falls 30 USD from peak
         "leverage": 2,
         "capital": 100.0,
    },
    "overrides": {
         "SOL/USDT:USDT": {
             "trigger_threshold": 0.3,        # Adjusted for SOL's price scale
             "grid_profit_distance": 0.2,
             "fixed_stop_loss": 0.1,
             "trailing_stop_drop": 0.3,
             "leverage": 2,
             "capital": 50.0,
         },
         "XRP/USDT:USDT": {
             "trigger_threshold": 0.1,
             "grid_profit_distance": 0.07,
             "fixed_stop_loss": 0.05,
             "trailing_stop_drop": 0.1,
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
        self.reserved_price = None      # The base price when starting/reseting the bot
        self.position = None            # Information about the active position
        self.entry_price = None         # Price at which the position was entered
        self.trailing_stop = None       # Dict holding 'peak' and 'stop' prices for trailing stop
        self.in_position = False        # Whether we currently hold a position
        self.last_order_ids = []        # Store IDs of orders placed (stop loss, take profit, etc.)
        self.last_profit_order_id = None

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
            self.position = order  # Store order info; you may later fetch detailed position info if needed

            # Immediately place a fixed stop loss order
            fixed_sl = self.config["fixed_stop_loss"]
            if direction == "long":
                stop_price = self.entry_price - fixed_sl
                sl_side = "sell"
            else:
                stop_price = self.entry_price + fixed_sl
                sl_side = "buy"
            sl_order = bitget.place_trigger_market_order(
                self.symbol, sl_side, position_size, trigger_price=stop_price, reduce=True)
            self.log(f"Placed fixed stop loss order at {stop_price}")
            self.last_order_ids.append(sl_order['id'])

            # Place a take profit (grid profit) limit order
            grid_profit = self.config["grid_profit_distance"]
            if direction == "long":
                tp_price = self.entry_price + grid_profit
                tp_side = "sell"
            else:
                tp_price = self.entry_price - grid_profit
                tp_side = "buy"
            tp_order = bitget.place_limit_order(self.symbol, tp_side, position_size, tp_price, reduce=True)
            self.log(f"Placed take profit order at {tp_price}")
            self.last_profit_order_id = tp_order['id']
            self.last_order_ids.append(tp_order['id'])

            # Initialize the trailing stop mechanism
            if direction == "long":
                initial_trailing_stop = self.entry_price - self.config["trailing_stop_drop"]
            else:
                initial_trailing_stop = self.entry_price + self.config["trailing_stop_drop"]
            self.trailing_stop = {"peak": self.entry_price, "stop": initial_trailing_stop}
            self.log(f"Initialized trailing stop: {self.trailing_stop}")
        except Exception as e:
            self.log(f"Error entering position: {e}")

    def update_trailing_stop(self):
        """Update the trailing stop if price moves favorably."""
        if not self.in_position or self.trailing_stop is None:
            return
        try:
            ticker = bitget.fetch_ticker(self.symbol)
            current_price = float(ticker['last'])
            # Determine direction based on how the position was entered.
            # (We assume that a market buy means a long position.)
            direction = "long" if self.position["side"].lower() == "buy" else "short"
            if direction == "long":
                if current_price > self.trailing_stop["peak"]:
                    self.trailing_stop["peak"] = current_price
                    self.trailing_stop["stop"] = current_price - self.config["trailing_stop_drop"]
                    self.log(f"Updated trailing stop to {self.trailing_stop}")
            else:
                if current_price < self.trailing_stop["peak"]:
                    self.trailing_stop["peak"] = current_price
                    self.trailing_stop["stop"] = current_price + self.config["trailing_stop_drop"]
                    self.log(f"Updated trailing stop to {self.trailing_stop}")
        except Exception as e:
            self.log(f"Error updating trailing stop: {e}")

    def check_exit_conditions(self) -> bool:
        """
        Check if exit conditions are met:
          - Price has retraced to the trailing stop level.
          - The take profit (grid) order has been filled.
        """
        if not self.in_position or self.trailing_stop is None:
            return False
        try:
            ticker = bitget.fetch_ticker(self.symbol)
            current_price = float(ticker['last'])
            direction = "long" if self.position["side"].lower() == "buy" else "short"
            if direction == "long" and current_price <= self.trailing_stop["stop"]:
                self.log(f"Trailing stop hit: current price {current_price} <= stop {self.trailing_stop['stop']}")
                return True
            elif direction == "short" and current_price >= self.trailing_stop["stop"]:
                self.log(f"Trailing stop hit: current price {current_price} >= stop {self.trailing_stop['stop']}")
                return True

            # Check if the take profit order is still open.
            open_orders = bitget.fetch_open_orders(self.symbol)
            profit_order_exists = any(o['id'] == self.last_profit_order_id for o in open_orders)
            if not profit_order_exists:
                self.log("Take profit order filled; exiting position")
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
            self.trailing_stop = None
            self.last_profit_order_id = None
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
        """Run one cycle: if not in position, look for a trigger; if in position, update trailing stop and check exit."""
        if not self.in_position:
            self.check_for_trigger()
        else:
            self.update_trailing_stop()
            if self.check_exit_conditions():
                self.exit_position()
                # Reset and reserve a new base price after exit
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
