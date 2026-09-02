"""Sweeper Bot V2 - Market Discovery Module"""
import json
import time
import logging
import requests
from datetime import datetime, timezone
from typing import Optional
from dataclasses import dataclass, field

logger = logging.getLogger("sweeper.discovery")
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

@dataclass
class CandidateMarket:
    condition_id: str
    question: str
    slug: str
    yes_token_id: str
    no_token_id: str
    yes_price: float
    no_price: float
    end_date: str
    volume_24hr: float
    liquidity: float
    neg_risk: bool
    accepting_orders: bool
    sweep_score: float = 0.0
    raw: dict = field(default_factory=dict)

class MarketDiscovery:
    def __init__(self, config):
        self.config = config

    def fetch_active_markets(self, limit=100, offset=0):
        url = f"{GAMMA_API}/markets"
        params = {"active": "true", "closed": "false", "limit": limit, "offset": offset, "order": "volume24hr", "ascending": "false"}
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        return resp.json()

    def parse_market(self, raw):
        try:
            condition_id = raw.get("conditionId", raw.get("condition_id", ""))
            if not condition_id: return None
            clob_ids = raw.get("clobTokenIds", "[]")
            if isinstance(clob_ids, str): clob_ids = json.loads(clob_ids)
            if len(clob_ids) < 2: return None
            outcomes = raw.get("outcomes", '["Yes", "No"]')
            if isinstance(outcomes, str): outcomes = json.loads(outcomes)
            prices = raw.get("outcomePrices", raw.get("outcome_prices", "[]"))
            if isinstance(prices, str): prices = json.loads(prices)
            if len(prices) < 2: prices = [0.5, 0.5]
            yes_price = float(prices[0]) if outcomes[0].lower() == "yes" else float(prices[1])
            no_price = float(prices[1]) if outcomes[0].lower() == "yes" else float(prices[0])
            yes_idx = 0 if outcomes[0].lower() == "yes" else 1
            no_idx = 1 - yes_idx
            return CandidateMarket(condition_id=condition_id, question=raw.get("question", ""),
                slug=raw.get("slug", ""), yes_token_id=clob_ids[yes_idx], no_token_id=clob_ids[no_idx],
                yes_price=yes_price, no_price=no_price, end_date=raw.get("endDate", raw.get("end_date", "")),
                volume_24hr=float(raw.get("volume24hr", 0) or 0), liquidity=float(raw.get("liquidity", 0) or 0),
                neg_risk=bool(raw.get("negRisk", False)), accepting_orders=bool(raw.get("acceptingOrders", True)), raw=raw)
        except Exception as e:
            logger.debug(f"Parse error: {e}")
            return None

    def score_market(self, market):
        score = 0.0
        max_price = max(market.yes_price, market.no_price)
        if max_price >= 0.999: score += 50.0
        elif max_price >= 0.99: score += 30.0
        elif max_price >= 0.95: score += 10.0
        score += min(market.volume_24hr / 10000, 20)
        score += min(market.liquidity / 10000, 5)
        if market.accepting_orders: score += 5.0
        return round(score, 4)

    def discover_candidates(self, max_markets=200):
        all_markets = []
        offset = 0
        while len(all_markets) < max_markets:
            batch = self.fetch_active_markets(limit=100, offset=offset)
            if not batch: break
            for raw in batch:
                market = self.parse_market(raw)
                if market:
                    market.sweep_score = self.score_market(market)
                    all_markets.append(market)
            offset += 100
            if len(batch) < 100: break
        all_markets.sort(key=lambda m: m.sweep_score, reverse=True)
        return all_markets[:max_markets]

    def get_market_book(self, token_id):
        url = f"{CLOB_API}/book"
        resp = requests.get(url, params={"token_id": token_id}, timeout=10)
        resp.raise_for_status()
        return resp.json()

    def find_sweepable_asks(self, book, min_price=0.99):
        asks = book.get("asks", [])
        return [a for a in asks if float(a.get("price", 0)) >= min_price]
