import os
import sys
import json
import ta
from datetime import datetime

# Extend the path so that BitgetFutures can be imported
sys.path.append(os.path.join(os.path.dirname(__file__), '..', '..'))
from utilities.bitget_futures import BitgetFutures

# --- CONFIGURATION ---
# Choose strategy: 'scalping' or 'grid'
strategy = 'scalping'  # Change to 'grid' to run the grid strategy

if strategy == 'scalping':
    # Scalping parameters for multiple symbols
    params = {
        'symbols': ['BTC/USDT:USDT', 'SOL/USDT:USDT', 'XRP/USDT:USDT'],
        'timeframe': '1m',
        'margin_mode': 'isolated',
        'balance_fraction': 0.1,
        'leverage': 20,
        'risk_per_trade': 0.02,
        # Trend Filter
        'use_trend_filter': True,
        'trend_timeframe': '5m',
        'trend_ema_period': 50,
        # Entry Parameters
        'entry_ema_fast': 9,
        'entry_ema_slow': 21,
        'rsi_period': 14,
        'rsi_overbought': 70,
        'rsi_oversold': 30,
        # Exit Parameters
        'trailing_stop_distance': 6,  # in USD
        'take_profit_ratio': 1.5,     # Ratio relative to stop distance
        'max_trade_duration': 300,    # in seconds
    }
else:  # grid strategy
    params = {
        'symbols': ['BTC/USDT:USDT', 'SOL/USDT:USDT', 'XRP/USDT:USDT'],
        'timeframe': '1m',
        'margin_mode': 'isolated',
        'balance_per_symbol': 100,  # USD per symbol
        'leverage': 2,
        'grid_distance': 5,         # USD between grids (adjustable)
        'num_grids': 4,             # Set to 4 for long and 4 for short (adjustable)
        'fixed_stop_loss': 5,       # Fixed stop loss in USD (adjustable)
        'trail_stop_activate_grid': 2,  # Activate trailing stop from 2nd grid (adjustable)
        'trailing_stop_distance': 6,    # USD trailing stop (adjustable)
        'trend_filter': True,
        'trend_ema_period': 50,
        'max_active_grids': 4,
    }

key_path = 'LiveTradingBots/secret.json'
key_name = 'envelope'  # Change to your key name if needed

print(f"\n{datetime.now().strftime('%H:%M:%S')}: Starting {strategy.capitalize()} Bot")
with open(key_path, "r") as f:
    api_setup = json.load(f)[key_name]
bitget = BitgetFutures(api_setup)

# --- Helper Functions ---
def calculate_indicators(df: pd.DataFrame, strategy='scalping') -> pd.DataFrame:
    if strategy == 'scalping':
        df['ema_fast'] = ta.trend.ema_indicator(df['close'], window=params['entry_ema_fast'])
        df['ema_slow'] = ta.trend.ema_indicator(df['close'], window=params['entry_ema_slow'])
        df['rsi'] = ta.momentum.rsi(df['close'], window=params['rsi_period'])
        try:
            df['vwap'] = ta.volume.volume_weighted_average_price(df['high'], df['low'], df['close'], df['volume'])
        except Exception as e:
            print(f"Error calculating VWAP: {e}")
    return df

def get_trend_direction(trend_data: pd.DataFrame) -> str:
    if params.get('use_trend_filter'):
        trend_ema = ta.trend.ema_indicator(trend_data['close'], window=params['trend_ema_period'])
        return 'bullish' if trend_data['close'].iloc[-1] > trend_ema.iloc[-1] else 'bearish'
    return None

