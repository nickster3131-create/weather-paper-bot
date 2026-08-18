#!/usr/bin/env python3
"""
Weather-bracket mispricing scanner -- PAPER TRADING ONLY.

Strategy replicated: the "near-certain harvest" mechanic seen in vip68 /
sailor82 / HighTempTation / Weatherstappen on Polymarket. Once a city's
actual reported high temperature for the day is known (or effectively
locked in), any temperature-bracket market whose correct outcome hasn't
fully repriced to ~100c is a candidate: buy the "No" side of brackets
that are now provably wrong, at whatever price under ~0.98 the book
still offers.

This script never places real orders and has no code path that can.
Every "buy" is a simulated fill at the observed book price, logged to
paper_trades.jsonl, so you can track hypothetical PnL before ever
risking real money.

This is NOT a secret edge. It is mechanical arithmetic, thin margins,
and discipline. Read the caveats below.

Two independent risks this script does NOT eliminate for you:
  1. Resolution-source mismatch. Polymarket resolves each market against
     a SPECIFIC station page (usually Wunderground). If the "actual temp"
     source you configure disagrees with that station, you can buy a
     "sure thing" that is not actually sure. VERIFY THE STATION MANUALLY
     for every city you trade before trusting this script's signal.
  2. Late revision. Reported highs get corrected sometimes. A bracket
     that looks dead at 3pm can flip by evening.

Data sources used (no API key required):
  - Gamma API (gamma-api.polymarket.com): market discovery
  - CLOB API (clob.polymarket.com): live order book / midpoint price
  - Open-Meteo (open-meteo.com): free current + historical weather,
    used ONLY as a sanity proxy -- cross-check against Polymarket's
    actual resolution source before trusting it for a city.
"""
import argparse
import time
import urllib.request
import urllib.parse
import json
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

PAPER_TRADE_LOG = "paper_trades.jsonl"

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
OPEN_METEO = "https://api.open-meteo.com/v1/forecast"

# Fill in lat/lon + IANA timezone + the Polymarket-recognized city name for
# each city you actually trade. Verify the resolution station manually on
# the market's own page before adding it here -- this file's job is just
# to help you not trade before the day's high is realistically locked in.
CITY_COORDS = {
    "Shanghai": (31.23, 121.47, "Asia/Shanghai"),
    "London": (51.51, -0.13, "Europe/London"),
    "New York City": (40.71, -74.01, "America/New_York"),
    "Miami": (25.76, -80.19, "America/New_York"),
    # added -- cities the verified profitable weather wallets actually trade
    "Hong Kong": (22.30, 114.17, "Asia/Hong_Kong"),
    "Paris": (48.85, 2.35, "Europe/Paris"),
    "Madrid": (40.42, -3.70, "Europe/Madrid"),
    "Amsterdam": (52.37, 4.90, "Europe/Amsterdam"),
    "Los Angeles": (34.05, -118.24, "America/Los_Angeles"),
    "Houston": (29.76, -95.37, "America/Chicago"),
    "Toronto": (43.65, -79.38, "America/Toronto"),
    "Seoul": (37.57, 126.98, "Asia/Seoul"),
    "Tokyo": (35.68, 139.65, "Asia/Tokyo"),
}

# Only treat a bracket as a real "dead outcome" signal once it's at least
# this late in LOCAL time for that city. Daily highs are usually reached
# mid-to-late afternoon; this is a blunt default, not a guarantee -- adjust
# per city/season, and prefer confirming against the actual reported high
# rather than trusting the clock alone.
MIN_LOCAL_HOUR_FOR_SIGNAL = 17

# Below this price, "No" on a dead bracket is still worth buying.
# Above it, the market has already repriced and there's no edge left.
MAX_BUY_PRICE = 0.97
MIN_BUY_PRICE = 0.50  # avoid brackets that aren't actually dead yet

MAX_ORDER_USD = 5.00    # simulated size per paper trade
STARTING_BANKROLL = 500.00  # imaginary paper-trading capital
BANKROLL_FILE = "paper_bankroll.json"


def http_get(url, params=None):
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "weather-mispricing-scanner/1.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def get_todays_temp_brackets(city):
    """Find today's open 'highest temperature in <city>' markets via Gamma search."""
    today = datetime.now(timezone.utc).strftime("%B %-d")  # e.g. "August 17"
    query = f"highest temperature in {city}"
    try:
        results = http_get(f"{GAMMA}/public-search", {"q": query, "limit_per_type": 10})
    except Exception as e:
        print(f"  [{city}] search error: {e}", file=sys.stderr)
        return []

    markets = []
    for event in results.get("events", []):
        if today.lower() not in event.get("title", "").lower():
            continue
        for m in event.get("markets", []):
            if m.get("closed"):
                continue
            markets.append({
                "title": m.get("slug", event.get("title")),
                "conditionId": m.get("conditionId"),
                "clobTokenIds": m.get("clobTokenIds"),
                "outcomes": m.get("outcomes"),
            })
    return markets


