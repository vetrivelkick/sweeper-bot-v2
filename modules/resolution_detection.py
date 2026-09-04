"""
Sweeper Bot V2 - Resolution Detection

FIX #11: Added category field to DetectionResult for fee rate lookup
"""
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
from datetime import datetime, timezone

logger = logging.getLogger("sweeper.detection")

class CertaintyLevel(Enum):
    UNCERTAIN = "uncertain"
    WEAK = "weak"
    STRONG = "strong"
    CERTAIN = "certain"

@dataclass
class DetectionResult:
    condition_id: str
    question: str
    winning_side: str  # "YES" or "NO"
    winning_token_id: str
    losing_token_id: str
    certainty: CertaintyLevel
    confidence_score: float
    detection_reason: str
    winning_price: float
    losing_price: float
    neg_risk: bool
    end_date: Optional[str]
    signals: list = field(default_factory=list)
    category: str = "other"  # FIX #11: Added category field
    tick_size: float = 0.01

class ResolutionDetector:
    def __init__(self, config):
        self.config = config

    def detect(self, market, book=None):
        if not market: return None
        yes_price = market.yes_price
        no_price = market.no_price
        winning_side = "YES" if yes_price > no_price else "NO"
        winning_price = max(yes_price, no_price)
        losing_price = min(yes_price, no_price)
        winning_token = market.yes_token_id if winning_side == "YES" else market.no_token_id
        losing_token = market.no_token_id if winning_side == "YES" else market.yes_token_id
        certainty = CertaintyLevel.UNCERTAIN
        confidence = 0.0
        reason = ""
        signals = []
        if winning_price >= 0.999:
            certainty = CertaintyLevel.CERTAIN; confidence = winning_price * 100; reason = f"Price {winning_price} >= 0.999"; signals.append("price_extreme")
        elif winning_price >= 0.99:
            certainty = CertaintyLevel.STRONG; confidence = winning_price * 100; reason = f"Price {winning_price} >= 0.99"; signals.append("price_high")
        elif winning_price >= 0.95:
            certainty = CertaintyLevel.WEAK; confidence = winning_price * 100; reason = f"Price {winning_price} >= 0.95"; signals.append("price_moderate")
        if market.end_date:
            try:
                dt = datetime.fromisoformat(market.end_date.replace("Z", "+00:00"))
                if dt < datetime.now(timezone.utc):
                    certainty = CertaintyLevel.CERTAIN; confidence = max(confidence, 99.0); reason = f"End date passed ({market.end_date})"; signals.append("expired")
            except Exception: pass
        return DetectionResult(
            condition_id=market.condition_id, question=market.question,
            winning_side=winning_side, winning_token_id=winning_token, losing_token_id=losing_token,
            certainty=certainty, confidence_score=round(confidence, 2), detection_reason=reason,
            winning_price=winning_price, losing_price=losing_price, neg_risk=market.neg_risk,
            end_date=market.end_date, signals=signals,
            category=getattr(market, 'category', 'other'),  # FIX #11
            tick_size=getattr(market, 'tick_size', 0.01),
        )

    def is_sweepable(self, result):
        if not result: return False
        if result.certainty in (CertaintyLevel.CERTAIN, CertaintyLevel.STRONG):
            return True
        return False