# --- Scalping Engine (Multiple Symbols) ---
class ScalpingEngine:
    def __init__(self, symbol):
        self.symbol = symbol
        self.trailing_stop = None  # Dictionary with 'peak_price' and 'stop_price'

    def manage_trades(self):
        try:
            positions = bitget.fetch_open_positions(self.symbol)
        except Exception as e:
            print(f"[{self.symbol}] Error fetching open positions: {e}")
            return

        for position in positions:
            try:
                current_price = float(position['markPrice'])
            except Exception as e:
                print(f"[{self.symbol}] Error extracting current price: {e}")
                continue

            # Initialize trailing stop if not set
            if self.trailing_stop is None:
                if position['side'] == 'long':
                    stop_price = current_price - params['trailing_stop_distance']
                else:  # short
                    stop_price = current_price + params['trailing_stop_distance']
                self.trailing_stop = {'peak_price': current_price, 'stop_price': stop_price}
                print(f"[{self.symbol}] Initialized trailing stop: {self.trailing_stop}")

            # Update trailing stop
            if position['side'] == 'long':
                if current_price > self.trailing_stop['peak_price']:
                    self.trailing_stop['peak_price'] = current_price
                    self.trailing_stop['stop_price'] = current_price - params['trailing_stop_distance']
            else:  # short
                if current_price < self.trailing_stop['peak_price']:
                    self.trailing_stop['peak_price'] = current_price
                    self.trailing_stop['stop_price'] = current_price + params['trailing_stop_distance']
            print(f"[{self.symbol}] Updated trailing stop: {self.trailing_stop}")

            # Check exit conditions based on trailing stop
            exit_condition = False
            if position['side'] == 'long' and current_price <= self.trailing_stop['stop_price']:
                exit_condition = True
            elif position['side'] == 'short' and current_price >= self.trailing_stop['stop_price']:
                exit_condition = True

            if exit_condition:
                try:
                    bitget.flash_close_position(self.symbol)
                    print(f"[{self.symbol}] Closed {position['side']} position due to trailing stop at {self.trailing_stop['stop_price']}")
                except Exception as e:
                    print(f"[{self.symbol}] Error closing position: {e}")
                finally:
                    self.trailing_stop = None

    def check_entries(self, data: pd.DataFrame, trend_direction: str):
        if data is None or data.empty:
            return
        last_close = data['close'].iloc[-1]
        if len(data) < 2:
            return

        # EMA crossover condition: fast EMA crosses above slow EMA
        ema_crossover = (data['ema_fast'].iloc[-2] < data['ema_slow'].iloc[-2] and 
                         data['ema_fast'].iloc[-1] > data['ema_slow'].iloc[-1])
        rsi_value = data['rsi'].iloc[-1]

        if params.get('use_trend_filter'):
            if trend_direction == 'bullish' and not ema_crossover:
                if rsi_value > params['rsi_oversold']:
                    print(f"[{self.symbol}] Bullish trend but RSI not oversold for long entry.")
                    return
            elif trend_direction == 'bearish' and ema_crossover:
                if rsi_value < params['rsi_overbought']:
                    print(f"[{self.symbol}] Bearish trend but RSI not overbought for short entry.")
                    return

        if ema_crossover and rsi_value < params['rsi_oversold']:
            self.enter_trade('long', last_close)
        elif (not ema_crossover) and rsi_value > params['rsi_overbought']:
            self.enter_trade('short', last_close)
        else:
            print(f"[{self.symbol}] No entry signal based on EMA crossover and RSI conditions.")

    def enter_trade(self, side: str, entry_price: float):
        try:
            balance = bitget.fetch_balance()['USDT']['total']
        except Exception as e:
            print(f"[{self.symbol}] Error fetching balance: {e}")
            return

        risk_amount = balance * params['risk_per_trade']
        try:
            position_size = (risk_amount * params['leverage']) / entry_price
            position_size = float(bitget.amount_to_precision(self.symbol, position_size))
        except Exception as e:
            print(f"[{self.symbol}] Error calculating position size: {e}")
            return

        try:
            order_side = 'buy' if side == 'long' else 'sell'
            order = bitget.place_market_order(
                symbol=self.symbol,
                side=order_side,
                amount=position_size
            )
            print(f"[{self.symbol}] Entered {side} position at {entry_price} with size {position_size}")
        except Exception as e:
            print(f"[{self.symbol}] Error entering {side} trade: {e}")
            return

        try:
            if side == 'long':
                tp_price = entry_price + (params['trailing_stop_distance'] * params['take_profit_ratio'])
                tp_side = 'sell'
            else:  # short
                tp_price = entry_price - (params['trailing_stop_distance'] * params['take_profit_ratio'])
                tp_side = 'buy'

            tp_order = bitget.place_limit_order(
                symbol=self.symbol,
                side=tp_side,
                amount=position_size,
                price=tp_price,
                reduce=True
            )
            print(f"[{self.symbol}] Placed take profit order at {tp_price} for {side} position.")
        except Exception as e:
            print(f"[{self.symbol}] Error placing take profit order: {e}")

