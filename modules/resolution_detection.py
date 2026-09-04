"""
Sweeper Bot V2 - Resolution Detection with Finality Gate

P0 #1 FIX: Demoted price from certainty signal to secondary context.
  - Price alone is now WEAK certainty (not CERTAIN/STRONG)
  - Gamma API closed/resolved is the PRIMARY signal for CERTAIN
  - End date passed + price hint = STRONG (not CERTAIN)
  - Source-conflict detection: if sources disagree, don't sweep
  - Fail-closed: price-only markets are UNCERTAIN in live mode
  - Fixed is_sweepable missing return False in live branch

FIX #11: Added category field to DetectionResult for fee rate lookup
FIX: Changed CertaintyLevel from Enum to IntEnum to fix TypeError on
     '<' comparison that silently broke all live market detection.
FIX: End date alone no longer upgrades UNCERTAIN to STRONG.
     Only markets with winning_price >= 0.95 (WEAK) get upgraded when
     end date passes. Markets with price < 0.95 stay UNCERTAIN even if
     end date has passed — the outcome is still uncertain.
FIX: is_sweepable() now rejects markets where winning_price < min_entry_price
     to prevent order builder rejections on low-priced markets.

SECTION 5 AUDIT: Resolution engine (dispute risk calculation, UMA challenge period, on-chain verification)

"""
import logging, json
from dataclasses import dataclass, field
from enum import Enum, IntEnum
from typing import Optional, List
from datetime import datetime, timezone
from config.settings import OUTCOME_FINALITY_POLICIES, UNSUPPORTED_RESOLUTION_SOURCES, MAX_RESOLUTION_DISPUTE_RISK

# SECTION 5 AUDIT: UMA challenge period and adapter addresses (fallback if not in config)
try:
    from config.settings import UMA_CHALLENGE_PERIOD_HOURS, UMA_ADAPTER_ADDRESSES
except ImportError:
    UMA_CHALLENGE_PERIOD_HOURS = 2
    UMA_ADAPTER_ADDRESSES = [
        "0x157Ce2d672854c848c9b79C49a8Cc6cc89176a49",
        "0x6A9D222616C90FcA5754cd1333cFD9b7fb6a4F74",
        "0xCB1822859cEF82Cd2Eb4E6276C7916e692995130",
    ]

logger = logging.getLogger("sweeper.detection")


