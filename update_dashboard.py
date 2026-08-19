#!/usr/bin/env python3
"""
Regenerates the dynamic parts of dashboard.html from paper_bankroll.json and
paper_trades.jsonl. Run after weather_mispricing_bot.py on each scan so the
dashboard reflects the latest simulated fills without any manual editing.

Resolution lookups (win/loss on closed markets) reuse
score_paper_trades.get_market_resolution and are best-effort -- a network
hiccup just leaves that trade counted as pending for this pass.

Never places or touches real orders; only rewrites dashboard.html in place
between its <!-- ...:START/END --> markers.
"""
import html
import json
import re
import sys
from datetime import datetime, timezone

from weather_mispricing_bot import CITY_COORDS, local_hour, past_signal_window, http_get, GAMMA, CLOB
from score_paper_trades import get_market_resolution, STARTING_BANKROLL

DASHBOARD_FILE = "dashboard.html"
TRADES_FILE = "paper_trades.jsonl"
BANKROLL_FILE = "paper_bankroll.json"


def load_trades():
    try:
        with open(TRADES_FILE) as f:
            return [json.loads(line) for line in f if line.strip()]
    except FileNotFoundError:
        return []


def load_bankroll():
    try:
        with open(BANKROLL_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {"cash": STARTING_BANKROLL, "committed": 0.0}


def get_live_price(condition_id, outcome):
    """Current best bid for a still-open position -- what you'd get selling
    right now. None if the market/outcome can't be found or has no bids."""
    try:
        markets = http_get(f"{GAMMA}/markets", {"condition_ids": condition_id})
        if not markets:
            return None
        m = markets[0]
        outcomes = json.loads(m["outcomes"])
        tokens = json.loads(m["clobTokenIds"])
        idx = outcomes.index(outcome)
        token = tokens[idx]
        book = http_get(f"{CLOB}/book", {"token_id": token})
        bids = book.get("bids", [])
        if not bids:
            return None
        return max(float(b["price"]) for b in bids)
    except Exception:
        return None


def score_trades(trades):
    """Best-effort resolution lookup per trade. Returns
    (rows, resolved, wins, losses, pending_usd, realized_pnl, resolved_stake_usd).
    resolved_stake_usd is the original capital in resolved positions -- it must
    be added back to total_value along with realized_pnl, since a resolved
    trade's stake isn't sitting in pending_usd (open only) or cash (never
    auto-credited back by paper_fill on resolution)."""
    rows = []
    resolved = wins = losses = 0
    pending_usd = 0.0
    realized_pnl = 0.0
    resolved_stake_usd = 0.0
    for t in trades:
        status = "PENDING"
        try:
            res = get_market_resolution(t["conditionId"])
        except Exception:
            res = None
        if res is not None:
            final_price = float(res.get(t["outcome"], 0))
            pnl = (final_price - t["fill_price"]) * t["shares"]
            realized_pnl += pnl
            resolved_stake_usd += t["size_usd"]
            resolved += 1
            if final_price > 0.5:
                wins += 1
                status = "WIN"
            else:
                losses += 1
                status = "LOSS"
        else:
            pending_usd += t["size_usd"]
        live_price = get_live_price(t["conditionId"], t["outcome"]) if status == "PENDING" else None
        rows.append((t, status, live_price))
    return rows, resolved, wins, losses, pending_usd, realized_pnl, resolved_stake_usd


BRACKET_RE = re.compile(r"^highest-temperature-in-.+?-on-[a-z]+-\d{1,2}-\d{4}-(.+)$", re.IGNORECASE)


def bracket_label(t):
    m = BRACKET_RE.match(t["market"])
    return m.group(1) if m else t["market"]


def render_city_cards():
    lines = []
    for city in CITY_COORDS:
        hour = local_hour(city)
        time_str = f"{hour}:00" if hour is not None else "--:--"
        if past_signal_window(city):
            pill = '<span class="pill open"><span class="pill-dot"></span>past window</span>'
        else:
            pill = '<span class="pill pending"><span class="pill-dot"></span>before window</span>'
        lines.append(
            f'    <div class="city-card"><div class="name">{html.escape(city)}</div>'
            f'<div class="local-time">{time_str}</div>{pill}</div>'
        )
    return "\n".join(lines)


def render_fills_content(rows):
    if not rows:
        return (
            '  <div class="empty-state" id="fills-empty">\n'
            '    <div class="big">&mdash;</div>\n'
            "    No paper fills yet. Every scanned city has been outside its post-5pm-local "
            "signal window on every hourly check so far -- once a bracket clears that window "
            "with real mispricing, it'll show up here with quoted price, actual fill price "
            "after order-book depth, and slippage.\n"
            "  </div>"
        )

    rows_sorted = sorted(rows, key=lambda r: r[0]["timestamp"], reverse=True)
    trs = []
    for t, status, live_price in rows_sorted:
        ts = datetime.fromisoformat(t["timestamp"]).strftime("%m-%d %H:%M UTC")
        status_class = {"WIN": "pos", "LOSS": "neg", "PENDING": "flat"}[status]
        if status == "PENDING":
            if live_price is not None:
                delta_cents = (live_price - t["fill_price"]) * 100
                delta_class = "pos" if delta_cents > 0 else ("neg" if delta_cents < 0 else "flat")
                move_cell = f'<td class="{delta_class}">{delta_cents:+.0f}&cent;</td>'
            else:
                move_cell = '<td>&mdash;</td>'
        else:
            # resolved -- realized move vs fill, to 0c or 100c
            final = 1.0 if status == "WIN" else 0.0
            delta_cents = (final - t["fill_price"]) * 100
            delta_class = "pos" if delta_cents > 0 else ("neg" if delta_cents < 0 else "flat")
            move_cell = f'<td class="{delta_class}">{delta_cents:+.0f}&cent;</td>'
        trs.append(
            "        <tr>"
            f"<td>{ts}</td><td>{html.escape(t['city'])}</td><td>{html.escape(bracket_label(t))}</td>"
            f"<td>{html.escape(t['outcome'])}</td><td>{t['fill_price']*100:.0f}&cent;</td>"
            f"{move_cell}"
            f"<td>${t['size_usd']:.2f}</td><td class=\"{status_class}\">{status}</td></tr>"
        )
    table_rows = "\n".join(trs)
    return (
        '  <div class="table-wrap" id="fills-table-wrap">\n'
        '    <table id="fills-table">\n'
        "      <thead>\n"
        "        <tr>\n"
        "          <th>Time</th><th>City</th><th>Bracket</th><th>Side</th>\n"
        "          <th>Fill</th><th>Move</th><th>Size</th><th>Status</th>\n"
        "        </tr>\n"
        "      </thead>\n"
        f'      <tbody id="fills-tbody">\n{table_rows}\n      </tbody>\n'
        "    </table>\n"
        "  </div>"
    )


def replace_id_text(html_src, elem_id, new_text):
    pattern = re.compile(r'(id="' + re.escape(elem_id) + r'">)[^<]*(</)')
    new_src, n = pattern.subn(lambda m: m.group(1) + new_text + m.group(2), html_src, count=1)
    if n == 0:
        raise RuntimeError(f"could not find id={elem_id!r} in {DASHBOARD_FILE}")
    return new_src


def replace_marker_block(html_src, marker, new_inner):
    pattern = re.compile(
        r"<!-- " + re.escape(marker) + r":START -->.*?<!-- " + re.escape(marker) + r":END -->",
        re.DOTALL,
    )
    replacement = f"<!-- {marker}:START -->\n{new_inner}\n<!-- {marker}:END -->"
    new_src, n = pattern.subn(lambda m: replacement, html_src, count=1)
    if n == 0:
        raise RuntimeError(f"could not find marker {marker} in {DASHBOARD_FILE}")
    return new_src


def main():
    trades = load_trades()
    bankroll = load_bankroll()
    rows, resolved, wins, losses, pending_usd, realized_pnl, resolved_stake_usd = score_trades(trades)

    # cash already had every trade's stake debited at fill time (win or lose),
    # and paper_fill never credits winnings back to cash on resolution -- so
    # a resolved trade's original stake must be added back here, or it just
    # vanishes from total_value (counted in neither cash nor pending_usd).
    total_value = bankroll["cash"] + pending_usd + resolved_stake_usd + realized_pnl
    pct = (total_value / STARTING_BANKROLL - 1) * 100

    with open(DASHBOARD_FILE) as f:
        src = f.read()

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    src = replace_id_text(src, "updated-at", f"Snapshot -- {now}")

    value_class = "pos" if pct > 0 else ("neg" if pct < 0 else "flat")
    src = re.sub(
        r'(id="bankroll-value">)[^<]*(</)',
        lambda m: f"{m.group(1)}${total_value:,.2f}{m.group(2)}",
        src, count=1,
    )
    src = re.sub(
        r'class="value \w+" id="bankroll-value"',
        f'class="value {value_class}" id="bankroll-value"',
        src, count=1,
    )
    src = replace_id_text(src, "bankroll-sub", f"started at ${STARTING_BANKROLL:.2f} -- {pct:+.1f}%")
    src = replace_id_text(src, "open-count", str(len(trades) - resolved))
    src = replace_id_text(src, "open-sub", f"${pending_usd:.2f} committed")
    src = replace_id_text(src, "resolved-count", str(resolved))
    src = replace_id_text(src, "resolved-sub", f"{wins} wins -- {losses} losses")
    src = replace_id_text(src, "cities-count", f"{len(CITY_COORDS)} tracked")
    src = replace_id_text(src, "fills-count", str(len(trades)))

    src = replace_marker_block(src, "CITY_CARDS", render_city_cards())
    src = replace_marker_block(src, "FILLS_CONTENT", render_fills_content(rows))

    with open(DASHBOARD_FILE, "w") as f:
        f.write(src)

    print(f"dashboard.html updated: {len(trades)} fills ({resolved} resolved, "
          f"{wins}W/{losses}L), equity ${total_value:,.2f} ({pct:+.1f}%)")


if __name__ == "__main__":
    main()
