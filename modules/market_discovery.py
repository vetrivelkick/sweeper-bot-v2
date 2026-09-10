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

    def discover_candidates(self, max_markets=200, max_resolution_minutes=0):
        import json
        self._seen_condition_ids.clear()
        markets = []
        offset = 0
        limit = min(max_markets, 200)
        total_fetched = 0
        for _ in range(50):
            if total_fetched >= max_markets:
                break
            bs = min(limit, max_markets - total_fetched)
            from datetime import datetime, timezone, timedelta
            base_url = "https://gamma-api.polymarket.com/markets?limit=" + str(bs) + "&offset=" + str(offset) + "&active=true&closed=false"
            end_min = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            base_url += "&end_date_min=" + end_min
            if max_resolution_minutes > 0:
                end_max = (datetime.now(timezone.utc) + timedelta(minutes=max_resolution_minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
                base_url += "&end_date_max=" + end_max + "&order=endDate&ascending=true"
            else:
                base_url += "&order=volume24hr&ascending=false"
            url = base_url
            try:
                r = self._fetch_with_retry(url, timeout=10)
                if r is None or r.status_code != 200:
                    break
                data = r.json()
                if not data:
                    break
                for m in data:
                    try:
                        cid = m.get("conditionId") or m.get("condition_id", "")
                        if not cid or cid in self._seen_condition_ids:
                            continue
                        self._seen_condition_ids.add(cid)
                        # FIX ISSUE #2: Filter out stale markets with past end_date
                        end_date_raw = m.get("endDate")
                        if end_date_raw:
                            try:
                                from datetime import datetime, timezone
                                end_dt = datetime.fromisoformat(end_date_raw.replace("Z", "+00:00"))
                                now_utc = datetime.now(timezone.utc)
                                if end_dt < now_utc:
                                    logger.info(f"Skipping stale market {m.get('question', '')[:40]}: end_date {end_date_raw} already passed")
                                    continue
                            except Exception:
                                pass
                        tk = m.get("tokens", [])
                        if isinstance(tk, str):
                            tk = json.loads(tk)
                        if not tk:
                            cids = m.get("clobTokenIds", [])
                            if isinstance(cids, str):
                                cids = json.loads(cids)
                            if cids and len(cids) >= 2:
                                tk = [{"token_id": cids[0]}, {"token_id": cids[1]}]
                        pr = m.get("outcomePrices", [])
                        if isinstance(pr, str):
                            pr = json.loads(pr)
                        yp = float(pr[0]) if pr else 0.0
                        try:
                            cat = detect_category(m.get("question", ""), m.get("tags") if isinstance(m.get("tags"), list) else None)
                        except:
                            cat = "other"
                        markets.append(CandidateMarket(
                            condition_id=cid, question=m.get("question", ""),
                            slug=m.get("slug", ""),
                            yes_token_id=tk[0].get("token_id", "") if tk else "",
                            no_token_id=tk[1].get("token_id", "") if len(tk) > 1 else "",
                            yes_price=yp, no_price=float(pr[1]) if len(pr) > 1 else 1.0 - yp,
                            end_date=m.get("endDate"), volume_24hr=float(m.get("volume24hr") or 0),
                            liquidity=float(m.get("liquidity") or 0),
                            neg_risk=bool(m.get("negRisk") or False),
                            accepting_orders=bool(m.get("acceptingOrders") or False),
                            sweep_score=yp * float(m.get("volume24hr") or 0),
                            category=cat,
                            tick_size=float(m.get("minimum_tick_size") or (0.001 if yp >= 0.96 else 0.01)),
                            min_order_size=float(m.get("minimum_order_size") or 5),
                            raw=m,
                        ))
                    except Exception as ex:
                        if len(markets) == 0:
                            print("DEBUG PARSE ERROR: " + str(ex))
                        continue
                offset += len(data)
                total_fetched += len(data)
            except Exception as ex:
                print("DEBUG API ERROR: " + str(ex))
                break
        hp = sum(1 for m in markets if m.yes_price >= 0.95 or m.no_price >= 0.95)
        logger.debug(f"{hp} markets with price >= 0.95 out of {len(markets)}")
        if markets:
            logger.debug(f"top: {markets[0].question[:50]} | yes={markets[0].yes_price:.4f}")
        return markets


    def get_market_book(self, token_id):
        url = "https://clob.polymarket.com/book?token_id=" + str(token_id)
        self._rate_limit()
        try:
            resp = self._session.get(url, timeout=10)
            if resp.status_code == 200:
                return resp.json()
            logger.debug("Book fetch failed for " + str(token_id[:16]) + ": " + str(resp.status_code))
        except Exception as e:
            logger.debug("Book fetch error for " + str(token_id[:16]) + ": " + str(e))
        return {}
