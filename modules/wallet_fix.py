"""Wallet balance fix - checks funder/proxy address and multiple USDC contracts.

FIX: The original get_usdc_balance() in main.py only checked the EOA wallet's
USDC.e balance on-chain. On Polymarket, deposited funds go to a proxy/Safe
wallet, not the EOA. This module checks:
  1. CLOB API get_balance_allowance (queries proxy wallet balance)
  2. On-chain USDC.e balance at funder/proxy address
  3. On-chain native USDC balance at funder/proxy address
  4. On-chain pUSD balance at funder/proxy address
  5. Falls back to EOA wallet if funder is set but has no balance
"""
import logging

logger = logging.getLogger("sweeper.main")


def get_usdc_balance(config, order_builder):
    """Get actual USDC/pUSD balance for pre-trade logging.

    Uses funder/proxy address if configured (Polymarket deposits go to
    a proxy/Safe wallet, not the EOA). Checks multiple USDC contract addresses.
    """
    if config.paper_mode:
        return -1.0

    # Determine which address holds the trading funds
    # On Polymarket, deposited USDC goes to a proxy/Safe wallet, not the EOA
    check_addr = getattr(config, 'funder', '') or getattr(config, 'wallet_address', '')
    if not check_addr:
        logger.warning("[WALLET] No wallet or funder address configured")
        return -1.0

    # Try CLOB API balance first (queries proxy wallet balance)
    try:
        client = order_builder._get_client()
        if client:
            from py_clob_client_v2 import BalanceAllowanceParams, AssetType
            resp = client.get_balance_allowance(
                params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            )
            if resp:
                bal_raw = (
                    resp.get('balance', resp.get('Balance', '0'))
                    if isinstance(resp, dict)
                    else str(resp)
                )
                try:
                    bal = float(bal_raw) / 1e6
                    if bal > 0:
                        return bal
                    logger.info(
                        f"[WALLET] CLOB API returned 0 for "
                        f"{check_addr[:12]}..., trying on-chain"
                    )
                except (ValueError, TypeError):
                    pass
    except Exception as e:
        logger.warning(f'CLOB balance check failed, trying on-chain: {e}')

    # On-chain fallback: check funder/proxy wallet across USDC contracts
    try:
        rpc = getattr(config, 'polygon_rpc', '') or getattr(config, 'rpc_url', '')
        if not rpc:
            return -1.0
        import requests

        # Polymarket may use different USDC contracts depending on migration state
        usdc_contracts = [
            "0x2791Bca1f2de4661ED88A30C99A7a9449Aa8414C",  # USDC.e (bridged)
            "0x3c499c542cEF6E9661c1F67d60F7366e4e4fB3Dc",  # Native USDC
        ]
        try:
            from config.settings import PUSD
            if PUSD and PUSD not in usdc_contracts:
                usdc_contracts.append(PUSD)
        except ImportError:
            pass

        addr = check_addr
        for contract in usdc_contracts:
            try:
                data = "0x70a08231000000000000000000000000" + addr[2:].lower()
                r = requests.post(
                    rpc,
                    json={
                        "method": "eth_call",
                        "params": [{"to": contract, "data": data}, "latest"],
                        "id": 1,
                        "jsonrpc": "2.0",
                    },
                    timeout=10,
                )
                result = r.json().get("result", "0x0")
                if not result or result == "0x":
                    result = "0x0"
                bal = int(result, 16) / 1e6
                if bal > 0:
                    logger.info(
                        f"[WALLET] Found {bal:.2f} USDC at "
                        f"{contract[:12]}... for {addr[:12]}..."
                    )
                    return bal
            except Exception:
                continue

        # Also check EOA directly if funder was used but had no balance
        eoa_addr = getattr(config, 'wallet_address', '')
        if eoa_addr and eoa_addr != check_addr:
            for contract in usdc_contracts:
                try:
                    data = (
                        "0x70a08231000000000000000000000000"
                        + eoa_addr[2:].lower()
                    )
                    r = requests.post(
                        rpc,
                        json={
                            "method": "eth_call",
                            "params": [
                                {"to": contract, "data": data},
                                "latest",
                            ],
                            "id": 1,
                            "jsonrpc": "2.0",
                        },
                        timeout=10,
                    )
                    result = r.json().get("result", "0x0")
                    if not result or result == "0x":
                        result = "0x0"
                    bal = int(result, 16) / 1e6
                    if bal > 0:
                        logger.info(
                            f"[WALLET] Found {bal:.2f} USDC at EOA"
                        )
                        return bal
                except Exception:
                    continue

        logger.warning(
            f"[WALLET] No USDC found at {check_addr[:12]}... "
            f"or EOA across all contracts"
        )
        return 0.0
    except Exception as e:
        logger.warning(f"USDC balance check failed: {e}")
        return -1.0
