"""
Sweeper Bot V2 - Market Discovery (Gamma API)

FIX #11: Added category detection (crypto, sports, politics, finance, geopolitics)
FIX #12: Added verify_trade_history() using DATA_API endpoint
"""
import requests, time, logging
from dataclasses import dataclass
from typing import Optional
from config.settings import GAMMA_API, DATA_API, get_fee_rate

logger = logging.getLogger("sweeper.discovery")

# FIX #11: Category mapping for fee rate lookup
CATEGORY_MAP = {
    "crypto": ["bitcoin", "btc", "ethereum", "eth", "crypto", "token", "defi", "solana", "xrp"],
    "sports": ["nba", "nfl", "mlb", "nhl", "soccer", "football", "basketball", "baseball", "hockey", "lakers", "warriors", "celtics"],
    "politics": ["election", "president", "congress", "senate", "governor", "political", "democrat", "republican", "primary"],
    "finance": ["fed", "rate", "interest", "gdp", "inflation", "cpi", "economic", "financial", "market", "stock", "bond"],
    "geopolitics": ["war", "ceasefire", "treaty", "sanction", "geopolitical", "conflict", "peace", "invasion", "nato"],
}

def detect_category(question: str, tags: list = None) -> str:
    q = (question or "").lower()
    if tags:
        for tag in tags:
            q += " " + str(tag).lower()
    for category, keywords in CATEGORY_MAP.items():
        for kw in keywords:
            if kw in q:
                return category
    return "other"

@dataclass
class CandidateMarket:
    condition_id: str
    question: str
    slug: str
    yes_token_id: str
    no_token_id: str
    yes_price: float
    no_price: float
    end_date: Optional[str]
    volume_24hr: float
    liquidity: float
    neg_risk: bool
    accepting_orders: bool
    sweep_score: float
    category: str = "other"  # FIX #11: Added category field
    raw: dict = None

class MarketDiscovery:
    def __init__(self, config):
        self.config = config
        self._session = requests.Session()

    def discover_candidates(self, max_markets=200):
        markets = []
        try:
            url = f"{GAMMA_API}/markets?limit={max_markets}&order=volume24hr&ascending=false&active=true"
            resp = self._session.get(url, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                for m in data:
                    try:
                        yes_price = float(m.get("outcomePrices", "[\"0.5\",\"0.5\"]").split('"')[1] if isinstance(m.get("outcomePrices"), str) else m.get("outcomePrices", [0.5, 0.5])[0])
                        no_price = 1.0 - yes_price
                        neg_risk = m.get("negRisk", False)
                        tokens = m.get("clobTokenIds", ["", ""])
                        if isinstance(tokens, str):
                            import json; tokens = json.loads(tokens)
                        category = detect_category(m.get("question", ""), m.get("tags", []))  # FIX #11
                        markets.append(CandidateMarket(
                            condition_id=m.get("conditionId", ""),
                            question=m.get("question", ""),
                            slug=m.get("slug", ""),
                            yes_token_id=tokens[0] if len(tokens) > 0 else "",
                            no_token_id=tokens[1] if len(tokens) > 1 else "",
                            yes_price=yes_price, no_price=no_price,
                            end_date=m.get("endDate"),
                            volume_24hr=float(m.get("volume24hr", 0)),
                            liquidity=float(m.get("liquidity", 0)),
                            neg_risk=neg_risk,
                            accepting_orders=m.get("acceptingOrders", True),
                            sweep_score=self._compute_score(yes_price, no_price, float(m.get("volume24hr", 0)), m.get("endDate")),
                            category=category,  # FIX #11
                            raw=m,
                        ))
                    except Exception as e:
                        logger.debug(f"Parse error for market: {e}")
                        continue
            else:
                logger.error(f"Gamma API returned {resp.status_code}")
        except Exception as e:
            logger.error(f"Gamma API error: {e}")
        markets.sort(key=lambda x: x.sweep_score, reverse=True)
        logger.info(f"[DISCOVERY] Found {len(markets)} candidate markets")
        return markets

    def _compute_score(self, yes_price, no_price, volume_24hr, end_date):
        max_price = max(yes_price, no_price)
        if max_price < 0.90: return 0.0
        score = (max_price - 0.90) * 100
        if volume_24hr > 10000: score += 20
        elif volume_24hr > 5000: score += 10
        if end_date:
            try:
                from datetime import datetime, timezone
                dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                days_left = (dt - datetime.now(timezone.utc)).days
                if 0 <= days_left <= 7: score += 15
                elif days_left < 0: score += 25
            except Exception: pass
        return score

    def get_market_book(self, token_id):
        from config.settings import CLOB_API
        try:
            url = f"{CLOB_API}/book?token_id={token_id}"
            resp = self._session.get(url, timeout=5)
            if resp.status_code == 200:
                return resp.json()
        except Exception as e:
            logger.debug(f"Book fetch error: {e}")
        return {"asks": [], "bids": []}

    # FIX #12: Added verify_trade_history using DATA_API
    def verify_trade_history(self, condition_id, token_id):
        """Verify trade history using the Polymarket DATA API."""
        try:
            url = f"{DATA_API}/trades?condition_id={condition_id}&token_id={token_id}&limit=10"
            resp = self._session.get(url, timeout=5)
            if resp.status_code == 200:
                trades = resp.json()
                logger.info(f"[DATA_API] {len(trades)} trades found for {condition_id[:16]}")
                return trades
            else:
                logger.debug(f"DATA_API returned {resp.status_code}")
        except Exception as e:
            logger.debug(f"DATA_API error: {e}")
        return []