def get_order_book(token_id):
    try:
        book = http_get(f"{CLOB}/book", {"token_id": token_id})
    except Exception:
        return None
    asks = book.get("asks", [])
    if not asks:
        return None
    # sort cheapest first -- CLOB doesn't guarantee order
    asks = sorted(({"price": float(a["price"]), "size": float(a["size"])} for a in asks),
                   key=lambda a: a["price"])
    return asks


def get_current_price(token_id):
    """Best (cheapest) ask only -- for display/reference. Not what you'll
    actually pay for real size; use simulate_fill for that."""
    asks = get_order_book(token_id)
    return asks[0]["price"] if asks else None


def simulate_fill(asks, target_usd):
    """
    Walk the real order book depth level by level to estimate what you'd
    ACTUALLY pay to fill target_usd worth of an order, instead of assuming
    the whole thing fills at the best ask -- these weather brackets often
    have only a few dollars of depth at the top price, so a $5 order can
    walk through 3-4 price levels and get a materially worse average price
    than the quote you see first.

    Returns (avg_price, shares, filled_usd, fully_filled).
    """
    remaining_usd = target_usd
    shares = 0.0
    spent = 0.0
    for level in asks:
        level_usd = level["price"] * level["size"]
        take_usd = min(remaining_usd, level_usd)
        take_shares = take_usd / level["price"]
        shares += take_shares
        spent += take_usd
        remaining_usd -= take_usd
        if remaining_usd <= 1e-9:
            break
    fully_filled = remaining_usd <= 1e-9
    avg_price = spent / shares if shares > 0 else None
    return avg_price, shares, spent, fully_filled


def local_hour(city):
    """Current local hour in the city's timezone, or None if unconfigured."""
    if city not in CITY_COORDS:
        return None
    _, _, tzname = CITY_COORDS[city]
    return datetime.now(ZoneInfo(tzname)).hour


def past_signal_window(city):
    hour = local_hour(city)
    if hour is None:
        return False
    return hour >= MIN_LOCAL_HOUR_FOR_SIGNAL


def get_actual_temp(city):
    """Open-Meteo current temp -- SANITY PROXY ONLY. Cross-check against
    Polymarket's real resolution source before trusting this for a city."""
    if city not in CITY_COORDS:
        return None
    lat, lon, _ = CITY_COORDS[city]
    try:
        data = http_get(OPEN_METEO, {
            "latitude": lat, "longitude": lon,
            "current": "temperature_2m",
            "daily": "temperature_2m_max",
            "temperature_unit": "celsius",
            "timezone": "auto",
        })
        return {
            "current_c": data["current"]["temperature_2m"],
            "today_max_c": data["daily"]["temperature_2m_max"][0],
        }
    except Exception as e:
        print(f"  [{city}] open-meteo error: {e}", file=sys.stderr)
        return None


def scan_city(city):
    print(f"\n=== {city} ===")
    hour = local_hour(city)
    in_window = past_signal_window(city)
    if hour is not None:
        print(f"  Local time: {hour}:00 -- {'past' if in_window else 'BEFORE'} the "
              f"{MIN_LOCAL_HOUR_FOR_SIGNAL}:00 signal window.")
    if not in_window:
        print(f"  Too early in the day for this city -- the day's high isn't realistically "
              f"locked in yet. Skipping candidate flagging (still showing prices for reference).")

    actual = get_actual_temp(city)
    if actual:
        print(f"  Open-Meteo proxy: current {actual['current_c']}C, today's max so far {actual['today_max_c']}C")
        print(f"  ! VERIFY this against Polymarket's actual resolution-source station before trusting it.")

    markets = get_todays_temp_brackets(city)
    if not markets:
        print("  No open markets found for today (or search didn't match -- check city name).")
        return []

    signals = []
    for m in markets:
        token_ids = m.get("clobTokenIds")
        if not token_ids:
            continue
        try:
            token_ids = json.loads(token_ids) if isinstance(token_ids, str) else token_ids
        except Exception:
            continue
        outcomes = m.get("outcomes")
        try:
            outcomes = json.loads(outcomes) if isinstance(outcomes, str) else outcomes
        except Exception:
            outcomes = ["Yes", "No"]

        for tok, outcome in zip(token_ids, outcomes):
            price = get_current_price(tok)
            if price is None:
                continue
            price_ok = MIN_BUY_PRICE <= price <= MAX_BUY_PRICE
            flagged = price_ok and in_window
            marker = " <-- CANDIDATE" if flagged else (" (price ok, too early)" if price_ok else "")
            print(f"  {m['title'][:55]:<55} {outcome:<4} price={price:.3f}{marker}")
            if flagged:
                signals.append({"market": m, "outcome": outcome, "token_id": tok, "price": price, "city": city})
            time.sleep(0.15)
    return signals


