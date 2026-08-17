#!/usr/bin/env python3
"""
Scores paper_trades.jsonl against real market resolutions, so you can see
hypothetical PnL from the weather_mispricing_bot.py signals without ever
having risked real money. Run this a day or two after fills accumulate,
once markets have actually resolved.
"""
import json
import urllib.request
import sys

GAMMA = "https://gamma-api.polymarket.com"
LOG = "paper_trades.jsonl"
BANKROLL_FILE = "paper_bankroll.json"
STARTING_BANKROLL = 500.00


def get_market_resolution(condition_id):
    req = urllib.request.Request(
        f"{GAMMA}/markets?condition_ids={condition_id}",
        headers={"User-Agent": "paper-trade-scorer/1.0"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read())
    if not data:
        return None
    m = data[0]
    if not m.get("closed"):
        return None
    prices = json.loads(m.get("outcomePrices", "[]"))
    outcomes = json.loads(m.get("outcomes", "[]"))
    return dict(zip(outcomes, prices))


def main():
    try:
        with open(LOG) as f:
            trades = [json.loads(line) for line in f]
    except FileNotFoundError:
        print(f"No {LOG} yet -- run weather_mispricing_bot.py first to generate paper fills.")
        sys.exit(0)

    total_pnl = 0.0
    resolved_count = 0
    pending_count = 0
    committed_pending = 0.0

    print(f"{'city':<15}{'outcome':<6}{'fill':>7}{'result':>10}{'pnl($)':>10}")
    for t in trades:
        res = get_market_resolution(t["conditionId"])
        if res is None:
            pending_count += 1
            committed_pending += t["size_usd"]
            continue
        final_price = float(res.get(t["outcome"], 0))
        pnl = (final_price - t["fill_price"]) * t["shares"]
        total_pnl += pnl
        resolved_count += 1
        result = "WIN" if final_price > 0.5 else "LOSS"
        print(f"{t['city']:<15}{t['outcome']:<6}{t['fill_price']:>7.3f}{result:>10}{pnl:>10.2f}")

    print(f"\n{resolved_count} resolved, {pending_count} still pending "
          f"(${committed_pending:.2f} committed to unresolved positions).")
    print(f"Total simulated PnL on resolved trades: ${total_pnl:,.2f}")

    slippages = [t["slippage"] for t in trades if "slippage" in t]
    if slippages:
        avg_slip = sum(slippages) / len(slippages)
        print(f"Avg slippage vs quoted price at scan time: {avg_slip:+.4f} "
              f"({sum(1 for s in slippages if s > 0)}/{len(slippages)} fills paid worse than quoted)")

    try:
        with open(BANKROLL_FILE) as f:
            bankroll = json.load(f)
        cash = bankroll["cash"]
    except FileNotFoundError:
        cash = STARTING_BANKROLL

    current_value = cash + committed_pending + total_pnl
    print(f"\nBankroll: started ${STARTING_BANKROLL:.2f} -> "
          f"cash ${cash:.2f} + pending ${committed_pending:.2f} + realized PnL ${total_pnl:.2f} "
          f"= ${current_value:.2f} total ({(current_value/STARTING_BANKROLL - 1)*100:+.1f}%)")


if __name__ == "__main__":
    main()
