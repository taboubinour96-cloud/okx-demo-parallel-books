#!/usr/bin/env python3
"""
Fully automatic runner for the 4 parallel books.

Designed to run on a schedule (GitHub Actions cron) with ZERO human
involvement. Each run:
  1. Reads each book's ledger JSON (persisted state: trades, risk tracking).
  2. Recomputes the trend signal (book1/book2) from fresh OKX market data.
  3. Resizes/flips the real OKX demo position to match the signal, within
     the book's target notional.
  4. Checks FTMO/FundedNext-style risk rules (daily loss %, max drawdown %)
     using a scaling factor that maps the small real position's P&L onto
     the book's nominal account size. If a rule is breached, the book is
     FLATTENED and marked "breached" -> it stops trading until a human
     resets it (this mirrors how a real prop-firm account would be
     disabled on a rule breach, it is not swept under the rug).
  5. Writes the updated ledger JSON back to disk.
  6. (If running inside GitHub Actions) commits and pushes the updated
     ledgers so state survives across ephemeral runs.

Honesty notes (do not remove):
  - book1 (FTMO) and book2 (FundedNext) trade ETH-USDT-SWAP / SOL-USDT-SWAP
    as crypto proxies because OKX has no SPY/QQQ/TradFi products. The
    entry signal is a simple 4H-close-vs-20-period-SMA trend filter. This
    is NOT the registry-validated htf_spy_qqq_tradfi_perp strategy.
  - book3 (crypto $100) runs the registry-validated funding_carry_btc_eth
    strategy (long BTC spot + short BTC perp, delta-neutral, earns the
    funding rate). It does not flip on the trend signal -- it is a
    buy-and-hold carry position, only monitored here.
  - book4 (Earn2Trade) is intentionally kept flat: the strategy registry
    has no validated intraday edge. This script will not fabricate one.
"""
import json
import math
import os
import subprocess
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(__file__))
from okx_client import OKXClient, OKXError  # noqa: E402

LEDGER_DIR = os.path.join(os.path.dirname(__file__), "..", "ledgers")

BOOKS = {
    "book1_ftmo": {
        "inst_id": "ETH-USDT-SWAP",
        "ccy_inst_id": "ETH-USDT",
        "target_notional_usd": 5000,
        "leverage": 3,
        "mgn_mode": "cross",
        "max_daily_loss_pct": 5,
        "max_total_drawdown_pct": 10,
        "scaling_factor": 5,  # target_notional(5000) -> nominal(25000) == 5x
        "tradeable": True,
    },
    "book2_fundednext": {
        "inst_id": "SOL-USDT-SWAP",
        "ccy_inst_id": "SOL-USDT",
        "target_notional_usd": 5000,
        "leverage": 3,
        "mgn_mode": "cross",
        "max_daily_loss_pct": 5,
        "max_total_drawdown_pct": 10,
        "scaling_factor": 5,
        "tradeable": True,
    },
    "book3_crypto": {
        "inst_id": "BTC-USDT-SWAP",
        "tradeable": False,  # buy-and-hold carry, no signal-based trading here
    },
    "book4_earn2trade": {
        "tradeable": False,  # intentionally flat, no validated edge
    },
}


def log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{ts}] {msg}", flush=True)


def load_ledger(book_id):
    path = os.path.join(LEDGER_DIR, f"{book_id}.json")
    with open(path) as f:
        return path, json.load(f)


def save_ledger(path, ledger):
    with open(path, "w") as f:
        json.dump(ledger, f, indent=2)
        f.write("\n")


def trend_signal(client, ccy_inst_id):
    """Simple, transparent 4H close vs 20-period SMA trend filter.
    Returns ('long'|'short', diagnostics dict)."""
    candles = client.candles(ccy_inst_id, bar="4H", limit=30)
    closes = [float(c[4]) for c in candles]  # most-recent-first
    closes_chrono = list(reversed(closes))
    sma20 = sum(closes_chrono[-20:]) / 20
    last = closes_chrono[-1]
    side = "long" if last > sma20 else "short"
    return side, {"last": last, "sma20_4h": sma20, "dist_pct": (last - sma20) / sma20 * 100}


def size_contracts(client, inst_id, target_notional_usd):
    spec = client.instrument(inst_id)
    ct_val = float(spec["ctVal"])
    lot_sz = float(spec["lotSz"])
    px = float(client.ticker(inst_id)["last"])
    raw_contracts = target_notional_usd / (ct_val * px)
    contracts = math.floor(raw_contracts / lot_sz) * lot_sz
    return round(contracts, 8), px, ct_val


def current_position(client, inst_id):
    positions = client.positions(inst_id)
    if not positions:
        return 0.0, 0.0  # pos size (signed contracts), unrealized pnl usd
    p = positions[0]
    return float(p.get("pos", 0) or 0), float(p.get("upl", 0) or 0)


