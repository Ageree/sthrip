"""Debug endpoint for wallet inspection."""

import logging
from fastapi import APIRouter, Depends
from api.deps import get_admin_session

logger = logging.getLogger("sthrip")
router = APIRouter(prefix="/v2/admin", tags=["debug"])


@router.get("/wallet-debug")
async def wallet_debug(_auth: bool = Depends(get_admin_session)):
    result = {"wallet": None, "transfers": None, "swap_orders": None, "errors": []}
    try:
        from requests.auth import HTTPDigestAuth
        import requests as _req
        host = "monero-wallet-rpc.railway.internal"
        port = 18082
        url = "http://{}:{}/json_rpc".format(host, port)
        auth = HTTPDigestAuth("stealthpay", "f243dbd8c693881c557b20d355001832")

        def rpc(method, params=None):
            payload = {"jsonrpc": "2.0", "id": "0", "method": method}
            if params:
                payload["params"] = params
            r = _req.post(url, json=payload, auth=auth, timeout=15)
            return r.json().get("result", {})

        bal = rpc("get_balance")
        result["wallet"] = {
            "balance_xmr": bal.get("balance", 0) / 1e12,
            "unlocked_xmr": bal.get("unlocked_balance", 0) / 1e12,
        }

        tr = rpc("get_transfers", {"in": True, "pool": True, "out": True})
        recent_in = []
        for t in tr.get("in", [])[:10]:
            recent_in.append({
                "amount_xmr": t.get("amount", 0) / 1e12,
                "confirmations": t.get("confirmations", 0),
                "address": t.get("address", "")[:40],
                "txid": t.get("txid", "")[:20],
            })
        recent_pool = []
        for t in tr.get("pool", [])[:5]:
            recent_pool.append({
                "amount_xmr": t.get("amount", 0) / 1e12,
                "address": t.get("address", "")[:40],
                "txid": t.get("txid", "")[:20],
            })
        recent_out = []
        for t in tr.get("out", [])[:5]:
            recent_out.append({
                "amount_xmr": t.get("amount", 0) / 1e12,
                "address": t.get("address", "")[:40],
                "txid": t.get("txid", "")[:20],
            })
        result["transfers"] = {
            "confirmed_in": len(tr.get("in", [])),
            "pool": len(tr.get("pool", [])),
            "outgoing": len(tr.get("out", [])),
            "recent_in": recent_in,
            "recent_pool": recent_pool,
            "recent_out": recent_out,
        }
    except Exception as e:
        result["errors"].append("wallet: " + str(e))

    try:
        from sthrip.db.models import SwapOrder
        from sthrip.db.database import get_db
        swaps = []
        with get_db() as db:
            for s in db.query(SwapOrder).order_by(SwapOrder.created_at.desc()).limit(10).all():
                swaps.append({
                    "id": str(s.id),
                    "from_amt": str(s.from_amount),
                    "from_cur": s.from_currency,
                    "to_amt": str(s.to_amount),
                    "to_cur": s.to_currency,
                    "state": s.state.value if hasattr(s.state, "value") else str(s.state),
                    "deposit_address": s.deposit_address,
                    "external_id": s.external_order_id,
                    "provider": s.provider_name,
                    "created_at": s.created_at.isoformat() if s.created_at else None,
                })
        result["swap_orders"] = swaps
    except Exception as e:
        result["errors"].append("swaps: " + str(e))

    return result
