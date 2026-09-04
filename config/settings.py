"""
Sweeper Bot V2 - Configuration & Fee Math
DO NOT change strategy parameters without explicit user approval.

FIX #1: Dynamic fee rate per category (Crypto=0.07, Sports=0.05, etc.)
FIX #3: Standardized gas cost to 0.001/share everywhere
FIX #8: Export all contract addresses and constants from __init__.py
FIX #16: BotState.from_dict() now preserves rate_limit_429_count
P0 #3: Added signature_type and funder fields to SweeperConfig for V2 SDK compatibility
P1: .env support via os.getenv for sensitive fields; atomic BotState.save
"""
from dataclasses import dataclass, field
from typing import Optional
import json
import os

# === CONTRACT ADDRESSES (verified against official docs) ===
PUSD = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
CTF = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
CTF_EXCHANGE = "0xE111180000d2663C0091e4f400237545B87B996B"
NEG_RISK_EXCHANGE = "0xe2222d279d744050d28e00520010520000310F59"
COLLATERAL_ONRAMP = "0x93070a847efEf7F70739046A929D47a521F5B8ee"
COLLATERAL_OFFRAMP = "0x2957922Eb93258b93368531d39fAcCA3B4dC5854"
CTF_COLLATERAL_ADAPTER = "0xAdA100Db00Ca00073811820692005400218FcE1f"
NEG_RISK_CTF_COLLATERAL_ADAPTER = "0xadA2005600Dec949baf300f4C6120000bDB6eAab"

# === API ENDPOINTS ===
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
DATA_API = "https://data-api.polymarket.com"
RELAYER_API = "https://relayer-v2.polymarket.com/submit"
WS_MARKET = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
WS_USER = "wss://ws-subscriptions-clob.polymarket.com/ws/user"
POLYGON_RPC = "https://polygon-rpc.com"

# === RATE LIMITS ===
RATE_LIMIT_ORDER_PER_MIN = 50
RATE_LIMIT_BOOK_PER_MIN = 200
RATE_LIMIT_GAMMA_PER_MIN = 200
RATE_LIMIT_API_KEY_PER_MIN = 80
RATE_LIMIT_RELAYER_PER_MIN = 20
RATE_LIMIT_HEADROOM = 0.8

# === STRATEGY PARAMETERS (DO NOT CHANGE) ===
BUY_PRICE = 0.99
LOSER_MAX_PRICE = 0.005
GROSS_EDGE = 1.0 - BUY_PRICE
MAX_DAILY_LOSS = 50.0
GAS_FLOOR = 0.5
RECONCILIATION_INTERVAL = 30
FILL_CONFIRM_TIMEOUT = 8
PROCESSING_FLOOR_MS = 20

PREFER_MAKER = True
ALLOW_TAKER_FALLBACK = False
RESTING_ORDER_TIMEOUT = 120.0
ORDER_RECONCILE_INTERVAL = 2.0
CANCEL_ORDERS_ON_SHUTDOWN = True
TOUCH_FILL_SECONDS = 8.0
FILL_PROBABILITY = 0.35
GHOST_PROBABILITY = 0.05
PARTIAL_FILL_PROBABILITY = 0.25
PARTIAL_FILL_RATIO = 0.4
MAX_EVENT_EXPOSURE_USD = 500.0
MAX_PORTFOLIO_EXPOSURE_USD = 2000.0
MAX_429_BEFORE_TRIP = 3

# === FIX #1: DYNAMIC FEE RATES PER CATEGORY ===
DEFAULT_FEE_RATE = 0.04
FEE_RATES = {
    "crypto": 0.07, "sports": 0.05, "finance": 0.04, "politics": 0.04,
    "economics": 0.05, "geopolitics": 0.00, "tech": 0.04, "mentions": 0.04,
    "culture": 0.05, "weather": 0.05, "other": 0.05,
}

def get_fee_rate(category="other"):
    return FEE_RATES.get(category, DEFAULT_FEE_RATE)

def fee_per_share(price, fee_rate=DEFAULT_FEE_RATE, is_maker=False):
    if is_maker: return 0.0
    return fee_rate * price * (1.0 - price)

def fee_total(price, shares, fee_rate=DEFAULT_FEE_RATE, is_maker=False):
    return fee_per_share(price, fee_rate, is_maker) * shares

def fee_fraction_of_edge(price, fee_rate=DEFAULT_FEE_RATE, is_maker=False):
    gross = 1.0 - price
    if gross <= 0: return float('inf')
    if is_maker: return 0.0
    return fee_per_share(price, fee_rate, is_maker) / gross

GAS_PER_SHARE = 0.001

def net_edge_per_share(buy_price, loser_price, gas_per_share=GAS_PER_SHARE, is_maker=False):
    gross = 1.0 - buy_price
    fee = fee_per_share(buy_price, is_maker=is_maker)
    return gross - fee - loser_price - gas_per_share

def min_viable_size(gas_cost, buy_price=BUY_PRICE, is_maker=False):
    edge = net_edge_per_share(buy_price, LOSER_MAX_PRICE, gas_cost / 100, is_maker=is_maker)
    if edge <= 0: return float('inf')
    return gas_cost / edge