# --- Grid Trader (Using Modified Features) ---
class GridTrader:
    def __init__(self, symbol):
        self.symbol = symbol
        self.grids = {'long': [], 'short': []}
        self.active_orders = []
        self.position = None
        self.trailing_stop = None
        self.last_price = None
        self.fixed_stop_order_placed = False  # New flag to track fixed stop loss order

    def calculate_grids(self, current_price):
        # Create grid levels based on the current price
        self.grids['long'] = [round(current_price + i * params['grid_distance'], 1) 
                              for i in range(1, params['num_grids'] + 1)]
        self.grids['short'] = [round(current_price - i * params['grid_distance'], 1) 
                               for i in range(1, params['num_grids'] + 1)]

    def place_grid_orders(self):
        self.cancel_all_orders()
        balance = params['balance_per_symbol'] * params['leverage']
        grid_size = balance / params['num_grids']
        
        # Place orders only on the activated side based on market trend
        for price in self.grids['long'][:params['max_active_grids']]:
            try:
                order = bitget.place_limit_order(
                    symbol=self.symbol,
                    side='buy',
                    amount=grid_size / price,
                    price=price,
                )
                self.active_orders.append(order['id'])
                print(f"[{self.symbol}] Placed long grid order at {price}")
            except Exception as e:
                print(f"[{self.symbol}] Error placing long order: {e}")
        
        for price in self.grids['short'][:params['max_active_grids']]:
            try:
                order = bitget.place_limit_order(
                    symbol=self.symbol,
                    side='sell',
                    amount=grid_size / price,
                    price=price,
                )
                self.active_orders.append(order['id'])
                print(f"[{self.symbol}] Placed short grid order at {price}")
            except Exception as e:
                print(f"[{self.symbol}] Error placing short order: {e}")
    
    def cancel_all_orders(self):
        try:
            orders = bitget.fetch_open_orders(self.symbol)
            for order in orders:
                bitget.cancel_order(order['id'], self.symbol)
            self.active_orders = []
        except Exception as e:
            print(f"[{self.symbol}] Error canceling orders: {e}")

    def check_filled_orders(self):
        positions = bitget.fetch_open_positions(self.symbol)
        if positions:
            self.position = positions[0]
            # Once a grid order is filled, immediately place a fixed stop loss if not done already.
            if not self.fixed_stop_order_placed:
                self.place_fixed_stop_loss()
            self.update_stop_management()
    
    def place_fixed_stop_loss(self):
        if self.position:
            try:
                entry_price = float(self.position['entryPrice'])
                side = self.position['side']
                fixed_stop = params.get('fixed_stop_loss', 5)
                if side == 'long':
                    stop_price = entry_price - fixed_stop
                    order_side = 'sell'
                else:
                    stop_price = entry_price + fixed_stop
                    order_side = 'buy'
                # Place a trigger market order as fixed stop loss.
                # Using the contract/amount from position if available.
                amount = self.position.get('contracts', None)
                if amount is None:
                    print(f"[{self.symbol}] Unable to place fixed stop loss: missing contract amount.")
                    return
                stop_order = bitget.place_trigger_market_order(
                    symbol=self.symbol,
                    side=order_side,
                    amount=float(amount),
                    trigger_price=stop_price,
                    reduce=True,
                    print_error=True
                )
                print(f"[{self.symbol}] Placed fixed stop loss order at {stop_price} for {side} position.")
                self.fixed_stop_order_placed = True
            except Exception as e:
                print(f"[{self.symbol}] Error placing fixed stop loss order: {e}")
    
    def update_stop_management(self):
        if self.position:
            current_price = float(self.position['markPrice'])
            entry_price = float(self.position['entryPrice'])
            # Recalculate grid levels based on entry price for trailing stop reference
            grid_levels = []
            if self.position['side'] == 'long':
                grid_levels = [entry_price + i * params['grid_distance'] for i in range(1, params['num_grids'] + 1)]
            else:
                grid_levels = [entry_price - i * params['grid_distance'] for i in range(1, params['num_grids'] + 1)]
            
            current_grid = None
            for i, level in enumerate(grid_levels):
                if (self.position['side'] == 'long' and current_price >= level) or \
                   (self.position['side'] == 'short' and current_price <= level):
                    current_grid = i + 1
            # Activate trailing stop only when current grid >= the activation threshold.
            if current_grid and current_grid >= params['trail_stop_activate_grid']:
                if not self.trailing_stop or current_price > self.trailing_stop['peak_price']:
                    self.trailing_stop = {
                        'peak_price': current_price,
                        'stop_price': current_price - params['trailing_stop_distance'] if self.position['side'] == 'long' else current_price + params['trailing_stop_distance']
                    }
                    print(f"[{self.symbol}] Activated/Updated trailing stop from grid {current_grid}: {self.trailing_stop}")
    
    def check_stop_conditions(self):
        if self.trailing_stop and self.position:
            current_price = float(self.position['markPrice'])
            if (self.position['side'] == 'long' and current_price <= self.trailing_stop['stop_price']) or \
               (self.position['side'] == 'short' and current_price >= self.trailing_stop['stop_price']):
                self.close_position()
    
    def close_position(self):
        try:
            bitget.flash_close_position(self.symbol)
            print(f"[{self.symbol}] Closed position due to stop condition")
            self.reset_trading()
        except Exception as e:
            print(f"[{self.symbol}] Error closing position: {e}")
    
    def reset_trading(self):
        self.position = None
        self.trailing_stop = None
        self.fixed_stop_order_placed = False
        self.cancel_all_orders()
        time.sleep(2)
        self.start_trading()
    
    def check_trend(self):
        if params.get('trend_filter'):
            data = bitget.fetch_recent_ohlcv(self.symbol, '15m', 100)
            ema = ta.trend.ema_indicator(data['close'], params['trend_ema_period'])
            return 'bullish' if data['close'].iloc[-1] > ema.iloc[-1] else 'bearish'
        return None
    
    def start_trading(self):
        ticker = bitget.fetch_ticker(self.symbol)
        self.last_price = float(ticker['last'])
        trend = self.check_trend()
        self.calculate_grids(self.last_price)
        # Activate only the side matching the current trend.
        if trend == 'bullish':
            self.grids['short'] = []
        elif trend == 'bearish':
            self.grids['long'] = []
        self.place_grid_orders()

