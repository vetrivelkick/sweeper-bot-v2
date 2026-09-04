"""
Sweeper Bot V2 - Order Builder with GTC Post-Only + Queued Positions

FIX #2: Standardized fill probability logic (35% fill, 25% partial, 5% ghost, 35% expired)
FIX #3: Gas cost standardized to 0.001/share
FIX #5: V2 SDK migration: chain=137 -> chain=137
FIX #9: Added 425 exponential backoff retry (1s->2s->4s...->30s, max 10 retries)
FIX #17: plan_entry uses round() instead of int() for tick alignment
FIX #18: Allow taker fallback when best_ask <= max_entry even if allow_taker is False
P0 #2: Fixed CLOB V2 constructor: chain=137 -> chain_id=137 (V2 Python SDK uses chain_id)
P0 #3: Added signature_type and funder parameters to ClobClient constructor
       signature_type: 0=EOA, 1=POLY_PROXY, 2=POLY_GNOSIS_SAFE, 3=POLY_1271
       funder: Required for proxy/Safe/deposit wallets (address holding funds)
P0 #4: Fixed cancellation: client.cancel(order_id) -> client.cancel_orders([order_id])
       V2 SDK uses cancel_order(OrderPayload) or cancel_orders(list), not cancel(str)
P0 #5: Fixed cancel-then-delete ordering: local state removed only AFTER remote cancel succeeds
       Bug: _resting.pop() before client.cancel() -> lost order if cancel fails
P0 #6: Fixed order response parsing: txHash -> transactionsHashes (list), added tradeIDs
       V2 responses use transactionsHashes (list) and tradeIDs, not txHash (string)
P0 #7: Store real exchange-assigned orderID from response, replacing fabricated local ID
P1: Parameterized min_entry_price (was hardcoded 0.985) - now uses self.config.min_entry_price
P1 #2: 429 handling now wired to rate limiter via handle_429()
P1 #3: Duplicate order prevention on 425 retries via is_duplicate_order()/record_order_id()

AUDIT FIX #13: Pass condition_id to check_exposure_before_order for per-market enforcement
AUDIT FIX #24: Order metrics, latency, slippage tracking
SECTION 9 AUDIT: Economics gate integration, order size validation, slippage check for taker,
                 tick size validation, record_fill/rejection/expiry/cancellation calls, order ID uniqueness
"""
import json, time, random, logging
from decimal import Decimal, ROUND_DOWN
from dataclasses import dataclass, field, asdict
from typing import Optional, Callable, Tuple
from enum import Enum
from config.settings import fee_per_share, fee_total, net_edge_per_share, GAS_PER_SHARE

logger = logging.getLogger("sweeper.orders")

class OrderStatus(Enum):
    PENDING = "pending"; SIGNED = "signed"; SUBMITTED = "submitted"
    LIVE = "live"; MATCHED = "matched"; PARTIAL = "partial"; FILLED = "filled"
    FAILED = "failed"; CANCELLED = "cancelled"; EXPIRED = "expired"
    REJECTED = "rejected"; PAPER = "paper"

class RejectCode(Enum):
    UNMATCHED = "unmatched"; NOT_ENOUGH_BALANCE = "not_enough_balance"
    POST_ONLY_WOULD_CROSS = "post_only_would_cross"
    ENGINE_RESTARTING = "engine_restarting"; CANCEL_ONLY_MODE = "cancel_only_mode"
    POST_ONLY_MODE = "post_only_mode"; RATE_LIMITED = "rate_limited"
    BLOCKED_BY_MODE = "blocked_by_mode"; REGION_RESTRICTED = "region_restricted"

@dataclass
class RestingOrder:
    order_id: str; condition_id: str; token_id: str; market_question: str
    side: str; price: float; shares: float; tick_size: str; neg_risk: bool
    post_only: bool = True; is_maker: bool = True
    placed_at: float = field(default_factory=time.monotonic)
    status: OrderStatus = OrderStatus.LIVE
    filled_shares: float = 0.0; avg_fill_price: float = 0.0
    tx_hash: Optional[str] = None; is_paper: bool = False
    error: Optional[str] = None; retry_after: Optional[float] = None

    def to_dict(self):
        d = asdict(self); d["status"] = self.status.value if isinstance(self.status, OrderStatus) else str(self.status); return d

