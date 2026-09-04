"""
Sweeper Bot V2 - Configuration & Fee Math
DO NOT change strategy parameters without explicit user approval.

FIX #1: Dynamic fee rate per category (Crypto=0.07, Sports=0.05, etc.)
FIX #3: Standardized gas cost to 0.001/share everywhere
FIX #8: Export all contract addresses and constants from __init__.py
FIX #16: BotState.from_dict() now preserves rate_limit_429_count
P0 #3: Added signature_type and funder fields to SweeperConfig for V2 SDK compatibility
P1 #4: Added server-time/clock-drift validation via validate_server_time()
P1 #5: State file saves are now atomic AND versioned (keeps .bak backup)
P1 #6: All strategy parameters now load from .env via os.getenv with module constants as defaults
P1: .env support via os.getenv for sensitive fields; atomic BotState.save
P1: Added min_entry_price parameter (was hardcoded 0.985 in order_executor.py and run_dry.py)
SECTION 1 AUDIT: Strategy specification - MAX_ENTRY_PRICE, finality policies, merge/redeem rules
SECTION 2 AUDIT: Unsafe production execution guards - MAX_CANARY_FUNDED_USD, APPROVED_SDK_VERSION

"""
from dataclasses import dataclass, field
from typing import Optional
import json
import os
import time
import logging

logger = logging.getLogger("sweeper.config")

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
MIN_ENTRY_PRICE = 0.985  # P1: Parameterized entry floor

# === SECTION 1 AUDIT: STRATEGY SPECIFICATION ===
MAX_ENTRY_PRICE = 0.99  # $0.999 NOT viable: gross=$0.001, loser=$0.005, gas=$0.001 => net=-$0.005/share

# Per-category outcome finality policies (min block confirmations, dispute window hours, source required)
OUTCOME_FINALITY_POLICIES = {
    "crypto": {"min_blocks": 128, "dispute_window_hours": 2, "require_source": True},
    "sports": {"min_blocks": 128, "dispute_window_hours": 2, "require_source": True},
    "politics": {"min_blocks": 256, "dispute_window_hours": 2, "require_source": True},
    "finance": {"min_blocks": 128, "dispute_window_hours": 2, "require_source": True},
    "geopolitics": {"min_blocks": 256, "dispute_window_hours": 2, "require_source": True},
    "economics": {"min_blocks": 128, "dispute_window_hours": 2, "require_source": True},
    "tech": {"min_blocks": 128, "dispute_window_hours": 2, "require_source": True},
    "mentions": {"min_blocks": 128, "dispute_window_hours": 2, "require_source": True},
    "culture": {"min_blocks": 128, "dispute_window_hours": 2, "require_source": True},
    "weather": {"min_blocks": 128, "dispute_window_hours": 2, "require_source": True},
    "other": {"min_blocks": 128, "dispute_window_hours": 2, "require_source": True},
}

# Markets with these resolution-source patterns are automatic skips
UNSUPPORTED_RESOLUTION_SOURCES = ["manual", "unresolved", "ambiguous", "cancelled", "void"]

# Maximum acceptable probability that resolution is overturned
MAX_RESOLUTION_DISPUTE_RISK = 0.02  # 2% max

# SECTION 5 AUDIT: UMA resolution verification
UMA_CHALLENGE_PERIOD_HOURS = 2  # UMA Optimistic Oracle challenge period
UMA_ADAPTER_ADDRESSES = [
    "0x157Ce2d672854c848c9b79C49a8Cc6cc89176a49",  # UmaCtfAdapter v3.0
    "0x6A9D222616C90FcA5754cd1333cFD9b7fb6a4F74",  # UmaCtfAdapter v2.0
    "0xCB1822859cEF82Cd2Eb4E6276C7916e692995130",  # UmaCtfAdapter v1.0
]

# Merge-now vs wait-for-redemption decision rules
MERGE_THRESHOLD_SPREAD = 0.02  # If losing-side ask <= this, merge now
PREFER_MERGE_OVER_REDEEM = True  # Merge is cheaper when both sides available
REDEMPTION_MIN_WAIT_BLOCKS = 128  # Min blocks after resolution before redemption

# === SECTION 2 AUDIT: UNSAFE PRODUCTION EXECUTION GUARDS ===
MAX_CANARY_FUNDED_USD = 50.0  # Max wallet balance in canary/live-test mode
APPROVED_SDK_VERSION = "1.1.0"  # Must match requirements.txt py-clob-client-v2

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

