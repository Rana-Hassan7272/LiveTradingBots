import ccxt
import time
import pandas as pd
from typing import Any, Optional, Dict, List

class BitgetFutures:
    def __init__(self, api_setup: Optional[Dict[str, Any]] = None) -> None:
        if api_setup is None:
            self.session = ccxt.bitget()
        else:
            api_setup.setdefault("options", {"defaultType": "future"})
            self.session = ccxt.bitget(api_setup)
        self.markets = self.session.load_markets()
  
    def fetch_ticker(self, symbol: str) -> Dict[str, Any]:
        try:
            return self.session.fetch_ticker(symbol)
        except Exception as e:
            raise Exception(f"Failed to fetch ticker for {symbol}: {e}")

    def fetch_min_amount_tradable(self, symbol: str) -> float:
        try:
            return self.markets[symbol]['limits']['amount']['min']
        except Exception as e:
            raise Exception(f"Failed to fetch minimum amount tradable for {symbol}: {e}")        
        
    def amount_to_precision(self, symbol: str, amount: float) -> str:
        try:
            return self.session.amount_to_precision(symbol, amount)
        except Exception as e:
            raise Exception(f"Failed to convert amount {amount} for {symbol} to precision: {e}")

    def price_to_precision(self, symbol: str, price: float) -> str:
        try:
            return self.session.price_to_precision(symbol, price)
        except Exception as e:
            raise Exception(f"Failed to convert price {price} for {symbol} to precision: {e}")

    def fetch_balance(self, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if params is None:
            params = {}
        try:
            return self.session.fetch_balance(params)
        except Exception as e:
            raise Exception(f"Failed to fetch balance: {e}")

    def fetch_order(self, id: str, symbol: str) -> Dict[str, Any]:
        try:
            return self.session.fetch_order(id, symbol)
        except Exception as e:
            raise Exception(f"Failed to fetch order {id} for {symbol}: {e}")

    def fetch_open_orders(self, symbol: str) -> List[Dict[str, Any]]:
        try:
            return self.session.fetch_open_orders(symbol)
        except Exception as e:
            raise Exception(f"Failed to fetch open orders for {symbol}: {e}")

    def cancel_order(self, id: str, symbol: str) -> Dict[str, Any]:
        try:
            return self.session.cancel_order(id, symbol)
        except Exception as e:
            raise Exception(f"Failed to cancel order {id} for {symbol}: {e}")

    def flash_close_position(self, symbol: str, side: Optional[str] = None) -> Dict[str, Any]:
        try:
            return self.session.close_position(symbol, side=side)
        except Exception as e:
            raise Exception(f"Failed to close position for {symbol}: {e}")

    def place_market_order(self, symbol: str, side: str, amount: float, reduce: bool = False) -> Dict[str, Any]:
        try:
            params = {'reduceOnly': reduce}
            amount_str = self.amount_to_precision(symbol, amount)
            return self.session.create_order(symbol, 'market', side, amount_str, params=params)
        except Exception as e:
            raise Exception(f"Failed to place market order for {symbol}: {e}")

    def place_limit_order(self, symbol: str, side: str, amount: float, price: float, reduce: bool = False) -> Dict[str, Any]:
        try:
            params = {'reduceOnly': reduce}
            amount_str = self.amount_to_precision(symbol, amount)
            price_str = self.price_to_precision(symbol, price)
            return self.session.create_order(symbol, 'limit', side, amount_str, price_str, params=params)
        except Exception as e:
            raise Exception(f"Failed to place limit order for {symbol}: {e}")

    def place_trigger_market_order(self, symbol: str, side: str, amount: float, trigger_price: float, reduce: bool = False, print_error: bool = False) -> Optional[Dict[str, Any]]:
        try:
            amount_str = self.amount_to_precision(symbol, amount)
            trigger_price_str = self.price_to_precision(symbol, trigger_price)
            params = {'reduceOnly': reduce, 'triggerPrice': trigger_price_str, 'delegateType': 'price_fill'}
            return self.session.create_order(symbol, 'market', side, amount_str, params=params)
        except Exception as err:
            if print_error:
                print(err)
                return None
            else:
                raise Exception(f"Failed to place trigger market order for {symbol}: {err}")