@dataclass
class SweeperConfig:
    buy_price: float = BUY_PRICE
    loser_max_price: float = LOSER_MAX_PRICE
    max_daily_loss: float = MAX_DAILY_LOSS
    gas_floor: float = GAS_FLOOR
    paper_mode: bool = True
    prefer_maker: bool = PREFER_MAKER
    allow_taker_fallback: bool = ALLOW_TAKER_FALLBACK
    resting_order_timeout: float = RESTING_ORDER_TIMEOUT
    order_reconcile_interval: float = ORDER_RECONCILE_INTERVAL
    cancel_orders_on_shutdown: bool = CANCEL_ORDERS_ON_SHUTDOWN
    touch_fill_seconds: float = TOUCH_FILL_SECONDS
    fill_probability: float = FILL_PROBABILITY
    ghost_probability: float = GHOST_PROBABILITY
    partial_fill_probability: float = PARTIAL_FILL_PROBABILITY
    partial_fill_ratio: float = PARTIAL_FILL_RATIO
    max_event_exposure: float = MAX_EVENT_EXPOSURE_USD
    max_portfolio_exposure: float = MAX_PORTFOLIO_EXPOSURE_USD
    max_429_before_trip: int = MAX_429_BEFORE_TRIP
    rate_limit_order_per_min: int = RATE_LIMIT_ORDER_PER_MIN
    rate_limit_book_per_min: int = RATE_LIMIT_BOOK_PER_MIN
    rate_limit_gamma_per_min: int = RATE_LIMIT_GAMMA_PER_MIN
    rate_limit_headroom: float = RATE_LIMIT_HEADROOM
    polygon_rpc: str = POLYGON_RPC
    private_key: str = field(default_factory=lambda: os.getenv("PRIVATE_KEY", ""))
    clob_api_key: str = field(default_factory=lambda: os.getenv("CLOB_API_KEY", ""))
    clob_api_secret: str = field(default_factory=lambda: os.getenv("CLOB_API_SECRET", ""))
    clob_api_passphrase: str = field(default_factory=lambda: os.getenv("CLOB_API_PASSPHRASE", ""))
    wallet_address: str = field(default_factory=lambda: os.getenv("WALLET_ADDRESS", ""))
    signature_type: int = field(default_factory=lambda: int(os.getenv("SIGNATURE_TYPE", "0")))  # 0=EOA, 1=POLY_PROXY, 2=POLY_GNOSIS_SAFE, 3=POLY_1271
    funder: str = field(default_factory=lambda: os.getenv("FUNDER", ""))  # Funder address for proxy/Safe/deposit wallets
    fee_rate: float = DEFAULT_FEE_RATE

    def validate(self):
        if not (0.90 <= self.buy_price <= 0.999): return False
        if self.max_daily_loss <= 0 or self.gas_floor <= 0: return False
        return True

    def net_edge(self, is_maker=False):
        return net_edge_per_share(self.buy_price, self.loser_max_price, is_maker=is_maker)

@dataclass
class BotState:
    worked_markets: set = field(default_factory=set)
    open_positions: dict = field(default_factory=dict)
    daily_pnl: float = 0.0
    cumulative_pnl: float = 0.0
    total_buys: int = 0
    total_redeems: int = 0
    total_merges: int = 0
    total_recycled: float = 0.0
    open_orders: list = field(default_factory=list)
    reserved_collateral: float = 0.0
    rate_limit_429_count: int = 0

    def to_dict(self):
        return {"worked_markets": list(self.worked_markets), "open_positions": self.open_positions,
            "daily_pnl": self.daily_pnl, "cumulative_pnl": self.cumulative_pnl,
            "total_buys": self.total_buys, "total_redeems": self.total_redeems,
            "total_merges": self.total_merges, "total_recycled": self.total_recycled,
            "open_orders": self.open_orders, "reserved_collateral": self.reserved_collateral,
            "rate_limit_429_count": self.rate_limit_429_count}

    @classmethod
    def from_dict(cls, d):
        return cls(worked_markets=set(d.get("worked_markets", [])),
            open_positions=d.get("open_positions", {}), daily_pnl=d.get("daily_pnl", 0.0),
            cumulative_pnl=d.get("cumulative_pnl", 0.0), total_buys=d.get("total_buys", 0),
            total_redeems=d.get("total_redeems", 0), total_merges=d.get("total_merges", 0),
            total_recycled=d.get("total_recycled", 0.0), open_orders=d.get("open_orders", []),
            reserved_collateral=d.get("reserved_collateral", 0.0),
            rate_limit_429_count=d.get("rate_limit_429_count", 0))

    def save(self, path):
        """P1: Atomic save - write to temp file then rename to prevent corruption."""
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp_path = path + '.tmp'
        with open(tmp_path, "w") as f: json.dump(self.to_dict(), f, indent=2)
        os.replace(tmp_path, path)

    @classmethod
    def load(cls, path):
        if os.path.exists(path):
            with open(path) as f: return cls.from_dict(json.load(f))
        return cls()