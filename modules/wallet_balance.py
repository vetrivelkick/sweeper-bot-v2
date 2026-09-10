"""Wallet balance checker - tries multiple signature types and USDC contracts.

FIX: Polymarket deposits go to a proxy/Safe wallet, not the EOA.
When signature_type=0 (EOA) returns 0, try types 1-3 (proxy/Safe/1271)
to find funds in the proxy wallet. Also checks on-chain USDC.e and native USDC.
"""
import logging

logger = logging.getLogger("sweeper.main")


def get_wallet_balance(config, order_builder=None):
    """Get actual USDC/pUSD balance - tries multiple signature types.

    Returns balance as float, or -1.0 if unavailable, or 0.0 if no balance found.
    """
    if config.paper_mode:
        return -1.0

    configured_sig = getattr(config, 'signature_type', 0)

    # Try CLOB API with configured signature_type first
    if order_builder:
        try:
            client = order_builder._get_client()
            if client:
                from py_clob_client_v2 import BalanceAllowanceParams, AssetType
                resp = client.get_balance_allowance(params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
                if resp:
                    bal_raw = resp.get('balance', resp.get('Balance', '0')) if isinstance(resp, dict) else str(resp)
                    try:
                        bal = float(bal_raw) / 1e6
                        if bal > 0:
                            return bal
                        logger.info(f'[WALLET] CLOB API (sig_type={configured_sig}) returned 0, trying other types...')
                    except (ValueError, TypeError):
                        pass
        except Exception as e:
            logger.warning(f'CLOB balance check failed (sig_type={configured_sig}): {e}')

    # FIX ISSUE #1: When order_builder is None (startup), still try configured sig_type first
    if not order_builder:
        try:
            from py_clob_client_v2 import ClobClient, ApiCreds, BalanceAllowanceParams, AssetType
            creds = ApiCreds(api_key=config.clob_api_key, api_secret=config.clob_api_secret, api_passphrase=config.clob_api_passphrase)
            test_client = ClobClient(host="https://clob.polymarket.com", key=config.private_key, chain_id=137, creds=creds, signature_type=configured_sig, funder=config.funder if config.funder else None)
            resp = test_client.get_balance_allowance(params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
            if resp:
                bal_raw = resp.get('balance', resp.get('Balance', '0')) if isinstance(resp, dict) else str(resp)
                try:
                    bal = float(bal_raw) / 1e6
                    if bal > 0:
                        return bal
                    logger.info(f'[WALLET] CLOB API (sig_type={configured_sig}) returned 0, trying other types...')
                except (ValueError, TypeError):
                    pass
        except Exception as e:
            logger.warning(f'CLOB balance check failed (sig_type={configured_sig}): {e}')

    # Try other signature_types: 1=POLY_PROXY, 2=POLY_GNOSIS_SAFE, 3=POLY_1271
    for sig_type in [1, 2, 3]:
        if sig_type == configured_sig:
            continue
        try:
            from py_clob_client_v2 import ClobClient, ApiCreds, BalanceAllowanceParams, AssetType
            creds = ApiCreds(
                api_key=config.clob_api_key,
                api_secret=config.clob_api_secret,
                api_passphrase=config.clob_api_passphrase,
            )
            test_client = ClobClient(
                host="https://clob.polymarket.com",
                key=config.private_key,
                chain_id=137,
                creds=creds,
                signature_type=sig_type,
                funder=config.funder if config.funder else None,
            )
            resp = test_client.get_balance_allowance(params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
            if resp:
                bal_raw = resp.get('balance', resp.get('Balance', '0')) if isinstance(resp, dict) else str(resp)
                try:
                    bal = float(bal_raw) / 1e6
                    if bal > 0:
                        logger.info(f'[WALLET] Found {bal:.2f} pUSD with signature_type={sig_type} - auto-switching config')
                        config.signature_type = sig_type
                        if order_builder:
                            order_builder.config.signature_type = sig_type
                            order_builder._client = None  # Force re-init with correct sig type
                        return bal
                except (ValueError, TypeError):
                    pass
        except Exception:
            pass

    # On-chain fallback: check multiple USDC contracts at wallet address
    try:
        addr = getattr(config, 'wallet_address', '')
        if not addr:
            return 0.0
        rpc = getattr(config, 'polygon_rpc', '') or getattr(config, 'rpc_url', '')
        if not rpc:
            return 0.0
        import requests
        usdc_contracts = [
            ("0x2791Bca1f2de4661ED88A30C99A7a9449Aa8414C", "USDC.e"),
            ("0x3c499c542cEF6E9661c1F67d60F7366e4e4fB3Dc", "USDC"),
        ]
        for contract, name in usdc_contracts:
            data = "0x70a08231000000000000000000000000" + addr[2:].lower()
            r = requests.post(rpc, json={"method": "eth_call", "params": [{"to": contract, "data": data}, "latest"], "id": 1, "jsonrpc": "2.0"}, timeout=10)
            result = r.json().get("result", "0x0")
            if not result or result == "0x":
                result = "0x0"
            bal = int(result, 16) / 1e6
            if bal > 0:
                logger.info(f'[WALLET] Found {bal:.2f} {name} on-chain at EOA')
                return bal
    except Exception as e:
        logger.warning(f"USDC on-chain balance check failed: {e}")

    logger.warning('[WALLET] No USDC found across all signature types and contracts')
    return 0.0


def check_wallet_balance(config):
    """Check if USDC balance is sufficient for trading. Returns True if OK."""
    bal = get_wallet_balance(config)
    if bal < 0:
        return True  # Can't check, allow trading
    min_bal = 5 * float(getattr(config, 'buy_price', 0.99))
    if bal >= min_bal:
        logger.info(f'pUSD OK: {bal:.2f}')
        return True
    logger.error(f'pUSD low: {bal:.2f} (min: {min_bal})')
    return False
