"""Sweeper Bot V2 - Resolution Detection Module"""
import time, logging
from dataclasses import dataclass
from typing import Optional
from enum import Enum
from datetime import datetime, timezone

logger = logging.getLogger("sweeper.detection")

class CertaintyLevel(Enum):
    NONE = 0; WEAK = 1; STRONG = 2; CERTAIN = 3

@dataclass
class DetectionResult:
    condition_id: str
    question: str
    winning_side: str
    winning_token_id: str
    losing_token_id: str
    certainty: CertaintyLevel
    confidence_score: float
    detection_reason: str
    winning_price: float
    losing_price: float
    neg_risk: bool = False
    end_date: str = ""
    signals: int = 1

class ResolutionDetector:
    def __init__(self, config):
        self.config = config
        self._price_history = {}

    def detect_by_price(self, market):
        yes_price = market.yes_price; no_price = market.no_price
        if yes_price >= no_price:
            winning_side, winning_price, losing_price = "YES", yes_price, no_price
            winning_token_id, losing_token_id = market.yes_token_id, market.no_token_id
        else:
            winning_side, winning_price, losing_price = "NO", no_price, yes_price
            winning_token_id, losing_token_id = market.no_token_id, market.yes_token_id
        if winning_price >= 0.999:
            certainty = CertaintyLevel.CERTAIN; reason = f"Price {winning_price} >= 0.999"
        elif winning_price >= 0.99:
            certainty = CertaintyLevel.STRONG; reason = f"Price {winning_price} >= 0.99"
        elif winning_price >= 0.95:
            certainty = CertaintyLevel.WEAK; reason = f"Price {winning_price} >= 0.95"
        else: return None
        return DetectionResult(condition_id=market.condition_id, question=market.question,
            winning_side=winning_side, winning_token_id=winning_token_id, losing_token_id=losing_token_id,
            certainty=certainty, confidence_score=min(winning_price*100,100), detection_reason=reason,
            winning_price=winning_price, losing_price=losing_price, neg_risk=market.neg_risk, end_date=market.end_date)

    def detect_by_end_date(self, market):
        if not market.end_date: return None
        try:
            end_dt = datetime.fromisoformat(market.end_date.replace("Z", "+00:00"))
            if datetime.now(timezone.utc) > end_dt:
                price_det = self.detect_by_price(market)
                if price_det:
                    price_det.certainty = CertaintyLevel.CERTAIN
                    price_det.detection_reason = f"End date passed ({market.end_date})"
                    return price_det
        except Exception: pass
        return None

    def detect_by_spread(self, market, book):
        asks = book.get("asks", []); bids = book.get("bids", [])
        if not asks or not bids: return None
        best_ask = max(float(a.get("price", 0)) for a in asks)
        best_bid = max(float(b.get("price", 0)) for b in bids)
        if best_ask - best_bid < 0.002 and best_ask >= 0.99:
            price_det = self.detect_by_price(market)
            if price_det:
                price_det.signals += 1
                price_det.detection_reason += " (confirmed by spread collapse)"
                return price_det
        return None

    def detect(self, market, book=None):
        results = []
        price_det = self.detect_by_price(market)
        if price_det: results.append(price_det)
        end_det = self.detect_by_end_date(market)
        if end_det: results.append(end_det)
        if book:
            spread_det = self.detect_by_spread(market, book)
            if spread_det: results.append(spread_det)
        if not results: return None
        best = max(results, key=lambda r: r.certainty.value)
        best.signals = len(results)
        return best

    def is_sweepable(self, result):
        return result.certainty.value >= CertaintyLevel.STRONG.value
