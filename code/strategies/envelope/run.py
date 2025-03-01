import os
import sys
import time
import pandas as pd
import json
import ta
from datetime import datetime

# Extend the path so that BitgetFutures can be imported
sys.path.append(os.path.join(os.path.dirname(__file__), '..', '..'))
from utilities.bitget_futures import BitgetFutures

# --- CONFIGURATION ---
# Choose strategy: 'scalping' or 'grid'
strategy = 'grid'  # Set to 'grid' to run the custom hybrid grid strategy

if strategy == 'scalping':
    # Scalping parameters for multiple symbols (unchanged)
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
        'trend_ema_period': 30,
        # Entry Parameters
        'entry_ema_fast': 9,
        'entry_ema_slow': 21,
        'rsi_period': 14,
        'rsi_overbought': 65,
        'rsi_oversold': 35,
        # Exit Parameters
        'trailing_stop_distance': 6,  # in USD
        'take_profit_ratio': 1.5,     # Ratio relative to stop distance
        'max_trade_duration': 300,    # in seconds
    }
else:  # grid strategy with custom hybrid logic
    params = {
        'symbols': ['BTC/USDT:USDT', 'SOL/USDT:USDT', 'XRP/USDT:USDT'],
        'timeframe': '1m',
        'margin_mode': 'isolated',
        'balance_per_symbol': 100,       # default USD per symbol (adjust per symbol if needed)
        'leverage': 2,
        'entry_trigger_offset': 30,      # Entry trigger offset in USD
        'fixed_stop_loss': 20,           # Fixed stop loss offset (grid exit) in USD
        'trailing_stop_distance': 30,    # Trailing stop offset in USD
        'trend_filter': True,
        'trend_ema_period': 50,
    }
    # (Optional) Symbol-specific overrides—for example, for XRP you might set a minimum allowed price.
    symbol_specific_params = {
        'BTC/USDT:USDT': {
            'balance_per_symbol': 100,
        },
        'SOL/USDT:USDT': {
            'balance_per_symbol': 50,
        },
        'XRP/USDT:USDT': {
            'balance_per_symbol': 50,
            'min_price': 2.1272,  # Explicit minimum allowed price for XRP
        },
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

            if self.trailing_stop is None:
                if position['side'] == 'long':
                    stop_price = current_price - params['trailing_stop_distance']
                else:
                    stop_price = current_price + params['trailing_stop_distance']
                self.trailing_stop = {'peak_price': current_price, 'stop_price': stop_price}
                print(f"[{self.symbol}] Initialized trailing stop: {self.trailing_stop}")

            if position['side'] == 'long':
                if current_price > self.trailing_stop['peak_price']:
                    self.trailing_stop['peak_price'] = current_price
                    self.trailing_stop['stop_price'] = current_price - params['trailing_stop_distance']
            else:
                if current_price < self.trailing_stop['peak_price']:
                    self.trailing_stop['peak_price'] = current_price
                    self.trailing_stop['stop_price'] = current_price + params['trailing_stop_distance']
            print(f"[{self.symbol}] Updated trailing stop: {self.trailing_stop}")

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
            else:
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

# --- Grid Trader (Custom Hybrid Strategy) ---
class GridTrader:
    def __init__(self, symbol):
        self.symbol = symbol
        self.reserved_price = None
        self.trend = None
        self.position = None
        self.trailing_stop = None
        self.fixed_stop_order_placed = False

    def start_trading(self):
        # Reserve the entry market price and perform trend analysis.
        ticker = bitget.fetch_ticker(self.symbol)
        self.reserved_price = float(ticker['last'])
        self.trend = self.check_trend()
        print(f"[{self.symbol}] Reserved entry price: {self.reserved_price} with trend {self.trend}")

    def check_entry_trigger(self):
        # Only attempt entry if no position exists.
        ticker = bitget.fetch_ticker(self.symbol)
        current_price = float(ticker['last'])
        if self.trend == 'bullish' and current_price >= self.reserved_price + params['entry_trigger_offset']:
            balance = params['balance_per_symbol'] * params['leverage']
            amount = balance / self.reserved_price
            try:
                order = bitget.place_limit_order(
                    symbol=self.symbol,
                    side='buy',
                    amount=amount,
                    price=self.reserved_price
                )
                print(f"[{self.symbol}] Entry triggered for LONG at reserved price {self.reserved_price}")
            except Exception as e:
                print(f"[{self.symbol}] Error placing long entry order: {e}")
        elif self.trend == 'bearish' and current_price <= self.reserved_price - params['entry_trigger_offset']:
            balance = params['balance_per_symbol'] * params['leverage']
            amount = balance / self.reserved_price
            try:
                order = bitget.place_limit_order(
                    symbol=self.symbol,
                    side='sell',
                    amount=amount,
                    price=self.reserved_price
                )
                print(f"[{self.symbol}] Entry triggered for SHORT at reserved price {self.reserved_price}")
            except Exception as e:
                print(f"[{self.symbol}] Error placing short entry order: {e}")

    def check_filled_orders(self):
        positions = bitget.fetch_open_positions(self.symbol)
        if positions:
            self.position = positions[0]
            if not self.fixed_stop_order_placed:
                self.place_fixed_stop_loss()
            self.update_trailing_stop()

    def place_fixed_stop_loss(self):
        if self.position:
            try:
                entry_price = float(self.position['entryPrice'])
                side = self.position['side']
                fixed_stop = params.get('fixed_stop_loss', 20)
                if side == 'long':
                    stop_price = entry_price - fixed_stop
                    order_side = 'sell'
                else:
                    stop_price = entry_price + fixed_stop
                    order_side = 'buy'
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

    def update_trailing_stop(self):
        if self.position:
            current_price = float(self.position['markPrice'])
            if not self.trailing_stop:
                if self.position['side'] == 'long':
                    self.trailing_stop = {'peak_price': current_price, 'stop_price': current_price - params['trailing_stop_distance']}
                else:
                    self.trailing_stop = {'peak_price': current_price, 'stop_price': current_price + params['trailing_stop_distance']}
                print(f"[{self.symbol}] Initialized trailing stop: {self.trailing_stop}")
            else:
                if self.position['side'] == 'long':
                    if current_price > self.trailing_stop['peak_price']:
                        self.trailing_stop['peak_price'] = current_price
                        self.trailing_stop['stop_price'] = current_price - params['trailing_stop_distance']
                else:
                    if current_price < self.trailing_stop['peak_price']:
                        self.trailing_stop['peak_price'] = current_price
                        self.trailing_stop['stop_price'] = current_price + params['trailing_stop_distance']
                print(f"[{self.symbol}] Updated trailing stop: {self.trailing_stop}")

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
        # Reserve a new entry price and re-run trend analysis
        self.start_trading()

    def check_trend(self):
        data = bitget.fetch_recent_ohlcv(self.symbol, '15m', 100)
        ema = ta.trend.ema_indicator(data['close'], params['trend_ema_period'])
        return 'bullish' if data['close'].iloc[-1] > ema.iloc[-1] else 'bearish'

# --- Main Execution Loop with 24/7 Mode ---
def run_bot():
    if strategy == 'scalping':
        engines = {symbol: ScalpingEngine(symbol) for symbol in params['symbols']}
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
        traders = {symbol: GridTrader(symbol) for symbol in params['symbols']}
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
                        # If no open position, check the entry trigger.
                        if trader.position is None:
                            trader.check_entry_trigger()
                        trader.check_filled_orders()
                        trader.check_stop_conditions()
                    except Exception as e:
                        print(f"[{symbol}] Error processing: {e}")
                time.sleep(5)
            except KeyboardInterrupt:
                print("\nExiting Grid Bot via KeyboardInterrupt.")
                break
            except Exception as e:
                print(f"Main loop error: {e}")
                time.sleep(30)

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
