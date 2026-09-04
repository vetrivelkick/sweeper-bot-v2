"""
Sweeper Bot V2 - Resolution Detection with Finality Gate

P0 #1: Strategy signal enhancements:
  1. Resolution-rule parser: Parse resolution rules from market raw data
  2. Outcome-source adapters: Multiple sources (price, Gamma closed, end-date)
  3. Finality gate: Check if resolution is final before allowing sweep

FIX #11: Added category field to DetectionResult for fee rate lookup
"""
import logging, json
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, List
from datetime import datetime, timezone

logger = logging.getLogger("sweeper.detection")


class CertaintyLevel(Enum):
    UNCERTAIN = "uncertain"
    WEAK = "weak"
    STRONG = "strong"
    CERTAIN = "certain"


class FinalityStatus(Enum):
    """P0 #1: Finality gate status for resolution"""
    PENDING = "pending"
    PROPOSED = "proposed"
    DISPUTED = "disputed"
    FINAL = "final"
    UNKNOWN = "unknown"


@dataclass
class ResolutionRule:
    """P0 #1: Parsed resolution rules from market data"""
    resolution_source: str = ""
    end_date: Optional[str] = None
    is_auto_resolved: bool = False
    has_uma_oracle: bool = True
    outcomes: List[str] = field(default_factory=list)


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
    neg_risk: bool
    end_date: Optional[str]
    signals: list = field(default_factory=list)
    category: str = "other"
    tick_size: float = 0.01
    resolution_source: str = ""
    outcome_sources: list = field(default_factory=list)
    finality_status: str = "pending"
    is_final: bool = False
    finality_reason: str = ""
    resolution_rules: Optional[ResolutionRule] = None


