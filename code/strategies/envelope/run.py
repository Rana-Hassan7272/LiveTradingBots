import os
import sys
import json
import time
from datetime import datetime
from utilities.bitget_futures import BitgetFutures

# --- CONFIG ---
params = {
    'symbol': 'BTC/USDT:USDT',
    'timeframe': '1m',  # not directly used in this logic
    'margin_mode': 'isolated',
    'balance_fraction': 1,
    'leverage': 1,
    'trigger_distance': 30,          # USD move from reserved price to trigger entry
    'trailing_stop_distance': 30,    # USD drop from extreme price to exit
    'grid_distance': 20,             # grid level for partial profit (optional)
    'use_long': True,
    'use_short': True,
}

key_path = 'LiveTradingBots/secret.json'
key_name = 'envelope'  # change as needed

# Tracker file for storing reserved price and order/position info
tracker_file = f"LiveTradingBots/code/strategies/reserved/tracker_{params['symbol'].replace('/', '-').replace(':', '-')}.json"

# --- AUTHENTICATION ---
print(f"\n{datetime.now().strftime('%H:%M:%S')}: >>> starting execution for {params['symbol']}")
with open(key_path, "r") as f:
    api_setup = json.load(f)[key_name]
bitget = BitgetFutures(api_setup)

# --- Tracker File Utilities ---
def init_tracker(file_path):
    tracker = {
        "status": "waiting_for_entry",  # statuses: waiting_for_entry, position_open, exit_triggered
        "reserved_price": None,
        "entry_order_ids": {"long": None, "short": None},
        "entry_direction": None,
        "extreme_price": None,  # highest for long, lowest for short
        "position_id": None,
    }
    with open(file_path, 'w') as file:
        json.dump(tracker, file)
    return tracker

def read_tracker(file_path):
    with open(file_path, 'r') as file:
        return json.load(file)

def update_tracker(file_path, tracker):
    with open(file_path, 'w') as file:
        json.dump(tracker, file)

if not os.path.exists(tracker_file):
    tracker = init_tracker(tracker_file)
else:
    tracker = read_tracker(tracker_file)

# --- Fetch current market price ---
ticker = bitget.fetch_ticker(params['symbol'])
current_price = ticker['last']
print(f"{datetime.now().strftime('%H:%M:%S')}: current market price is {current_price}")

# --- Check for open position ---
positions = bitget.fetch_open_positions(params['symbol'])
open_position = positions[0] if positions else None

if not open_position:
    # No open position; handle entry logic.
    # If no reserved price yet, set it to the current market price.
    if tracker["reserved_price"] is None:
        tracker["reserved_price"] = current_price
        update_tracker(tracker_file, tracker)
        print(f"{datetime.now().strftime('%H:%M:%S')}: reserved price set to {tracker['reserved_price']}")
    
    reserved_price = tracker["reserved_price"]
    trigger_distance = params['trigger_distance']
    
    # Calculate trigger levels for long and short entries.
    long_trigger_price = reserved_price + trigger_distance
    short_trigger_price = reserved_price - trigger_distance
    
    order_executed = False
    
    # Check for long entry condition.
    if params['use_long'] and current_price >= long_trigger_price:
        # Execute long entry immediately (you may choose to place a trigger order via API instead).
        balance = params['balance_fraction'] * params['leverage'] * bitget.fetch_balance()['USDT']['total']
        amount = balance / long_trigger_price  # basic sizing strategy
        print(f"{datetime.now().strftime('%H:%M:%S')}: long trigger met (price reached {current_price}). Placing long order.")
        order = bitget.place_market_order(params['symbol'], 'buy', amount)
        tracker["entry_direction"] = "long"
        tracker["status"] = "position_open"
        tracker["extreme_price"] = current_price  # starting highest price for long
        tracker["position_id"] = order.get('id')
        update_tracker(tracker_file, tracker)
        order_executed = True
        # (Any pending short order would be canceled here if previously placed.)
    
    # Check for short entry condition.
    elif params['use_short'] and current_price <= short_trigger_price:
        balance = params['balance_fraction'] * params['leverage'] * bitget.fetch_balance()['USDT']['total']
        amount = balance / short_trigger_price
        print(f"{datetime.now().strftime('%H:%M:%S')}: short trigger met (price reached {current_price}). Placing short order.")
        order = bitget.place_market_order(params['symbol'], 'sell', amount)
        tracker["entry_direction"] = "short"
        tracker["status"] = "position_open"
        tracker["extreme_price"] = current_price  # starting lowest price for short
        tracker["position_id"] = order.get('id')
        update_tracker(tracker_file, tracker)
        order_executed = True
    
    if not order_executed:
        print(f"{datetime.now().strftime('%H:%M:%S')}: No entry trigger met. Waiting...")
        print(f"Reserved price: {reserved_price}, Long trigger: {long_trigger_price}, Short trigger: {short_trigger_price}")
        sys.exit()

else:
    # A position is open; handle trailing stop management.
    position = open_position  # assuming a single open position
    entry_direction = tracker.get("entry_direction")
    
    # For a long position, update the highest price reached.
    if entry_direction == "long":
        if current_price > tracker.get("extreme_price", 0):
            tracker["extreme_price"] = current_price
            update_tracker(tracker_file, tracker)
            print(f"{datetime.now().strftime('%H:%M:%S')}: Updated highest price to {tracker['extreme_price']}")
        # Check if the price has dropped by the trailing stop distance from the highest price.
        if current_price <= tracker["extreme_price"] - params['trailing_stop_distance']:
            print(f"{datetime.now().strftime('%H:%M:%S')}: Trailing stop hit for long (current: {current_price}, highest: {tracker['extreme_price']}). Exiting position.")
            bitget.place_market_order(params['symbol'], 'sell', position['contracts'] * position['contractSize'], reduce=True)
            tracker["status"] = "exit_triggered"
            update_tracker(tracker_file, tracker)
            sys.exit()
        else:
            print(f"{datetime.now().strftime('%H:%M:%S')}: Long position active. Current price: {current_price}, Highest: {tracker['extreme_price']}")
    
    # For a short position, update the lowest price reached.
    elif entry_direction == "short":
        if current_price < tracker.get("extreme_price", float('inf')):
            tracker["extreme_price"] = current_price
            update_tracker(tracker_file, tracker)
            print(f"{datetime.now().strftime('%H:%M:%S')}: Updated lowest price to {tracker['extreme_price']}")
        # Check if the price has risen by the trailing stop distance from the lowest price.
        if current_price >= tracker["extreme_price"] + params['trailing_stop_distance']:
            print(f"{datetime.now().strftime('%H:%M:%S')}: Trailing stop hit for short (current: {current_price}, lowest: {tracker['extreme_price']}). Exiting position.")
            bitget.place_market_order(params['symbol'], 'buy', position['contracts'] * position['contractSize'], reduce=True)
            tracker["status"] = "exit_triggered"
            update_tracker(tracker_file, tracker)
            sys.exit()
        else:
            print(f"{datetime.now().strftime('%H:%M:%S')}: Short position active. Current price: {current_price}, Lowest: {tracker['extreme_price']}")

print(f"{datetime.now().strftime('%H:%M:%S')}: <<< execution completed")