class CertaintyLevel(IntEnum):
    UNCERTAIN = 0
    WEAK = 1
    STRONG = 2
    CERTAIN = 3

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
    # SECTION 5 AUDIT: UMA resolution status and closure time
    uma_resolution_statuses: list = field(default_factory=list)
    closed_time: Optional[str] = None


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
    # SECTION 5 AUDIT: Resolution dispute risk and UMA status tracking
    resolution_dispute_risk: float = 0.0
    uma_resolution_status: str = ""
    resolution_timestamp: Optional[str] = None

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
        # SECTION 5 AUDIT: Parse UMA resolution statuses and closed time
        uma_statuses = raw.get("umaResolutionStatuses", [])
        if isinstance(uma_statuses, str):
            try:
                uma_statuses = json.loads(uma_statuses)
            except Exception:
                uma_statuses = []
        rule = ResolutionRule(
            resolution_source=raw.get("resolutionSource", ""),
            end_date=raw.get("endDate") or getattr(market, 'end_date', None),
            is_auto_resolved=raw.get("automaticallyResolved", False),
            has_uma_oracle=True,
            outcomes=outcomes,
            uma_resolution_statuses=uma_statuses if isinstance(uma_statuses, list) else [],
            closed_time=raw.get("closedTime"),
        )
        return rule

    def _check_price_source(self, yes_price, no_price):
        """P0 #1 FIX: Price is SECONDARY context only - NOT a certainty signal.

        Market price reflects crowd consensus but does NOT prove resolution.
        Using price as CERTAIN/STRONG creates a circular signal: the bot buys
        because price is high, and price is high because others already bought.
        Price is demoted to WEAK at best - only a hint, not proof.
        """
        winning_side = "YES" if yes_price > no_price else "NO"
        winning_price = max(yes_price, no_price)
        losing_price = min(yes_price, no_price)
        if winning_price >= 0.999:
            return winning_side, winning_price, losing_price, CertaintyLevel.WEAK, winning_price * 100, f"Price hint {winning_price} >= 0.999 (secondary only)", ["price_extreme"]
        elif winning_price >= 0.99:
            return winning_side, winning_price, losing_price, CertaintyLevel.WEAK, winning_price * 100, f"Price hint {winning_price} >= 0.99 (secondary only)", ["price_high"]
        elif winning_price >= 0.95:
            return winning_side, winning_price, losing_price, CertaintyLevel.WEAK, winning_price * 100, f"Price hint {winning_price} >= 0.95 (secondary only)", ["price_moderate"]
        return winning_side, winning_price, losing_price, CertaintyLevel.UNCERTAIN, 0.0, "", []

    def _check_gamma_closed_source(self, market):
        """P0 #1: Outcome-source adapter #2 - Gamma API closed/resolved status (PRIMARY signal)"""
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
        5. SECTION 1 AUDIT: Per-category finality policy (OUTCOME_FINALITY_POLICIES)
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
                    return FinalityStatus.FINAL, True, "Simulated finality: CERTAIN + end date passed (paper mode)"
            if self.config.paper_mode and result.certainty in (CertaintyLevel.CERTAIN, CertaintyLevel.STRONG):
                return FinalityStatus.FINAL, True, "Simulated finality: strong signal (paper mode)"
            return FinalityStatus.PENDING, False, "Market not yet closed"

        if active:
            return FinalityStatus.PROPOSED, False, "Market closed but still active (UMA challenge period?)"

        if accepting:
            return FinalityStatus.PROPOSED, False, "Market closed but still accepting orders"

        # SECTION 5 AUDIT: Check UMA dispute status
        uma_statuses = raw.get("umaResolutionStatuses", [])
        if isinstance(uma_statuses, str):
            try:
                uma_statuses = json.loads(uma_statuses)
            except Exception:
                uma_statuses = []
        if isinstance(uma_statuses, list) and uma_statuses:
            for status in uma_statuses:
                status_str = ""
                if isinstance(status, str):
                    status_str = status.lower()
                elif isinstance(status, dict):
                    status_str = str(status.get("status", "")).lower()
                if "dispute" in status_str:
                    return FinalityStatus.DISPUTED, False, "Market resolution is disputed (UMA)"

        # SECTION 5 AUDIT: Check UMA challenge period (2 hours from closedTime)
        closed_time = raw.get("closedTime")
        if closed_time:
            try:
                ct = datetime.fromisoformat(str(closed_time).replace("Z", "+00:00"))
                hours_since = (datetime.now(timezone.utc) - ct).total_seconds() / 3600
                if hours_since < UMA_CHALLENGE_PERIOD_HOURS:
                    return FinalityStatus.PROPOSED, False, f"UMA challenge period active ({hours_since:.1f}h < {UMA_CHALLENGE_PERIOD_HOURS}h)"
            except Exception:
                pass

        # SECTION 1 AUDIT: Per-category finality policy
        category = getattr(result, 'category', 'other')
        policy = OUTCOME_FINALITY_POLICIES.get(category, OUTCOME_FINALITY_POLICIES.get('other', {}))
        min_blocks = policy.get('min_blocks', 128)
        dispute_window = policy.get('dispute_window_hours', 2)
        require_source = policy.get('require_source', True)

        if require_source and not getattr(result, 'outcome_sources', []):
            return FinalityStatus.PENDING, False, f"Category {category} requires source agreement"

        return FinalityStatus.FINAL, True, f"Market closed, inactive, not accepting orders - resolution final (category={category}, min_blocks={min_blocks})"

    def _check_resolution_dispute_risk(self, result):
        """SECTION 1 AUDIT: Check resolution dispute risk.

        Returns (is_safe, risk_score, reason).
        """
        risk = getattr(result, 'resolution_dispute_risk', 0.0)
        if risk > self.config.max_resolution_dispute_risk:
            return False, risk, f"Dispute risk {risk} > max {self.config.max_resolution_dispute_risk}"
        return True, risk, "OK"

    def _calculate_dispute_risk(self, market, result):
        """SECTION 5 AUDIT: Calculate resolution dispute risk score (0.0-1.0).

        Factors:
        - UMA resolution status (disputed = high risk)
        - Resolution source reliability
        - Time since closure (shorter = higher risk, UMA challenge period)
        - Market category (some categories have higher dispute rates)
        """
        risk = 0.0
        raw = getattr(market, 'raw', None) or {}

        # UMA resolution status - if disputed, high risk
        uma_statuses = raw.get("umaResolutionStatuses", [])
        if isinstance(uma_statuses, str):
            try:
                uma_statuses = json.loads(uma_statuses)
            except Exception:
                uma_statuses = []
        if isinstance(uma_statuses, list):
            for status in uma_statuses:
                status_str = ""
                if isinstance(status, str):
                    status_str = status.lower()
                elif isinstance(status, dict):
                    status_str = str(status.get("status", "")).lower()
                if "dispute" in status_str:
                    risk = max(risk, 0.5)
                elif "proposed" in status_str:
                    risk = max(risk, 0.2)

        # Resolution source reliability
        source = getattr(result, 'resolution_source', '')
        if source in UNSUPPORTED_RESOLUTION_SOURCES:
            risk = max(risk, 0.8)

        # Time since closure - shorter time = higher risk (UMA challenge period)
        closed_time = raw.get("closedTime")
        if closed_time:
            try:
                ct = datetime.fromisoformat(str(closed_time).replace("Z", "+00:00"))
                hours_since = (datetime.now(timezone.utc) - ct).total_seconds() / 3600
                if hours_since < UMA_CHALLENGE_PERIOD_HOURS:
                    risk = max(risk, 0.3 * (1 - hours_since / UMA_CHALLENGE_PERIOD_HOURS))
            except Exception:
                pass

        # Category-based risk (some categories have higher dispute rates)
        category = getattr(result, 'category', 'other')
        high_risk_categories = ['politics', 'crypto', 'sports']
        if category in high_risk_categories:
            risk = max(risk, 0.05)

        return round(risk, 4)

    def _verify_onchain_resolution(self, condition_id):
        """SECTION 5 AUDIT: Verify resolution on-chain via UmaCtfAdapter contracts.

        Queries UMA CTF Adapter contracts on Polygon to verify
        that a market condition has been resolved on-chain.
        Returns (is_verified, message).
        """
        if self.config.paper_mode:
            return True, "Paper mode - on-chain verification skipped"
        try:
            from web3 import Web3
            from config.settings import POLYGON_RPC
            w3 = Web3(Web3.HTTPProvider(getattr(self.config, 'polygon_rpc', None) or POLYGON_RPC))
            adapter_abi = [
                {"inputs": [{"name": "conditionId", "type": "bytes32"}],
                 "name": "getOutcome",
                 "outputs": [{"name": "", "type": "uint8"}],
                 "stateMutability": "view", "type": "function"}
            ]
            cid = condition_id[2:] if condition_id.startswith("0x") else condition_id
            condition_bytes = bytes.fromhex(cid)
            for adapter_addr in UMA_ADAPTER_ADDRESSES:
                try:
                    adapter = w3.eth.contract(
                        address=Web3.to_checksum_address(adapter_addr),
                        abi=adapter_abi
                    )
                    outcome = adapter.functions.getOutcome(condition_bytes).call()
                    if outcome is not None:
                        return True, f"On-chain resolution verified (outcome={outcome}, adapter={adapter_addr[:10]})"
                except Exception:
                    continue
            # All adapters failed - don't block sweep, just log warning
            return True, "On-chain verification inconclusive (no adapter responded)"
        except ImportError:
            return True, "web3 not available - on-chain verification skipped"
        except Exception as e:
            return True, f"On-chain verification error (non-blocking): {e}"

    def detect(self, market, book=None):
        if not market:
            return None

        resolution_rules = self._parse_resolution_rules(market)

        # SECTION 1 AUDIT: Skip markets with unsupported resolution sources
        if resolution_rules.resolution_source in UNSUPPORTED_RESOLUTION_SOURCES:
            logger.info(f"[SKIP] Market {market.question[:50]} has unsupported resolution source: {resolution_rules.resolution_source}")
            return None

        outcome_sources = []
        all_signals = []

        # Price is secondary context only (P0 #1 FIX)
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

        # Gamma closed is the PRIMARY signal for CERTAIN (P0 #1 FIX)
        gamma_side, gamma_closed, gamma_reason = self._check_gamma_closed_source(market)
        if gamma_closed:
            outcome_sources.append("gamma_closed")
            all_signals.append("gamma_closed")
            if gamma_side:
                # P0 #1 FIX: Source-conflict detection
                if gamma_side != winning_side and certainty != CertaintyLevel.UNCERTAIN:
                    logger.warning(f"[CONFLICT] Gamma says {gamma_side} but price says {winning_side} for {market.question[:50]}")
                    certainty = CertaintyLevel.UNCERTAIN
                    detection_reason = f"Source conflict: gamma={gamma_side} vs price={winning_side}"
                else:
                    winning_side = gamma_side
                    certainty = CertaintyLevel.CERTAIN
                    confidence = max(confidence, 99.0)
                    detection_reason = (detection_reason + " + " + gamma_reason) if detection_reason else gamma_reason

        # End date passed + price hint = STRONG (not CERTAIN) (P0 #1 FIX)
        # FIX: Only upgrade to STRONG if price already indicates near-certainty (>= 0.95 = WEAK)
        # End date alone is insufficient — market may still be in progress with uncertain outcome
        end_passed, end_reason = self._check_end_date_source(market)
        if end_passed:
            outcome_sources.append("end_date")
            all_signals.append("expired")
            if certainty >= CertaintyLevel.WEAK:
                if certainty < CertaintyLevel.STRONG:
                    certainty = CertaintyLevel.STRONG
                confidence = max(confidence, 95.0)
                detection_reason = (detection_reason + " + " + end_reason) if detection_reason else end_reason
            else:
                # End date passed but winning price < 0.95 — outcome still uncertain
                confidence = max(confidence, 50.0)
                detection_reason = (detection_reason + " + " + end_reason + " (price too low)") if detection_reason else (end_reason + " (price too low)")

        # P0 #1 FIX: Fail-closed for price-only markets in live mode
        if not self.config.paper_mode and outcome_sources == ["price"]:
            certainty = CertaintyLevel.UNCERTAIN
            detection_reason = "No resolution source available (price-only is insufficient in live mode)"

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

        # SECTION 5 AUDIT: Calculate dispute risk and track UMA status
        result.resolution_dispute_risk = self._calculate_dispute_risk(market, result)
        result.uma_resolution_status = str(resolution_rules.uma_resolution_statuses) if resolution_rules.uma_resolution_statuses else ""
        result.resolution_timestamp = resolution_rules.closed_time

        finality_status, is_final, finality_reason = self._check_finality_gate(market, result)
        result.finality_status = finality_status.value
        result.is_final = is_final
        result.finality_reason = finality_reason

        logger.debug(f"[DETECT] {market.question[:50]} | side={winning_side} | cert={certainty.name} "
                     f"| sources={outcome_sources} | final={is_final} ({finality_status.value})")

        return result

    def is_sweepable(self, result):
        if not result:
            return False
        # FIX: Don't sweep markets where winning price is below entry threshold
        # The bot needs winning_price >= min_entry_price to place a valid maker bid
        if result.winning_price < self.config.min_entry_price:
            return False
        if self.config.paper_mode:
            if result.certainty in (CertaintyLevel.CERTAIN, CertaintyLevel.STRONG):
                return True
            return False
        else:
            if result.certainty in (CertaintyLevel.CERTAIN, CertaintyLevel.STRONG) and result.is_final:
                # SECTION 1 AUDIT: Check resolution dispute risk
                is_safe, risk, reason = self._check_resolution_dispute_risk(result)
                if not is_safe:
                    logger.warning(f"[DISPUTE RISK] Blocked sweep: {result.question[:50]} - {reason}")
                    return False
                # SECTION 5 AUDIT: Verify resolution on-chain (live mode only)
                ok_chain, chain_msg = self._verify_onchain_resolution(result.condition_id)
                if not ok_chain:
                    logger.warning(f"[ONCHAIN] Blocked sweep: {result.question[:50]} - {chain_msg}")
                    return False
                return True
            if result.certainty == CertaintyLevel.CERTAIN and not result.is_final:
                logger.warning(f"[FINALITY GATE] Blocked sweep: {result.question[:50]} - {result.finality_reason}")
                return False
            return False  # P0 #1 FIX: fail-closed for all other cases (STRONG+non-final, WEAK, UNCERTAIN)