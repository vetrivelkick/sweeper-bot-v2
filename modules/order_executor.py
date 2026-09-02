"""Sweeper Bot V2 - Order Builder with GTC Post-Only + Queued Positions"""
import json, time, random, logging
from dataclasses import dataclass, field, asdict
from typing import Optional, Callable
from enum import Enum
from config.settings import fee_per_share, fee_total, net_edge_per_share

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
    tick = tick_size if isinstance(tick_size, float) else float(tick_size)
    if prefer_maker:
        maker_ceiling = best_ask - tick
        desired = max(maker_ceiling, min_entry)
        price = min(desired, maker_ceiling, max_entry)
        price = int(price / tick) * tick
        if min_entry <= price < best_ask:
            return (price, True, f"resting maker bid @ {price} (ask={best_ask}, tick={tick})")
        if not allow_taker:
            return None
    if min_entry <= best_ask <= max_entry:
        return (best_ask, False, f"taker fallback @ best ask {best_ask}")
    return None

class OrderBuilder:
    def __init__(self, config):
        self.config = config; self._client = None
        self._resting = {}; self._reserved = {}
        self._rng = random.Random(); self._force_post_only = False; self._cancel_only = False

    def _get_client(self):
        if self._client or self.config.paper_mode: return self._client
        try:
            from py_clob_client_v2 import ClobClient, ApiCreds
            creds = ApiCreds(api_key=self.config.clob_api_key, api_secret=self.config.clob_api_secret, api_passphrase=self.config.clob_api_passphrase)
            self._client = ClobClient(host="https://clob.polymarket.com", key=self.config.private_key, chain_id=137, creds=creds)
            logger.info("CLOB client initialized for live trading")
        except Exception as e: logger.error(f"Failed to init CLOB client: {e}")
        return self._client

    def reserved_collateral(self): return sum(self._reserved.values())
    def list_open_orders(self): return list(self._resting.values())

    def cancel_all(self):
        count = 0
        for order_id in list(self._resting):
            if self._cancel_order(order_id): count += 1
        return count

    def _cancel_order(self, order_id):
        order = self._resting.pop(order_id, None)
        if not order: return True
        self._reserved.pop(order_id, None)
        if order.is_paper: order.status = OrderStatus.CANCELLED; return True
        client = self._get_client()
        if client:
            try: client.cancel(order_id); order.status = OrderStatus.CANCELLED; logger.info(f"Cancelled {order_id}"); return True
            except Exception as e: logger.error(f"Cancel failed {order_id}: {e}"); return False
        return True

    def build_and_place(self, detection_result, size=100.0, best_ask=None, tick_size=0.001, neg_risk=False):
        is_maker = self.config.prefer_maker
        order_type = "GTC" if is_maker else "FAK"
        post_only = is_maker
        if is_maker and best_ask is not None:
            plan = plan_entry(best_ask, tick_size, 0.985, self.config.buy_price, self.config.prefer_maker, self.config.allow_taker_fallback)
            if plan is None: logger.warning(f"No valid entry for {detection_result.question[:40]}"); return False, None
            price, is_maker, detail = plan; order_type = "GTC" if is_maker else "FAK"; post_only = is_maker
            logger.info(f"Entry plan: {detail}")
        else:
            price = self.config.buy_price
        tick_str = f"{tick_size}" if isinstance(tick_size, float) else str(tick_size)
        order_id = f"sweep_{detection_result.condition_id[:16]}_{int(time.time())}"
        if is_maker:
            order = RestingOrder(order_id=order_id, condition_id=detection_result.condition_id, token_id=detection_result.winning_token_id,
                market_question=detection_result.question, side="BUY", price=price, shares=size, tick_size=tick_str, neg_risk=neg_risk, post_only=True, is_maker=True)
        else:
            order = SweepOrder(order_id=order_id, condition_id=detection_result.condition_id, token_id=detection_result.winning_token_id,
                side="BUY", price=price, size=size, order_type="FAK", tick_size=tick_str, neg_risk=neg_risk, is_maker=False)
        if self.config.paper_mode: return self._paper_place(order, is_maker, size, price)
        return self._live_place(order, is_maker, post_only, order_type)

    def _paper_place(self, order, is_maker, size, price):
        order.is_paper = True
        if is_maker:
            if self._rng.random() > self.config.fill_probability:
                order.status = OrderStatus.LIVE; self._resting[order.order_id] = order; self._reserved[order.order_id] = size * price
                logger.info(f"[PAPER] GTC post-only RESTING: BUY {size} @ {price} for {order.market_question[:40]} - queued")
                return True, order
            shares = size
            if self._rng.random() < self.config.partial_fill_probability:
                shares = max(1.0, size * self.config.partial_fill_ratio); shares = int(shares)
                logger.info(f"[PAPER] Partial maker fill: {shares}/{size}")
            ghost = self._rng.random() < self.config.ghost_probability
            order.filled_shares = float(shares); order.avg_fill_price = price
            order.tx_hash = None if ghost else f"paper_tx_{int(time.time())}"
            order.status = OrderStatus.MATCHED if not ghost else OrderStatus.LIVE
            if not ghost: order.status = OrderStatus.FILLED if shares >= size else OrderStatus.PARTIAL
            self._resting[order.order_id] = order
            self._reserved[order.order_id] = 0.0 if shares >= size else (size - shares) * price
            logger.info(f"[PAPER] GTC maker FILL: {shares} @ {price} {'GHOST' if ghost else 'OK'} for {order.market_question[:40]}")
            return True, order
        else:
            order.status = OrderStatus.SIGNED; order.submitted_at = time.time(); order.matched_at = time.time()
            order.fill_amount = size; order.tx_hash = f"paper_tx_{int(time.time())}"
            logger.info(f"[PAPER] FAK taker FILL: BUY {size} @ {price} for {order.condition_id[:16]}")
            return True, order

    def _live_place(self, order, is_maker, post_only, order_type):
        client = self._get_client()
        if not client: order.status = OrderStatus.FAILED; order.error = "CLOB client not available"; return False, order
        try:
            from py_clob_client_v2 import OrderArgs, PartialCreateOrderOptions
            from py_clob_client_v2.order_builder.constants import BUY
            order_args = OrderArgs(token_id=order.token_id, price=order.price, size=order.shares if isinstance(order, RestingOrder) else order.size, side=BUY)
            options = PartialCreateOrderOptions(tick_size=order.tick_size, neg_risk=order.neg_risk)
            signed = client.create_order(order_args, options)
            if post_only: response = client.post_order(signed, order_type="GTC", post_only=True)
            else: response = client.post_order(signed, order_type="FAK")
            order.submitted_at = time.time()
            if isinstance(response, dict):
                if response.get("status") == "matched":
                    order.status = OrderStatus.MATCHED; order.matched_at = time.time()
                    order.tx_hash = response.get("txHash", "")
                    fill = float(response.get("size_matched", 0))
                    if isinstance(order, RestingOrder): order.filled_shares = fill; order.avg_fill_price = order.price
                    else: order.fill_amount = fill
                    logger.info(f"[LIVE] Order MATCHED: {fill} shares")
                elif response.get("orderID"):
                    order.order_id = response.get("orderID", order.order_id); order.status = OrderStatus.LIVE
                    logger.info(f"[LIVE] GTC post-only RESTING: {order.order_id}")
                elif response.get("error"): self._handle_rejection(order, response.get("error", ""), response)
            else: order.status = OrderStatus.SUBMITTED
            if order.status in (OrderStatus.LIVE, OrderStatus.MATCHED, OrderStatus.FILLED, OrderStatus.PARTIAL):
                if isinstance(order, RestingOrder) and order.status == OrderStatus.LIVE:
                    self._resting[order.order_id] = order; self._reserved[order.order_id] = order.shares * order.price
            return True, order
        except Exception as e: self._handle_rejection(order, str(e), None); return False, order

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
        elif "post_only_would_cross" in e or "would cross" in e:
            order.status = OrderStatus.REJECTED; order.error = RejectCode.POST_ONLY_WOULD_CROSS.value; logger.warning("Post-only would cross")
        elif "not_enough_balance" in e:
            order.status = OrderStatus.FAILED; order.error = RejectCode.NOT_ENOUGH_BALANCE.value; logger.error("Insufficient balance")
        elif "geoblock" in e or "blocked" in e or "region" in e:
            order.status = OrderStatus.FAILED; order.error = RejectCode.REGION_RESTRICTED.value; logger.error("Region restricted")
        else:
            order.status = OrderStatus.FAILED; order.error = err_msg; logger.error(f"Order rejected: {err_msg}")

    def get_order(self, order_id, ask_source=None):
        order = self._resting.get(order_id)
        if not order or order.status not in (OrderStatus.LIVE, OrderStatus.PARTIAL): return order
        elapsed = time.monotonic() - order.placed_at
        if elapsed > self.config.resting_order_timeout:
            order.status = OrderStatus.EXPIRED; self._reserved.pop(order_id, None)
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
                            order.tx_hash = status.get("txHash", "")
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
        edge = net_edge_per_share(self.config.buy_price, self.config.loser_max_price, gas_cost / 100, is_maker=is_maker)
        if edge <= 0: return 0.0
        max_size = available_capital / self.config.buy_price
        min_size = gas_cost / edge
        if max_size < min_size: return 0.0
        return round(max_size * 0.9, 2)