def validate_server_time(max_drift_seconds=5.0):
    """P1 #4: Validate local clock drift against Polymarket server time.

    Fetches server time from CLOB API and compares with local time.
    Returns (drift_seconds, is_ok).
    Fails open (returns 0, True) if server is unreachable.
    """
    try:
        import urllib.request
        req = urllib.request.Request(f"{CLOB_API}/time", method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            server_time_ms = int(resp.read().decode().strip())
            server_time = server_time_ms / 1000.0
        local_time = time.time()
        drift = abs(local_time - server_time)
        if drift > max_drift_seconds:
            logger.warning(f"Clock drift {drift:.2f}s exceeds {max_drift_seconds}s threshold")
            return drift, False
        logger.info(f"Server time OK (drift: {drift:.3f}s)")
        return drift, True
    except Exception as e:
        logger.warning(f"Server time validation failed: {e}")
        return 0.0, True  # fail open

@dataclass
class SweeperConfig:
    # P1 #6: All strategy parameters now load from .env with module constants as defaults
    buy_price: float = field(default_factory=lambda: float(os.getenv("BUY_PRICE", str(BUY_PRICE))))
    loser_max_price: float = field(default_factory=lambda: float(os.getenv("LOSER_MAX_PRICE", str(LOSER_MAX_PRICE))))
    max_daily_loss: float = field(default_factory=lambda: float(os.getenv("MAX_DAILY_LOSS", str(MAX_DAILY_LOSS))))
    gas_floor: float = field(default_factory=lambda: float(os.getenv("GAS_FLOOR", str(GAS_FLOOR))))
    paper_mode: bool = field(default_factory=lambda: os.getenv("PAPER_MODE", "true").lower() != "false")
    prefer_maker: bool = field(default_factory=lambda: os.getenv("PREFER_MAKER", "true").lower() == "true")
    allow_taker_fallback: bool = field(default_factory=lambda: os.getenv("ALLOW_TAKER_FALLBACK", "false").lower() == "true")
    resting_order_timeout: float = field(default_factory=lambda: float(os.getenv("RESTING_ORDER_TIMEOUT", str(RESTING_ORDER_TIMEOUT))))
    order_reconcile_interval: float = field(default_factory=lambda: float(os.getenv("ORDER_RECONCILE_INTERVAL", str(ORDER_RECONCILE_INTERVAL))))
    cancel_orders_on_shutdown: bool = field(default_factory=lambda: os.getenv("CANCEL_ORDERS_ON_SHUTDOWN", "true").lower() == "true")
    touch_fill_seconds: float = field(default_factory=lambda: float(os.getenv("TOUCH_FILL_SECONDS", str(TOUCH_FILL_SECONDS))))
    fill_probability: float = field(default_factory=lambda: float(os.getenv("FILL_PROBABILITY", str(FILL_PROBABILITY))))
    ghost_probability: float = field(default_factory=lambda: float(os.getenv("GHOST_PROBABILITY", str(GHOST_PROBABILITY))))
    partial_fill_probability: float = field(default_factory=lambda: float(os.getenv("PARTIAL_FILL_PROBABILITY", str(PARTIAL_FILL_PROBABILITY))))
    partial_fill_ratio: float = field(default_factory=lambda: float(os.getenv("PARTIAL_FILL_RATIO", str(PARTIAL_FILL_RATIO))))
    max_event_exposure: float = field(default_factory=lambda: float(os.getenv("MAX_EVENT_EXPOSURE_USD", str(MAX_EVENT_EXPOSURE_USD))))
    max_per_market_exposure: float = field(default_factory=lambda: float(os.getenv("MAX_PER_MARKET_EXPOSURE", "200.0")))  # AUDIT FIX #8: Per-market risk limit
    max_portfolio_exposure: float = field(default_factory=lambda: float(os.getenv("MAX_PORTFOLIO_EXPOSURE_USD", str(MAX_PORTFOLIO_EXPOSURE_USD))))
    max_429_before_trip: int = field(default_factory=lambda: int(os.getenv("MAX_429_BEFORE_TRIP", str(MAX_429_BEFORE_TRIP))))
    rate_limit_order_per_min: int = field(default_factory=lambda: int(os.getenv("RATE_LIMIT_ORDER_PER_MIN", str(RATE_LIMIT_ORDER_PER_MIN))))
    rate_limit_book_per_min: int = field(default_factory=lambda: int(os.getenv("RATE_LIMIT_BOOK_PER_MIN", str(RATE_LIMIT_BOOK_PER_MIN))))
    rate_limit_gamma_per_min: int = field(default_factory=lambda: int(os.getenv("RATE_LIMIT_GAMMA_PER_MIN", str(RATE_LIMIT_GAMMA_PER_MIN))))
    rate_limit_headroom: float = field(default_factory=lambda: float(os.getenv("RATE_LIMIT_HEADROOM", str(RATE_LIMIT_HEADROOM))))
    polygon_rpc: str = field(default_factory=lambda: os.getenv("POLYGON_RPC", POLYGON_RPC))
    private_key: str = field(default_factory=lambda: os.getenv("PRIVATE_KEY", ""))
    clob_api_key: str = field(default_factory=lambda: os.getenv("CLOB_API_KEY", ""))
    clob_api_secret: str = field(default_factory=lambda: os.getenv("CLOB_API_SECRET", ""))
    clob_api_passphrase: str = field(default_factory=lambda: os.getenv("CLOB_API_PASSPHRASE", ""))
    wallet_address: str = field(default_factory=lambda: os.getenv("WALLET_ADDRESS", ""))
    signature_type: int = field(default_factory=lambda: int(os.getenv("SIGNATURE_TYPE", "0")))  # 0=EOA, 1=POLY_PROXY, 2=POLY_GNOSIS_SAFE, 3=POLY_1271
    funder: str = field(default_factory=lambda: os.getenv("FUNDER", ""))  # Funder address for proxy/Safe/deposit wallets
    fee_rate: float = DEFAULT_FEE_RATE
    min_entry_price: float = field(default_factory=lambda: float(os.getenv("MIN_ENTRY_PRICE", str(MIN_ENTRY_PRICE))))  # P1: Parameterized entry floor
    # SECTION 1 AUDIT: Strategy specification fields
    max_entry_price: float = field(default_factory=lambda: float(os.getenv("MAX_ENTRY_PRICE", str(MAX_ENTRY_PRICE))))
    max_resolution_dispute_risk: float = field(default_factory=lambda: float(os.getenv("MAX_RESOLUTION_DISPUTE_RISK", str(MAX_RESOLUTION_DISPUTE_RISK))))
    require_source_agreement: bool = field(default_factory=lambda: os.getenv("REQUIRE_SOURCE_AGREEMENT", "true").lower() == "true")
    merge_threshold_spread: float = field(default_factory=lambda: float(os.getenv("MERGE_THRESHOLD_SPREAD", str(MERGE_THRESHOLD_SPREAD))))
    prefer_merge_over_redeem: bool = field(default_factory=lambda: os.getenv("PREFER_MERGE_OVER_REDEEM", "true").lower() == "true")
    # SECTION 2 AUDIT: Production execution guards
    max_canary_funded_usd: float = field(default_factory=lambda: float(os.getenv("MAX_CANARY_FUNDED_USD", str(MAX_CANARY_FUNDED_USD))))

    def validate(self) -> list:
        """AUDIT FIX #30 + SECTION 1: Comprehensive config validation with detailed error messages."""
        errors = []
        # Strategy parameter validation
        if not (0.90 <= self.buy_price <= self.max_entry_price):
            errors.append(f"buy_price {self.buy_price} must be between 0.90 and {self.max_entry_price}")
        if not (0.0 <= self.loser_max_price <= 0.05):
            errors.append(f"loser_max_price {self.loser_max_price} must be between 0.0 and 0.05")
        if not (0.0 <= self.min_entry_price < self.buy_price):
            errors.append(f"min_entry_price {self.min_entry_price} must be >= 0.0 and < buy_price {self.buy_price}")
        if self.max_daily_loss <= 0:
            errors.append(f"max_daily_loss {self.max_daily_loss} must be > 0")
        if self.gas_floor <= 0:
            errors.append(f"gas_floor {self.gas_floor} must be > 0")
        if self.max_canary_funded_usd <= 0:
            errors.append(f"max_canary_funded_usd {self.max_canary_funded_usd} must be > 0")
        # Probability validation
        total_prob = self.fill_probability + self.partial_fill_probability + self.ghost_probability
        if total_prob > 1.0:
            errors.append(f"probabilities sum to {total_prob:.2f} > 1.0")
        return errors

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
        """P1 #5: Atomic save with versioning - write to temp, rename, keep .bak backup."""
        os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
        if os.path.exists(path):
            backup_path = path + '.bak'
            try:
                os.replace(path, backup_path)
            except OSError:
                pass
        tmp_path = path + '.tmp'
        with open(tmp_path, "w") as f: json.dump(self.to_dict(), f, indent=2)
        os.replace(tmp_path, path)

    @classmethod
    def load(cls, path):
        if os.path.exists(path):
            with open(path) as f: return cls.from_dict(json.load(f))
        return cls()

# === SECTION 8 AUDIT: ECONOMICS GATE ===
MIN_PROFIT_MARGIN = 0.001
MAX_SLIPPAGE = 0.002
MIN_ORDER_SIZE_ECONOMIC = 5.0
BREAK_EVEN_PRICE = BUY_PRICE + GAS_PER_SHARE + LOSER_MAX_PRICE

def calculate_break_even(buy_price, loser_price=LOSER_MAX_PRICE, gas_per_share=GAS_PER_SHARE, is_maker=False, fee_rate=DEFAULT_FEE_RATE):
    """SECTION 8 AUDIT: Calculate break-even selling price."""
    fee = fee_per_share(buy_price, fee_rate, is_maker)
    return buy_price + fee + loser_price + gas_per_share

def estimate_slippage(order_size, book_liquidity=1000):
    """SECTION 8 AUDIT: Estimate slippage for taker orders."""
    if book_liquidity <= 0:
        return float('inf')
    impact = order_size / book_liquidity
    return min(impact * 0.01, 0.05)
