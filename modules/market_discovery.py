"""
Sweeper Bot V2 - Market Discovery (Gamma API)

FIX #11: Added category detection (crypto, sports, politics, finance, geopolitics)
FIX #12: Added verify_trade_history() using DATA_API endpoint
P1: Parse actual NO price from outcomePrices (was computing 1-YES)
P1: Enforce accepting_orders filter (was including markets not accepting orders)
AUDIT FIX #22: Market caching, retry logic, deduplication, discovery metrics

SECTION 6 AUDIT: Market discovery (closed market discovery, pagination, caching, rate limiting)
"""
import requests, time, logging, json
from dataclasses import dataclass
from typing import Optional
from config.settings import GAMMA_API, DATA_API

logger = logging.getLogger("sweeper.discovery")

CATEGORY_MAP = {
    "crypto": ["bitcoin", "btc", "ethereum", "eth", "crypto", "token", "defi", "solana", "xrp"],
    "sports": ["nba", "nfl", "mlb", "nhl", "soccer", "football", "basketball", "baseball", "hockey", "lakers", "warriors", "celtics", "calcio", "serie a", "premier league", "tennis", "golf", "mma", "ufc", "boxing", "cricket", "rugby", "f1", "formula 1", "la liga", "champions league", "win on"],
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
    category: str = "other"
    tick_size: float = 0.01
    min_order_size: float = 5.0
    raw: dict = None

class MarketDiscovery:
    def __init__(self, config):
        self.config = config
        self._session = requests.Session()
        self._cache = {}
        self._cache_ttl = 60.0
        self._total_discovered = 0
        self._total_api_errors = 0
        self._cache_hits = 0
        self._max_retries = 3
        self._seen_condition_ids = set()
        # SECTION 6 AUDIT: Rate limiting
        self._last_request_time = 0.0
        self._min_request_interval = 0.3
        # SECTION 6 AUDIT: Book caching
        self._book_cache = {}
        self._book_cache_ttl = 5.0

    def _rate_limit(self):
        """SECTION 6 AUDIT: Enforce rate limiting on API calls."""
        elapsed = time.time() - self._last_request_time
        if elapsed < self._min_request_interval:
            time.sleep(self._min_request_interval - elapsed)
        self._last_request_time = time.time()

    def _fetch_with_retry(self, url, timeout=10, use_cache=False, cache_key=None):
        """AUDIT FIX #22 + SECTION 6 AUDIT: Fetch with retry, caching, rate limiting."""
        if use_cache and cache_key and cache_key in self._cache:
            cached = self._cache[cache_key]
            if time.time() - cached['time'] < self._cache_ttl:
                self._cache_hits += 1
                return cached['response']
        for attempt in range(self._max_retries):
            try:
                self._rate_limit()
                resp = self._session.get(url, timeout=timeout)
                if resp.status_code == 200:
                    if use_cache and cache_key:
                        self._cache[cache_key] = {'response': resp, 'time': time.time()}
                    return resp
                elif resp.status_code == 429:
                    wait = min(2 ** attempt, 10)
                    logger.warning(f"Rate limited (429), waiting {wait}s (attempt {attempt+1}/{self._max_retries})")
                    time.sleep(wait)
                else:
                    self._total_api_errors += 1
                    logger.error(f"API returned {resp.status_code} (attempt {attempt+1}/{self._max_retries})")
                    return resp
            except Exception as e:
                self._total_api_errors += 1
                if attempt < self._max_retries - 1:
                    wait = min(2 ** attempt, 10)
                    logger.warning(f"Fetch error: {e}, retrying in {wait}s (attempt {attempt+1}/{self._max_retries})")
                    time.sleep(wait)
                else:
                    logger.error(f"Fetch failed after {self._max_retries} attempts: {e}")
                    return None
        return None

    def discover_candidates(self, max_markets=200):
        """SECTION 6 AUDIT: Discover active markets with pagination support."""
        markets = []
        offset = 0
        limit = min(max_markets, 200)
        total_fetched = 0
        while total_fetched < max_markets:
            batch_size = min(limit, max_markets - total_fetched)
            cache_key = f"active_{batch_size}_{offset}"
            resp = self._fetch_with_retry(
                f"{GAMMA_API}/markets?limit={batch_size}&order=volume24hr&ascending=false&active=true&offset={offset}",
                use_cache=True, cache_key=cache_key
            )
            if not resp or resp.status_code != 200:
                break
            try:
                data = resp.json()
                if not data:
                    break
                for m in data:
                    try:
                        prices = m.get("outcomePrices", "[\"0.5\",\"0.5\"]")
                        if isinstance(prices, str):
                            prices = json.loads(prices)
                        yes_price = float(prices[0])
                        no_price = float(prices[1]) if len(prices) > 1 else 1.0 - yes_price
                        neg_risk = m.get("negRisk", False)
                        tokens = m.get("clobTokenIds", ["", ""])
                        if isinstance(tokens, str):
                            tokens = json.loads(tokens)
                        category = detect_category(m.get("question", ""), m.get("tags", []))
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
                            category=category,
                            tick_size=float(m.get("orderPriceMinTickSize", 0.01)),
                            min_order_size=float(m.get("orderMinSize", 5)),
                            raw=m,
                        ))
                    except Exception as e:
                        logger.debug(f"Parse error for market: {e}")
                        continue
                total_fetched += len(data)
                if len(data) < batch_size:
                    break
                offset += batch_size
            except Exception as e:
                self._total_api_errors += 1
                logger.error(f"Gamma API parse error: {e}")
                break
        unique_markets = []
        for m in markets:
            if m.condition_id and m.condition_id not in self._seen_condition_ids:
                self._seen_condition_ids.add(m.condition_id)
                unique_markets.append(m)
            elif m.condition_id:
                self._cache_hits += 1
        markets = unique_markets
        markets = [m for m in markets if m.accepting_orders]
        markets.sort(key=lambda x: x.sweep_score, reverse=True)
        self._total_discovered += len(markets)
        logger.info(f"[DISCOVERY] Found {len(markets)} active candidate markets (accepting orders only)")
        return markets

    def discover_closed_markets(self, max_markets=50):
        """SECTION 6 AUDIT: Discover closed/resolved markets for sweeping."""
        markets = []
        cache_key = f"closed_{max_markets}_0"
        resp = self._fetch_with_retry(
            f"{GAMMA_API}/markets?limit={max_markets}&order=volume24hr&ascending=false&closed=true&active=false",
            use_cache=True, cache_key=cache_key
        )
        if resp and resp.status_code == 200:
            try:
                data = resp.json()
                for m in data:
                    try:
                        prices = m.get("outcomePrices", "[\"0.5\",\"0.5\"]")
                        if isinstance(prices, str):
                            prices = json.loads(prices)
                        yes_price = float(prices[0])
                        no_price = float(prices[1]) if len(prices) > 1 else 1.0 - yes_price
                        neg_risk = m.get("negRisk", False)
                        tokens = m.get("clobTokenIds", ["", ""])
                        if isinstance(tokens, str):
                            tokens = json.loads(tokens)
                        category = detect_category(m.get("question", ""), m.get("tags", []))
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
                            accepting_orders=m.get("acceptingOrders", False),
                            sweep_score=self._compute_closed_score(yes_price, no_price, m),
                            category=category,
                            tick_size=float(m.get("orderPriceMinTickSize", 0.01)),
                            min_order_size=float(m.get("orderMinSize", 5)),
                            raw=m,
                        ))
                    except Exception as e:
                        logger.debug(f"Parse error for closed market: {e}")
                        continue
            except Exception as e:
                self._total_api_errors += 1
                logger.error(f"Gamma API parse error (closed): {e}")
        elif resp:
            self._total_api_errors += 1
            logger.error(f"Gamma API returned {resp.status_code} for closed markets")
        else:
            self._total_api_errors += 1
            logger.error("Gamma API returned no response for closed markets")
        unique_markets = []
        for m in markets:
            if m.condition_id and m.condition_id not in self._seen_condition_ids:
                self._seen_condition_ids.add(m.condition_id)
                unique_markets.append(m)
            elif m.condition_id:
                self._cache_hits += 1
        markets = unique_markets
        markets.sort(key=lambda x: x.sweep_score, reverse=True)
        self._total_discovered += len(markets)
        logger.info(f"[DISCOVERY] Found {len(markets)} closed/resolved markets")
        return markets

    def discover_all_markets(self, max_active=200, max_closed=50):
        """SECTION 6 AUDIT: Discover both active and closed markets."""
        active = self.discover_candidates(max_active)
        closed = self.discover_closed_markets(max_closed)
        logger.info(f"[DISCOVERY] Total: {len(active)} active + {len(closed)} closed = {len(active)+len(closed)} markets")
        return active, closed

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

    def _compute_closed_score(self, yes_price, no_price, raw_market):
        """SECTION 6 AUDIT: Score closed/resolved markets for sweeping priority."""
        max_price = max(yes_price, no_price)
        score = 0.0
        if max_price >= 0.99:
            score += 60
        elif max_price >= 0.95:
            score += 40
        elif max_price >= 0.90:
            score += 20
        uma_statuses = raw_market.get("umaResolutionStatuses", [])
        if isinstance(uma_statuses, str):
            try:
                uma_statuses = json.loads(uma_statuses)
            except Exception:
                uma_statuses = []
        if isinstance(uma_statuses, list):
            if not uma_statuses:
                score += 20
            elif any("dispute" in str(s).lower() for s in uma_statuses):
                score -= 50
            elif any("proposed" in str(s).lower() for s in uma_statuses):
                score += 5
        volume = float(raw_market.get("volume24hr", 0))
        if volume > 10000: score += 10
        elif volume > 5000: score += 5
        closed_time = raw_market.get("closedTime")
        if closed_time:
            try:
                from datetime import datetime, timezone
                ct = datetime.fromisoformat(str(closed_time).replace("Z", "+00:00"))
                hours_since = (datetime.now(timezone.utc) - ct).total_seconds() / 3600
                if hours_since > 2:
                    score += 10
                elif hours_since > 1:
                    score += 5
            except Exception: pass
        return score

    def get_market_book(self, token_id):
        """SECTION 6 AUDIT: Get market order book with caching."""
        if token_id in self._book_cache:
            cached = self._book_cache[token_id]
            if time.time() - cached['time'] < self._book_cache_ttl:
                return cached['book']
        from config.settings import CLOB_API
        try:
            url = f"{CLOB_API}/book?token_id={token_id}"
            resp = self._session.get(url, timeout=5)
            if resp.status_code == 200:
                book = resp.json()
                self._book_cache[token_id] = {'book': book, 'time': time.time()}
                return book
        except Exception as e:
            logger.debug(f"Book fetch error: {e}")
        return {"asks": [], "bids": []}

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

    def get_discovery_status(self) -> dict:
        """AUDIT FIX #22 + SECTION 6: Return discovery status for monitoring."""
        return {
            'total_discovered': self._total_discovered,
            'total_api_errors': self._total_api_errors,
            'cache_hits': self._cache_hits,
            'cache_size': len(self._cache),
            'book_cache_size': len(self._book_cache),
            'seen_markets': len(self._seen_condition_ids),
            'cache_ttl': self._cache_ttl,
            'book_cache_ttl': self._book_cache_ttl,
            'max_retries': self._max_retries,
            'min_request_interval': self._min_request_interval,
        }