# --- Main Execution Loop with 24/7 Mode ---
def run_bot():
    if strategy == 'scalping':
        # Create a scalping engine instance for each symbol
        engines = { symbol: ScalpingEngine(symbol) for symbol in params['symbols'] }
        while True:
            for symbol, engine in engines.items():
                try:
                    main_data = bitget.fetch_recent_ohlcv(symbol, params['timeframe'], 100)
                    if main_data is None or main_data.empty:
                        print(f"[{symbol}] No main market data received. Skipping iteration.")
                        continue
                    main_data = calculate_indicators(main_data, strategy='scalping')
                    if params.get('use_trend_filter'):
                        trend_data = bitget.fetch_recent_ohlcv(symbol, params['trend_timeframe'], 100)
                        if trend_data is None or trend_data.empty:
                            print(f"[{symbol}] No trend data received. Skipping trend filter check.")
                            trend_direction = None
                        else:
                            trend_direction = get_trend_direction(trend_data)
                            print(f"[{symbol}] Trend direction: {trend_direction}")
                    else:
                        trend_direction = None

                    engine.manage_trades()
                    engine.check_entries(main_data, trend_direction)
                except Exception as e:
                    print(f"[{symbol}] Error in processing: {e}")
            time.sleep(10)
    elif strategy == 'grid':
        traders = { symbol: GridTrader(symbol) for symbol in params['symbols'] }
        for symbol, trader in traders.items():
            try:
                bitget.set_margin_mode(symbol, params['margin_mode'])
                bitget.set_leverage(symbol, params['margin_mode'], params['leverage'])
                trader.start_trading()
            except Exception as e:
                print(f"[{symbol}] Error initializing: {e}")
        while True:
            try:
                for symbol, trader in traders.items():
                    try:
                        ticker = bitget.fetch_ticker(symbol)
                        trader.last_price = float(ticker['last'])
                        trader.check_filled_orders()
                        trader.check_stop_conditions()
                        # Reset grids every 5 minutes
                        if int(time.time()) % 300 == 0:
                            trader.reset_trading()
                    except Exception as e:
                        print(f"[{symbol}] Error processing: {e}")
                time.sleep(5)
            except KeyboardInterrupt:
                print("\nExiting Grid Bot via KeyboardInterrupt.")
                break
            except Exception as e:
                print(f"Main loop error: {e}")
                time.sleep(30)

# -------------------
# 24/7 Mode: Outer Watchdog Loop
# -------------------
if __name__ == "__main__":
    while True:
        try:
            run_bot()
        except KeyboardInterrupt:
            print("Exiting bot via KeyboardInterrupt.")
            break
        except Exception as e:
            print(f"Unhandled exception in run_bot: {e}")
        print("Restarting bot in 10 seconds...")
        time.sleep(10)