def load_existing_positions():
    """Set of (conditionId, outcome) already paper-filled, so we don't
    re-buy the same bracket every hour it stays in the signal window."""
    positions = set()
    try:
        with open(PAPER_TRADE_LOG) as f:
            for line in f:
                rec = json.loads(line)
                positions.add((rec.get("conditionId"), rec.get("outcome")))
    except FileNotFoundError:
        pass
    return positions


def load_bankroll():
    try:
        with open(BANKROLL_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {"cash": STARTING_BANKROLL, "committed": 0.0}


def save_bankroll(state):
    with open(BANKROLL_FILE, "w") as f:
        json.dump(state, f, indent=2)


def paper_fill(signal, bankroll):
    """
    Simulated fill using REAL order-book depth, not the quoted top price.
    Re-fetches the book fresh (price/depth may have moved since the scan)
    and walks it level by level via simulate_fill -- these weather brackets
    are often thin, so a $5 order can walk through several price levels
    and get a materially worse average fill than the first quote showed.
    Records the actual simulated fill to PAPER_TRADE_LOG and debits the
    imaginary bankroll. No wallet, no key, no network call to any trading
    endpoint -- this function cannot place a real order.
    """
    target_usd = min(MAX_ORDER_USD, bankroll["cash"])
    if target_usd <= 0:
        print(f"  [SKIPPED] Bankroll exhausted (${bankroll['cash']:.2f} left) -- not paper-filling {signal['market']['title'][:40]}")
        return

    asks = get_order_book(signal["token_id"])
    if not asks:
        print(f"  [SKIPPED] No live order book for {signal['market']['title'][:40]} (may have moved/closed since scan)")
        return

    avg_price, shares, filled_usd, fully_filled = simulate_fill(asks, target_usd)
    if avg_price is None or filled_usd < 0.01:
        print(f"  [SKIPPED] No fillable depth for {signal['market']['title'][:40]}")
        return

    if avg_price > MAX_BUY_PRICE:
        print(f"  [SKIPPED] Thin book pushed avg fill to {avg_price:.3f} (quoted {signal['price']:.3f}), "
              f"above MAX_BUY_PRICE {MAX_BUY_PRICE} -- edge gone after slippage, not filling.")
        return

    slippage = avg_price - signal["price"]
    partial_note = f" (PARTIAL -- only ${filled_usd:.2f} of ${target_usd:.2f} had depth)" if not fully_filled else ""

    bankroll["cash"] -= filled_usd
    bankroll["committed"] += filled_usd
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "city": signal["city"],
        "market": signal["market"]["title"],
        "conditionId": signal["market"].get("conditionId"),
        "outcome": signal["outcome"],
        "quoted_price": signal["price"],
        "fill_price": round(avg_price, 4),
        "slippage": round(slippage, 4),
        "size_usd": round(filled_usd, 2),
        "shares": round(shares, 4),
        "fully_filled": fully_filled,
        "status": "PAPER_FILL",
    }
    with open(PAPER_TRADE_LOG, "a") as f:
        f.write(json.dumps(record) + "\n")
    print(f"  [PAPER FILL] BUY {signal['outcome']} on {signal['market']['title'][:50]} "
          f"quoted={signal['price']:.3f} actual_avg={avg_price:.3f} (slippage {slippage:+.3f}) "
          f"x {round(shares,4)} shares (${filled_usd:.2f}){partial_note} -- "
          f"bankroll cash remaining: ${bankroll['cash']:.2f}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cities", nargs="+", default=list(CITY_COORDS.keys()))
    args = ap.parse_args()

    all_signals = []
    for city in args.cities:
        signals = scan_city(city)
        all_signals.extend(signals)

    print(f"\n{'='*60}")
    print(f"{len(all_signals)} candidate mispricings found across {len(args.cities)} cities "
          f"(only counting cities past their local signal window).")

    if not all_signals:
        return

    existing = load_existing_positions()
    new_signals = []
    for sig in all_signals:
        key = (sig["market"].get("conditionId"), sig["outcome"])
        if key in existing:
            print(f"  [SKIP -- already hold this position] {sig['city']}: {sig['market']['title'][:45]} {sig['outcome']}")
        else:
            new_signals.append(sig)

    if not new_signals:
        print("\nAll candidates already have open paper positions from a prior run -- nothing new to fill.")
        return

    bankroll = load_bankroll()
    print(f"\nBankroll before this run: ${bankroll['cash']:.2f} cash, "
          f"${bankroll['committed']:.2f} committed to open paper positions.")
    for sig in new_signals:
        paper_fill(sig, bankroll)
    save_bankroll(bankroll)

    print(f"\nAll fills above are SIMULATED and logged to {PAPER_TRADE_LOG}. "
          f"No real order was placed -- this script has no code path to do so. "
          f"Bankroll after this run: ${bankroll['cash']:.2f} cash left of ${STARTING_BANKROLL:.2f} starting.")


if __name__ == "__main__":
    main()