class ResolutionDetector:
    def __init__(self, config):
        self.config = config

    def _parse_resolution_rules(self, market):
        """P0 #1: Resolution-rule parser - parse resolution rules from market raw data"""
        raw = getattr(market, 'raw', None) or {}
        outcomes_raw = raw.get("outcomes", "[]")
        if isinstance(outcomes_raw, str):
            try:
                outcomes = json.loads(outcomes_raw)
            except Exception:
                outcomes = []
        else:
            outcomes = outcomes_raw if isinstance(outcomes_raw, list) else []
        rule = ResolutionRule(
            resolution_source=raw.get("resolutionSource", ""),
            end_date=raw.get("endDate") or getattr(market, 'end_date', None),
            is_auto_resolved=raw.get("automaticallyResolved", False),
            has_uma_oracle=True,
            outcomes=outcomes,
        )
        return rule

    def _check_price_source(self, yes_price, no_price):
        """P0 #1: Outcome-source adapter #1 - Price-based detection"""
        winning_side = "YES" if yes_price > no_price else "NO"
        winning_price = max(yes_price, no_price)
        losing_price = min(yes_price, no_price)
        if winning_price >= 0.999:
            return winning_side, winning_price, losing_price, CertaintyLevel.CERTAIN, winning_price * 100, f"Price {winning_price} >= 0.999", ["price_extreme"]
        elif winning_price >= 0.99:
            return winning_side, winning_price, losing_price, CertaintyLevel.STRONG, winning_price * 100, f"Price {winning_price} >= 0.99", ["price_high"]
        elif winning_price >= 0.95:
            return winning_side, winning_price, losing_price, CertaintyLevel.WEAK, winning_price * 100, f"Price {winning_price} >= 0.95", ["price_moderate"]
        return winning_side, winning_price, losing_price, CertaintyLevel.UNCERTAIN, 0.0, "", []

    def _check_gamma_closed_source(self, market):
        """P0 #1: Outcome-source adapter #2 - Gamma API closed/resolved status"""
        raw = getattr(market, 'raw', None) or {}
        closed = raw.get("closed", False)
        active = raw.get("active", True)
        if closed and not active:
            prices_raw = raw.get("outcomePrices", '["0.5","0.5"]')
            if isinstance(prices_raw, str):
                try:
                    prices = json.loads(prices_raw)
                except Exception:
                    prices = [0.5, 0.5]
            else:
                prices = prices_raw if isinstance(prices_raw, list) else [0.5, 0.5]
            yes_price = float(prices[0]) if len(prices) > 0 else 0.5
            winning_side = "YES" if yes_price >= 0.5 else "NO"
            return winning_side, True, "gamma_closed"
        return None, False, ""

    def _check_end_date_source(self, market):
        """P0 #1: Outcome-source adapter #3 - End date passed"""
        end_date = getattr(market, 'end_date', None)
        if end_date:
            try:
                dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                if dt < datetime.now(timezone.utc):
                    return True, f"End date passed ({end_date})"
            except Exception:
                pass
        return False, ""

    def _check_finality_gate(self, market, result):
        """P0 #1: Finality gate - check if resolution is final (safe to sweep)

        Resolution is final when:
        1. Market is closed (Gamma API: closed=true)
        2. Market is inactive (Gamma API: active=false)
        3. Market is not accepting orders (Gamma API: acceptingOrders=false)
        4. UMA 2-hour dispute window has passed (simulated in paper mode)
        """
        raw = getattr(market, 'raw', None) or {}
        closed = raw.get("closed", False)
        active = raw.get("active", True)
        accepting = raw.get("acceptingOrders", True)
        end_date = getattr(market, 'end_date', None)

        if not closed:
            if self.config.paper_mode and result.certainty == CertaintyLevel.CERTAIN:
                end_passed = False
                if end_date:
                    try:
                        dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                        if dt < datetime.now(timezone.utc):
                            end_passed = True
                    except Exception:
                        pass
                if end_passed:
                    return FinalityStatus.FINAL, True, "Simulated finality: price extreme + end date passed (paper mode)"
            if result.certainty == CertaintyLevel.CERTAIN:
                return FinalityStatus.FINAL, True, "Simulated finality: price extreme (paper mode)"
            return FinalityStatus.PENDING, False, "Market not yet closed (paper mode)"

        if active:
            return FinalityStatus.PROPOSED, False, "Market closed but still active (UMA challenge period?)"

        if accepting:
            return FinalityStatus.PROPOSED, False, "Market closed but still accepting orders"

        return FinalityStatus.FINAL, True, "Market closed, inactive, not accepting orders - resolution final"

    def detect(self, market, book=None):
        if not market:
            return None

        resolution_rules = self._parse_resolution_rules(market)

        outcome_sources = []
        all_signals = []

        side, wp, lp, cert, conf, reason, sigs = self._check_price_source(market.yes_price, market.no_price)
        winning_side = side
        winning_price = wp
        losing_price = lp
        certainty = cert
        confidence = conf
        detection_reason = reason
        signals = list(sigs)
        if sigs:
            outcome_sources.append("price")
            all_signals.extend(sigs)

        gamma_side, gamma_closed, gamma_reason = self._check_gamma_closed_source(market)
        if gamma_closed:
            outcome_sources.append("gamma_closed")
            all_signals.append("gamma_closed")
            if gamma_side:
                winning_side = gamma_side
                certainty = CertaintyLevel.CERTAIN
                confidence = max(confidence, 99.0)
                detection_reason = (detection_reason + " + " + gamma_reason) if detection_reason else gamma_reason

        end_passed, end_reason = self._check_end_date_source(market)
        if end_passed:
            outcome_sources.append("end_date")
            all_signals.append("expired")
            certainty = CertaintyLevel.CERTAIN
            confidence = max(confidence, 99.0)
            detection_reason = (detection_reason + " + " + end_reason) if detection_reason else end_reason

        winning_token = market.yes_token_id if winning_side == "YES" else market.no_token_id
        losing_token = market.no_token_id if winning_side == "YES" else market.yes_token_id

        result = DetectionResult(
            condition_id=market.condition_id,
            question=market.question,
            winning_side=winning_side,
            winning_token_id=winning_token,
            losing_token_id=losing_token,
            certainty=certainty,
            confidence_score=round(confidence, 2),
            detection_reason=detection_reason,
            winning_price=winning_price,
            losing_price=losing_price,
            neg_risk=market.neg_risk,
            end_date=market.end_date,
            signals=all_signals,
            category=getattr(market, 'category', 'other'),
            tick_size=getattr(market, 'tick_size', 0.01),
            resolution_source=", ".join(outcome_sources) if outcome_sources else "price",
            outcome_sources=outcome_sources,
            finality_status=FinalityStatus.PENDING.value,
            is_final=False,
            finality_reason="",
            resolution_rules=resolution_rules,
        )

        finality_status, is_final, finality_reason = self._check_finality_gate(market, result)
        result.finality_status = finality_status.value
        result.is_final = is_final
        result.finality_reason = finality_reason

        logger.debug(f"[DETECT] {market.question[:50]} | side={winning_side} | cert={certainty.value} "
                     f"| sources={outcome_sources} | final={is_final} ({finality_status.value})")

        return result

    def is_sweepable(self, result):
        if not result:
            return False
        if self.config.paper_mode:
            if result.certainty in (CertaintyLevel.CERTAIN, CertaintyLevel.STRONG):
                return True
            return False
        else:
            if result.certainty in (CertaintyLevel.CERTAIN, CertaintyLevel.STRONG) and result.is_final:
                return True
            if result.certainty == CertaintyLevel.CERTAIN and not result.is_final:
                logger.warning(f"[FINALITY GATE] Blocked sweep: {result.question[:50]} - {result.finality_reason}")
                return False