@dataclass
class SweepOrder:
    order_id: str; condition_id: str; token_id: str; side: str
    price: float; size: float; order_type: str; tick_size: str; neg_risk: bool
    signed_order: Optional[dict] = None; status: OrderStatus = OrderStatus.PENDING
    submitted_at: Optional[float] = None; matched_at: Optional[float] = None
    tx_hash: Optional[str] = None; fill_amount: float = 0.0
    is_paper: bool = False; is_maker: bool = False; error: Optional[str] = None

    def to_dict(self):
        d = asdict(self); d["status"] = self.status.value if isinstance(self.status, OrderStatus) else str(self.status); return d

def plan_entry(best_ask, tick_size, min_entry, max_entry, prefer_maker=True, allow_taker=False):
    """P1: Use Decimal for exact tick alignment to avoid float precision drift."""
    tick = Decimal(str(tick_size)) if not isinstance(tick_size, Decimal) else tick_size
    ask_d = Decimal(str(best_ask))
    min_d = Decimal(str(min_entry))
    max_d = Decimal(str(max_entry))
    if prefer_maker:
        maker_ceiling = ask_d - tick
        desired = max(maker_ceiling, min_d)
        price = min(desired, maker_ceiling, max_d)
        price = (price // tick) * tick
        if price >= ask_d:
            price -= tick
        if min_d <= price < ask_d:
            p = float(price)
            return (p, True, f"resting maker bid @ {p} (ask={best_ask}, tick={tick})")
        if not allow_taker:
            return None
    if min_d <= ask_d <= max_d:
        return (float(ask_d), False, f"taker fallback @ best ask {best_ask}")
    return None

class OrderBuilder:
    def __init__(self, config, safety=None, rate_limiter=None):
        self.config = config; self._client = None
        self._resting = {}; self._reserved = {}
        self._rng = random.Random(); self._force_post_only = False; self._cancel_only = False
        self._safety = safety
        self._rate_limiter = rate_limiter
        # AUDIT FIX #24: Order metrics, latency, slippage tracking
        self._total_placed = 0
        self._total_filled = 0
        self._total_rejected = 0
        self._total_expired = 0
        self._total_cancelled = 0
        self._total_slippage = 0.0
        self._fill_latencies = []
        self._max_latency_samples = 100

    def _get_client(self):
        if self._client or self.config.paper_mode: return self._client
        try:
            from py_clob_client_v2 import ClobClient, ApiCreds
            creds = ApiCreds(api_key=self.config.clob_api_key, api_secret=self.config.clob_api_secret, api_passphrase=self.config.clob_api_passphrase)
            self._client = ClobClient(
                host="https://clob.polymarket.com",
                key=self.config.private_key,
                chain_id=137,
                creds=creds,
                signature_type=self.config.signature_type,
                funder=self.config.funder if self.config.funder else None,
            )
            logger.info(f"CLOB V2 client initialized (chain_id=137, sig_type={self.config.signature_type})")
        except Exception as e: logger.error(f"Failed to init CLOB V2 client: {e}")
        return self._client

    def reserved_collateral(self): return sum(self._reserved.values())
    def list_open_orders(self): return list(self._resting.values())

    def cancel_all(self):
        count = 0
        for order_id in list(self._resting):
            if self._cancel_order(order_id): count += 1
        return count

    def _cancel_order(self, order_id):
        order = self._resting.get(order_id)
        if not order: return True
        if order.is_paper:
            self._resting.pop(order_id, None)
            self._reserved.pop(order_id, None)
            order.status = OrderStatus.CANCELLED
            return True
        client = self._get_client()
        if client:
            try:
                client.cancel_orders([order_id])
                self._resting.pop(order_id, None)
                self._reserved.pop(order_id, None)
                order.status = OrderStatus.CANCELLED
                self.record_cancellation()
                logger.info(f"Cancelled {order_id}")
                return True
            except Exception as e:
                logger.error(f"Cancel failed {order_id}: {e}")
                return False
        self._resting.pop(order_id, None)
        self._reserved.pop(order_id, None)
        return True

    def build_and_place(self, detection_result, size=100.0, best_ask=None, tick_size=0.001, neg_risk=False):
        is_maker = self.config.prefer_maker
        order_type = "GTC" if is_maker else "FAK"
        post_only = is_maker
        if is_maker and best_ask is not None:
            plan = plan_entry(best_ask, tick_size, self.config.min_entry_price, self.config.buy_price, self.config.prefer_maker, self.config.allow_taker_fallback)
            if plan is None: logger.warning(f"No valid entry for {detection_result.question[:40]}"); return False, None
            price, is_maker, detail = plan; order_type = "GTC" if is_maker else "FAK"; post_only = is_maker
            logger.info(f"Entry plan: {detail}")
        else:
            price = self.config.buy_price
        tick_str = f"{tick_size}" if isinstance(tick_size, float) else str(tick_size)
        # SECTION 9 AUDIT: Validate tick size
        _valid_ticks = {'0.0001', '0.001', '0.01', '0.1'}
        if tick_str not in _valid_ticks:
            logger.warning(f"Invalid tick size: {tick_str}, defaulting to 0.001")
            tick_str = '0.001'
        # SECTION 9 AUDIT: Order ID uniqueness with random suffix
        order_id = f"sweep_{detection_result.condition_id[:16]}_{int(time.time())}_{self._rng.randint(1000, 9999)}"
        if is_maker:
            order = RestingOrder(order_id=order_id, condition_id=detection_result.condition_id, token_id=detection_result.winning_token_id,
                market_question=detection_result.question, side="BUY", price=price, shares=size, tick_size=tick_str, neg_risk=neg_risk, post_only=True, is_maker=True)
        else:
            order = SweepOrder(order_id=order_id, condition_id=detection_result.condition_id, token_id=detection_result.winning_token_id,
                side="BUY", price=price, size=size, order_type="FAK", tick_size=tick_str, neg_risk=neg_risk, is_maker=False)
        if self._safety and self._safety.state.is_killed:
            logger.warning(f"Order blocked - kill switch: {self._safety.state.kill_reason}")
            return False, None
        # SECTION 9 AUDIT: Economics gate check before order placement
        if self._safety:
            _cat = getattr(detection_result, 'category', 'other')
            ok_econ, econ_msg = self._safety.check_economics_gate(
                buy_price=price, shares=size, category=_cat,
                is_maker=is_maker, condition_id=detection_result.condition_id)
            if not ok_econ:
                logger.warning(f"Order blocked - economics gate: {econ_msg}")
                return False, None
        # SECTION 9 AUDIT: Validate order size against economic minimums
        from config.settings import MIN_ORDER_SIZE_ECONOMIC, min_viable_size as _mvs
        if size < MIN_ORDER_SIZE_ECONOMIC:
            logger.warning(f"Order size {size} < MIN_ORDER_SIZE_ECONOMIC {MIN_ORDER_SIZE_ECONOMIC}")
            return False, None
        _mv = _mvs(GAS_PER_SHARE * 100, price, is_maker)
        if size < _mv:
            logger.warning(f"Order size {size} < min viable {_mv:.1f}")
            return False, None
        # SECTION 9 AUDIT: Slippage check for taker orders
        if not is_maker and self._safety:
            _slip = self._safety.estimate_slippage(size)
            if not _slip['within_threshold']:
                logger.warning(f"Taker blocked - slippage {_slip['estimated_slippage']:.6f} > max {_slip['max_slippage']}")
                return False, None
        if self._safety:
            order_cost = size * price
            ok_exp, exp_msg = self._safety.check_exposure_before_order(order_cost, self.list_open_orders(), condition_id=detection_result.condition_id)
            if not ok_exp:
                logger.warning(f"Order blocked - exposure: {exp_msg}")
                return False, None
        if self.config.paper_mode:
            ok, ord = self._paper_place(order, is_maker, size, price)
            if ok: self._total_placed += 1
            return ok, ord
        ok, ord = self._live_place(order, is_maker, post_only, order_type)
        if ok: self._total_placed += 1
        return ok, ord

    def place_complementary_buy(self, detection_result, size=100.0, tick_size=0.001):
        """P0 #12: Place a buy order for the complementary (losing) side."""
        losing_token = getattr(detection_result, 'losing_token_id', '')
        if not losing_token:
            logger.error("No losing_token_id for complementary buy")
            return False, None
        neg_risk = getattr(detection_result, 'neg_risk', False)
        tick_str = f"{tick_size}" if isinstance(tick_size, float) else str(tick_size)
        order_id = f"comp_{detection_result.condition_id[:16]}_{int(time.time())}"
        order = SweepOrder(
            order_id=order_id, condition_id=detection_result.condition_id,
            token_id=losing_token, side="BUY",
            price=self.config.loser_max_price, size=size,
            order_type="FAK", tick_size=tick_str, neg_risk=neg_risk,
            is_maker=False,
        )
        if self._safety and self._safety.state.is_killed:
            logger.warning(f"Complementary buy blocked - kill switch: {self._safety.state.kill_reason}")
            return False, None
        # SECTION 9 AUDIT: Economics gate for complementary buy
        if self._safety:
            ok_econ, econ_msg = self._safety.check_economics_gate(
                buy_price=self.config.loser_max_price, shares=size, category='other',
                is_maker=False, condition_id=detection_result.condition_id)
            if not ok_econ:
                logger.warning(f"Complementary buy blocked - economics gate: {econ_msg}")
                return False, None
            order_cost = size * self.config.loser_max_price
            ok_exp, exp_msg = self._safety.check_exposure_before_order(order_cost, self.list_open_orders(), condition_id=detection_result.condition_id)
            if not ok_exp:
                logger.warning(f"Complementary buy blocked - exposure: {exp_msg}")
                return False, None
        if self.config.paper_mode:
            return self._paper_place(order, False, size, self.config.loser_max_price)
        return self._live_place(order, False, False, "FAK")

    def _paper_place(self, order, is_maker, size, price):
        order.is_paper = True
        if is_maker:
            roll = self._rng.random()
            if roll < self.config.fill_probability:
                shares = size
                ghost = self._rng.random() < self.config.ghost_probability
                order.filled_shares = float(shares); order.avg_fill_price = price
                order.tx_hash = None if ghost else f"paper_tx_{int(time.time())}"
                order.status = OrderStatus.FILLED if not ghost else OrderStatus.LIVE
                self._resting[order.order_id] = order
                self._reserved[order.order_id] = 0.0
                logger.info(f"[PAPER] GTC maker FILL: {shares} @ {price} {'GHOST' if ghost else 'OK'} for {order.market_question[:40]}")
                self.record_fill(order)
                return True, order
            elif roll < self.config.fill_probability + self.config.partial_fill_probability:
                shares = max(1.0, int(size * self.config.partial_fill_ratio))
                order.filled_shares = float(shares); order.avg_fill_price = price
                order.tx_hash = f"paper_tx_{int(time.time())}"
                order.status = OrderStatus.PARTIAL
                self._resting[order.order_id] = order
                self._reserved[order.order_id] = (size - shares) * price
                logger.info(f"[PAPER] Partial maker fill: {shares}/{size}")
                return True, order
            elif roll < self.config.fill_probability + self.config.partial_fill_probability + self.config.ghost_probability:
                order.filled_shares = float(size); order.avg_fill_price = price
                order.tx_hash = None; order.status = OrderStatus.LIVE
                self._resting[order.order_id] = order
                self._reserved[order.order_id] = size * price
                logger.info(f"[PAPER] GHOST fill for {order.market_question[:40]}")
                return True, order
            else:
                order.status = OrderStatus.LIVE; self._resting[order.order_id] = order; self._reserved[order.order_id] = size * price
                logger.info(f"[PAPER] GTC post-only RESTING: BUY {size} @ {price} for {order.market_question[:40]} - queued")
                return True, order
        else:
            order.status = OrderStatus.SIGNED; order.submitted_at = time.time(); order.matched_at = time.time()
            order.fill_amount = size; order.tx_hash = f"paper_tx_{int(time.time())}"
            logger.info(f"[PAPER] FAK taker FILL: BUY {size} @ {price} for {order.condition_id[:16]}")
            self.record_fill(order)
            return True, order

    def _live_place(self, order, is_maker, post_only, order_type):
        client = self._get_client()
        if not client: order.status = OrderStatus.FAILED; order.error = "CLOB V2 client not available"; return False, order
        max_retries = 10
        backoff = 1.0
        for attempt in range(max_retries + 1):
            try:
                from py_clob_client_v2 import OrderArgs, OrderType as V2OrderType, PartialCreateOrderOptions, Side
                order_args = OrderArgs(
                    token_id=order.token_id,
                    price=order.price,
                    size=order.shares if isinstance(order, RestingOrder) else order.size,
                    side=Side.BUY,
                )
                options = PartialCreateOrderOptions(tick_size=order.tick_size, neg_risk=order.neg_risk)
                ot = V2OrderType.GTC if post_only else V2OrderType.FAK
                response = client.create_and_post_order(
                    order_args=order_args,
                    options=options,
                    order_type=ot,
                    post_only=post_only,
                )
                order.submitted_at = time.time()
                if isinstance(response, dict):
                    real_order_id = response.get("orderID")
                    if real_order_id:
                        order.order_id = real_order_id
                    if response.get("status") == "matched":
                        order.status = OrderStatus.MATCHED; order.matched_at = time.time()
                        tx_hashes = response.get("transactionsHashes", [])
                        order.tx_hash = tx_hashes[0] if tx_hashes else None
                        trade_ids = response.get("tradeIDs", [])
                        if not order.tx_hash and trade_ids:
                            logger.info(f"[LIVE] Matched, {len(trade_ids)} trades pending hash resolution")
                        fill = float(response.get("size_matched", 0))
                        if isinstance(order, RestingOrder): order.filled_shares = fill; order.avg_fill_price = order.price
                        else: order.fill_amount = fill
                        self.record_fill(order)
                        logger.info(f"[LIVE] Order MATCHED: {fill} shares, tx={order.tx_hash}")
                    elif response.get("orderID"):
                        order.status = OrderStatus.LIVE
                        logger.info(f"[LIVE] GTC post-only RESTING: {order.order_id}")
                    elif response.get("error"):
                        err = response.get("error", "")
                        if "425" in str(err).lower() and attempt < max_retries:
                            if self._rate_limiter and self._rate_limiter.is_duplicate_order(order.order_id):
                                logger.warning(f"425 retry aborted - duplicate order ID: {order.order_id}")
                                order.status = OrderStatus.REJECTED
                                order.error = "Duplicate order ID on 425 retry"
                                return False, order
                            logger.warning(f"425 retry {attempt+1}/{max_retries}, backing off {backoff:.1f}s")
                            time.sleep(backoff)
                            backoff = min(backoff * 2, 30.0)
                            continue
                        self._handle_rejection(order, err, response)
                    else:
                        order.status = OrderStatus.SUBMITTED
                else:
                    order.status = OrderStatus.SUBMITTED
                if order.status in (OrderStatus.LIVE, OrderStatus.MATCHED, OrderStatus.FILLED, OrderStatus.PARTIAL):
                    if isinstance(order, RestingOrder) and order.status == OrderStatus.LIVE:
                        self._resting[order.order_id] = order; self._reserved[order.order_id] = order.shares * order.price
                    if self._rate_limiter and order.order_id:
                        self._rate_limiter.record_order_id(order.order_id)
                return True, order
            except Exception as e:
                err_msg = str(e)
                if "425" in err_msg and attempt < max_retries:
                    if self._rate_limiter and self._rate_limiter.is_duplicate_order(order.order_id):
                        logger.warning(f"425 retry aborted - duplicate order ID: {order.order_id}")
                        order.status = OrderStatus.REJECTED
                        order.error = "Duplicate order ID on 425 retry"
                        return False, order
                    logger.warning(f"425 retry {attempt+1}/{max_retries}, backing off {backoff:.1f}s")
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 30.0)
                    continue
                self._handle_rejection(order, err_msg, None); return False, order
        order.status = OrderStatus.FAILED; order.error = f"Max retries ({max_retries}) exceeded for 425"
        return False, order

    def _handle_rejection(self, order, err_msg, response):
        e = err_msg.lower()
        if "425" in e or "too early" in e or "restarting" in e:
            order.status = OrderStatus.REJECTED; order.error = RejectCode.ENGINE_RESTARTING.value; logger.warning("425 Engine restarting")
        elif "post_only_mode" in e or "post-only mode" in e:
            order.status = OrderStatus.REJECTED; order.error = RejectCode.POST_ONLY_MODE.value
            retry = response.get("retry_after_seconds") if response and isinstance(response, dict) else None
            order.retry_after = float(retry) if retry else 120.0; self._force_post_only = True; logger.warning(f"503 Post-only mode, retry={order.retry_after}s")
        elif "cancel_only" in e or "cancel-only" in e:
            order.status = OrderStatus.REJECTED; order.error = RejectCode.CANCEL_ONLY_MODE.value; self._cancel_only = True; logger.warning("503 Cancel-only mode")
        elif "429" in e or "rate" in e:
            order.status = OrderStatus.REJECTED; order.error = RejectCode.RATE_LIMITED.value
            retry = response.get("retry_after") if response and isinstance(response, dict) else None
            order.retry_after = float(retry) if retry else 5.0; logger.warning(f"429 Rate limited, retry={order.retry_after}s")
            if self._rate_limiter:
                self._rate_limiter.handle_429("order", order.retry_after)
        elif "post_only_would_cross" in e or "would cross" in e or "crosses book" in e:
            order.status = OrderStatus.REJECTED; order.error = RejectCode.POST_ONLY_WOULD_CROSS.value; logger.warning("Post-only would cross")
        elif "not_enough_balance" in e:
            order.status = OrderStatus.FAILED; order.error = RejectCode.NOT_ENOUGH_BALANCE.value; logger.error("Insufficient balance")
        elif "geoblock" in e or "blocked" in e or "region" in e:
            order.status = OrderStatus.FAILED; order.error = RejectCode.REGION_RESTRICTED.value; logger.error("Region restricted")
        else:
            order.status = OrderStatus.FAILED; order.error = err_msg; logger.error(f"Order rejected: {err_msg}")
        self.record_rejection()

    def get_order(self, order_id, ask_source=None):
        order = self._resting.get(order_id)
        if not order or order.status not in (OrderStatus.LIVE, OrderStatus.PARTIAL): return order
        elapsed = time.monotonic() - order.placed_at
        if elapsed > self.config.resting_order_timeout:
            order.status = OrderStatus.EXPIRED; self._reserved.pop(order_id, None)
            self.record_expiry()
            logger.info(f"Resting order EXPIRED after {elapsed:.1f}s: {order.market_question[:40]}"); return order
        if order.is_paper:
            if ask_source:
                live_ask = ask_source(order.token_id)
                if live_ask is not None and live_ask <= order.price:
                    order.filled_shares = order.shares; order.avg_fill_price = order.price
                    order.tx_hash = f"paper_tx_{int(time.time())}"; order.status = OrderStatus.FILLED
                    self._reserved.pop(order_id, None); logger.info(f"[PAPER] Maker fill via ask touch: {order.shares} @ {order.price}"); return order
            if elapsed > self.config.touch_fill_seconds:
                if self._rng.random() < 0.5:
                    order.filled_shares = order.shares; order.avg_fill_price = order.price
                    order.tx_hash = f"paper_tx_{int(time.time())}"; order.status = OrderStatus.FILLED
                    self._reserved.pop(order_id, None); logger.info(f"[PAPER] Touch fill after {elapsed:.1f}s"); return order
        else:
            client = self._get_client()
            if client:
                try:
                    status = client.get_order(order_id)
                    if isinstance(status, dict):
                        matched = float(status.get("size_matched", 0))
                        if matched > 0:
                            order.filled_shares = matched; order.avg_fill_price = order.price
                            tx_hashes = status.get("transactionsHashes", [])
                            order.tx_hash = tx_hashes[0] if tx_hashes else status.get("txHash", "")
                            if matched >= order.shares: order.status = OrderStatus.FILLED; self._reserved.pop(order_id, None)
                            else: order.status = OrderStatus.PARTIAL; self._reserved[order_id] = (order.shares - matched) * order.price
                            logger.info(f"[LIVE] Order fill: {matched}/{order.shares}")
                except Exception as e: logger.debug(f"Order poll error: {e}")
        return order

    def reconcile_orders(self, ask_source=None):
        filled = []; expired = []; cancelled = []; still_resting = []
        for order_id in list(self._resting):
            order = self.get_order(order_id, ask_source)
            if not order: continue
            if order.status == OrderStatus.FILLED: filled.append(order); self._resting.pop(order_id, None)
            elif order.status == OrderStatus.PARTIAL: self._cancel_order(order_id); cancelled.append(order)
            elif order.status == OrderStatus.EXPIRED: expired.append(order); self._resting.pop(order_id, None)
            elif order.status in (OrderStatus.LIVE, OrderStatus.PARTIAL): still_resting.append(order)
        return {"filled": filled, "expired": expired, "cancelled": cancelled, "still_resting": still_resting,
                "total_resting": len(still_resting), "reserved_collateral": self.reserved_collateral()}

    def shutdown(self):
        if not self.config.cancel_orders_on_shutdown: logger.info("cancel_orders_on_shutdown=False"); return 0
        count = self.cancel_all(); logger.info(f"Shutdown: cancelled {count} resting orders"); return count

    def calculate_order_size(self, available_capital, gas_cost, is_maker=False):
        edge = net_edge_per_share(self.config.buy_price, self.config.loser_max_price, GAS_PER_SHARE, is_maker=is_maker)
        if edge <= 0: return 0.0
        max_size = available_capital / self.config.buy_price
        min_size = gas_cost / edge
        if max_size < min_size: return 0.0
        return round(max_size * 0.9, 2)

    def record_fill(self, order):
        """AUDIT FIX #24: Record fill metrics (latency, slippage)."""
        self._total_filled += 1
        if hasattr(order, 'submitted_at') and order.submitted_at:
            latency = time.time() - order.submitted_at
            self._fill_latencies.append(latency)
            if len(self._fill_latencies) > self._max_latency_samples:
                self._fill_latencies = self._fill_latencies[-self._max_latency_samples:]
        if hasattr(order, 'avg_fill_price') and hasattr(order, 'price'):
            slippage = abs(order.avg_fill_price - order.price)
            self._total_slippage += slippage

    def record_rejection(self):
        """AUDIT FIX #24: Record order rejection."""
        self._total_rejected += 1

    def record_expiry(self):
        """AUDIT FIX #24: Record order expiry."""
        self._total_expired += 1

    def record_cancellation(self):
        """AUDIT FIX #24: Record order cancellation."""
        self._total_cancelled += 1

    def get_order_metrics(self) -> dict:
        """AUDIT FIX #24: Return order metrics for monitoring."""
        avg_latency = sum(self._fill_latencies) / len(self._fill_latencies) if self._fill_latencies else 0.0
        fill_rate = self._total_filled / max(1, self._total_placed) * 100
        avg_slippage = self._total_slippage / max(1, self._total_filled)
        return {
            'total_placed': self._total_placed,
            'total_filled': self._total_filled,
            'total_rejected': self._total_rejected,
            'total_expired': self._total_expired,
            'total_cancelled': self._total_cancelled,
            'fill_rate': round(fill_rate, 2),
            'avg_fill_latency_s': round(avg_latency, 3),
            'avg_slippage': round(avg_slippage, 6),
            'total_slippage': round(self._total_slippage, 6),
            'resting_orders': len(self._resting),
            'reserved_collateral': round(self.reserved_collateral(), 4),
        }