def manage_book(client, book_id, cfg, ledger):
    inst_id = cfg["inst_id"]
    signal_side, diag = trend_signal(client, cfg["ccy_inst_id"])
    desired_sign = 1 if signal_side == "long" else -1

    # ---- risk check first: compute book equity from ledger risk_state ----
    risk_state = ledger.setdefault(
        "risk_state",
        {
            "inception_equity_usd": cfg["target_notional_usd"] * 0,  # filled below on first run
            "high_water_mark_usd": None,
            "day": None,
            "day_start_equity_usd": None,
            "breached": False,
        },
    )

    cur_pos, upl = current_position(client, inst_id)
    nominal_capital = ledger["nominal_account_size_usd"]
    scaling = cfg["scaling_factor"]
    # realized pnl already banked in ledger trade log (sum of any closed-trade realizedPnl we recorded)
    realized_scaled = ledger.get("realized_pnl_scaled_usd", 0.0)
    book_equity = nominal_capital + realized_scaled + upl * scaling

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if risk_state["day"] != today:
        risk_state["day"] = today
        risk_state["day_start_equity_usd"] = book_equity
    if risk_state["high_water_mark_usd"] is None:
        risk_state["high_water_mark_usd"] = nominal_capital
    risk_state["high_water_mark_usd"] = max(risk_state["high_water_mark_usd"], book_equity)

    daily_loss_pct = (risk_state["day_start_equity_usd"] - book_equity) / nominal_capital * 100
    total_dd_pct = (risk_state["high_water_mark_usd"] - book_equity) / nominal_capital * 100

    breach_reason = None
    if daily_loss_pct >= cfg["max_daily_loss_pct"]:
        breach_reason = f"daily loss {daily_loss_pct:.2f}% >= limit {cfg['max_daily_loss_pct']}%"
    elif total_dd_pct >= cfg["max_total_drawdown_pct"]:
        breach_reason = f"max drawdown {total_dd_pct:.2f}% >= limit {cfg['max_total_drawdown_pct']}%"

    if breach_reason or risk_state.get("breached"):
        if not risk_state.get("breached"):
            risk_state["breached"] = True
            risk_state["breach_reason"] = breach_reason
            risk_state["breach_time_utc"] = datetime.now(timezone.utc).isoformat()
            log(f"{book_id}: RULE BREACH ({breach_reason}) -> flattening and pausing book")
        desired_sign = 0  # force flat

    log(
        f"{book_id}: signal={signal_side} ({diag['dist_pct']:.2f}% vs SMA20) "
        f"cur_pos={cur_pos} upl={upl:.2f} book_equity={book_equity:.2f} "
        f"daily_loss={daily_loss_pct:.2f}% total_dd={total_dd_pct:.2f}%"
    )

    if risk_state.get("breached") and desired_sign == 0 and cur_pos == 0:
        ledger["risk_state"] = risk_state
        return  # nothing to do, already flat and breached

    contracts, px, ct_val = size_contracts(client, inst_id, cfg["target_notional_usd"])
    desired_pos = desired_sign * contracts

    delta = round(desired_pos - cur_pos, 8)
    min_step = 0.0  # lot size granularity already applied in `contracts`
    if abs(delta) < 1e-9:
        ledger["risk_state"] = risk_state
        return  # already positioned correctly, nothing to trade

    side = "buy" if delta > 0 else "sell"
    sz = abs(round(delta, 8))
    client.set_leverage(inst_id, cfg["leverage"], cfg["mgn_mode"])
    cl_ord_id = f"{book_id.replace('_','')[:20]}{datetime.now(timezone.utc).strftime('%m%d%H%M')}"
    resp = client.place_order(inst_id, cfg["mgn_mode"], side, sz, cl_ord_id=cl_ord_id)
    ord_id = resp["data"][0]["ordId"]
    detail = client.order_detail(inst_id, ord_id)
    trade_record = {
        "clOrdId": cl_ord_id,
        "ordId": ord_id,
        "instId": inst_id,
        "side": side,
        "sz_contracts": sz,
        "avgPx": detail.get("avgPx"),
        "signal_basis": f"trend_signal={signal_side}, dist_vs_sma20={diag['dist_pct']:.2f}%",
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "state": detail.get("state"),
        "reason": "risk_breach_flatten" if breach_reason else "rebalance_to_signal",
    }
    ledger.setdefault("trades", []).append(trade_record)
    ledger["risk_state"] = risk_state
    log(f"{book_id}: placed {side} {sz} {inst_id} -> {detail.get('state')} @ {detail.get('avgPx')}")


def monitor_book3(client, ledger):
    """book3 is buy-and-hold funding-carry: no signal flips, just log health."""
    pos_data = client.positions("BTC-USDT-SWAP")
    bal = client.balance()
    fr = client.funding_rate("BTC-USDT-SWAP")
    log(
        f"book3_crypto: BTC-SWAP pos={pos_data[0]['pos'] if pos_data else 'none'} "
        f"fundingRate={fr['fundingRate']} totalEq={bal['totalEq']}"
    )


def git_commit_and_push(message):
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return
    repo_dir = os.path.join(os.path.dirname(__file__), "..")
    subprocess.run(["git", "config", "user.email", "bot@parallel-books.local"], cwd=repo_dir)
    subprocess.run(["git", "config", "user.name", "parallel-books-bot"], cwd=repo_dir)
    subprocess.run(["git", "add", "ledgers/"], cwd=repo_dir)
    result = subprocess.run(["git", "commit", "-m", message], cwd=repo_dir)
    if result.returncode == 0:
        subprocess.run(["git", "push"], cwd=repo_dir)


def main():
    client = OKXClient()
    for book_id, cfg in BOOKS.items():
        path, ledger = load_ledger(book_id)
        try:
            if book_id == "book3_crypto":
                monitor_book3(client, ledger)
            elif cfg["tradeable"]:
                manage_book(client, book_id, cfg, ledger)
            else:
                log(f"{book_id}: not tradeable (no validated edge) -- skipped, staying flat")
        except OKXError as e:
            log(f"{book_id}: OKX API ERROR -- {e}")
        save_ledger(path, ledger)
    git_commit_and_push(f"auto: bot run {datetime.now(timezone.utc).isoformat()}")


if __name__ == "__main__":
    main()
