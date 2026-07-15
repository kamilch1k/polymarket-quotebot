#!/usr/bin/env python3
"""
Polymarket LP-rewards paper-trading bot + local dashboard.

PHASE 0 — PAPER ONLY. This file contains NO order-placement code, NO key
handling, NO signing: grep it. It watches real reward-paying markets, holds
*virtual* two-sided quotes, simulates fills against the real public trade
tape (conservatively: we assume we are LAST in queue at our price), accrues
simulated liquidity rewards by the published scoring formula, and measures
the number that decides whether phase 1 (live micro quoting) is worth it:

    net yield = rewards + captured spread - adverse selection (markouts)

Strategy being tested = what the smooth-curve LP accounts actually do:
two-sided resting quotes on SLOW, long-horizon markets (politics/macro),
never sports, never in-play. Structurally disjoint from the copybot by
construction (it only quotes markets ending >= MIN_DAYS_OUT away and skips
sporty titles — the copybot trades the exact opposite).

RUN
  python quotebot.py --check     offline self-test
  python quotebot.py             dashboard at http://127.0.0.1:8778
"""
import html
import json
import math
import os
import re
import sys
import threading
import time
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

try:
    import requests
except ImportError:  # self-heal: install with THIS interpreter, then retry —
    import subprocess  # a broken env must not strand the paper run on a banner
    subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "requests"],
                   timeout=600, check=False)
    import requests

# ---- config (defaults; editable in the page) ---------------------------------
LIVE = False             # PHASE 1 master switch. Off = pure paper (default).
                         # On + funder + key = real GTC quotes, hard-capped below.
FUNDER = ""              # the quotebot's OWN wallet — never the copybot's
LIVE_BANKROLL = 100.0    # hard cap: total live order notional + inventory cost
KILL_END_DAYS = 7.0      # cancel + stop quoting a market this close to its end
KILL_MOVE_C = 3.0        # mid moved this many cents in 10 min = news → cancel,
                         # pause that market 30 min (stale quotes get picked off)
SIG_TYPE = 3             # Polymarket wallet signature type (3 = 2026 wallets)
PAPER_BANKROLL = 400.0   # virtual capital the simulation is allowed to deploy
N_MARKETS = 4            # how many reward markets to quote at once
QUOTE_DIST_C = 2.0       # rest quotes this many cents off mid (closer = more
                         # reward score, quadratically — and more fill risk)
REQUOTE_C = 0.5          # re-center quotes when mid moved this many cents
POLL_SECONDS = 8
MIN_DAYS_OUT = 14        # only long-horizon markets (the whole thesis) — also
                         # the structural no-overlap guard vs the copybot
MARKOUT_MIN = 30         # minutes after a fill at which adverse selection is scored
INV_CAP_X = 3.0          # max |inventory| per market, in multiples of one quote's size
REWARD_CAL = 1.0         # rewards_daily_rate is assumed $/day per unit; phase 1
                         # calibrates this against a real payout — until then
                         # the SHARE column is exact, the $ column is share×rate×CAL
SCAN_EVERY_H = 6.0       # re-scan the market universe this often
PORT = 8778
HEADLESS = False

APP_DIR = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
CONFIG_FILE = APP_DIR / "quotebot_config.json"
STATE_FILE = APP_DIR / "quotebot_state.json"
DAILY_FILE = APP_DIR / "quotebot_daily.jsonl"
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
DATA_API = "https://data-api.polymarket.com"

SPORTY = (" vs", "o/u", "spread:", "win on", "1st half", "team to advance", "(bo",
          "end in a draw", "world cup", "wimbledon", "grand slam", "f1", "ufc",
          "nba", "nfl", "mlb", "nhl")

LOCK = threading.Lock()
STATE = {
    "started": time.time(),   # this process
    "born": time.time(),      # the dataset (persisted — survives relaunches)
    "last_poll": 0.0, "log": deque(maxlen=200),
    "markets": {},        # cid -> live market card (quotes, share, fills…)
    "watched": [],        # chosen market universe (persisted — restarts resume, not rescan)
    "fills": [],          # persisted paper fills (with markouts filled in later)
    "rewards_usd": 0.0,   # accrued simulated rewards (share × rate × CAL)
    "spread_pnl": 0.0,    # realized round-trip P&L (avg-cost)
    "day_start": 0.0,     # epoch of the current rollup day
    "scan_note": "", "last_scan": 0.0,
}


def logline(**e):
    e["t"] = time.strftime("%H:%M:%S")
    with LOCK:
        STATE["log"].appendleft(e)


# ---- pure math (unit-tested in --check) ---------------------------------------
def order_score(size, dist_c, max_spread_c, min_size):
    """Published LP scoring shape: size × ((S−d)/S)² inside the band, 0 outside.
    Qualification needs size ≥ the market's min_size. docs.polymarket.com,
    'Liquidity rewards' — absolute payout calibrated in phase 1."""
    if size < min_size or dist_c > max_spread_c or max_spread_c <= 0:
        return 0.0
    w = (max_spread_c - dist_c) / max_spread_c
    return size * w * w


def two_sided(q_bid, q_ask):
    """Reward credit is the two-sided minimum — one-sided quoting earns nothing
    (conservative reading of the docs; phase 1 validates)."""
    return min(q_bid, q_ask)


def book_score(levels, mid, max_spread_c, min_size):
    """Total qualifying score already resting in one side of the real book.
    Levels = [(price, size), …]. Aggregate book can't be split per maker, so
    this is the denominator approximation the README documents."""
    s = 0.0
    for price, size in levels:
        d = abs(price - mid) * 100.0
        s += order_score(size, d, max_spread_c, max(min_size, 0.0))
    return s


def our_share(our_q, book_q):
    """Our slice of the per-minute reward pie. Book score excludes us (we are
    virtual), so the denominator adds us in."""
    tot = our_q + book_q
    return our_q / tot if tot > 0 else 0.0


def yes_print(asset, price, size, side, yes_tid):
    """Normalize a tape print onto the YES token: a NO buy of s @ p is
    economically a YES sell of s @ 1−p."""
    if asset == yes_tid:
        return side.upper(), float(price), float(size)
    return ("SELL" if side.upper() == "BUY" else "BUY"), 1.0 - float(price), float(size)


def merged_yes_book(book_yes, book_no):
    """Both outcome books folded onto the YES token: a NO bid at p is a YES ask
    at 1−p and vice versa. Competitors resting on the complement are invisible
    to a single-book reading — merging stops the share estimate flattering us."""
    bids = [(float(x["price"]), float(x["size"])) for x in book_yes.get("bids", [])]
    asks = [(float(x["price"]), float(x["size"])) for x in book_yes.get("asks", [])]
    bids += [(round(1 - float(x["price"]), 4), float(x["size"])) for x in book_no.get("asks", [])]
    asks += [(round(1 - float(x["price"]), 4), float(x["size"])) for x in book_no.get("bids", [])]
    return bids, asks


def fill_against(quote_px, quote_sz, side, print_side, print_px, print_sz):
    """Conservative queue model: our resting order fills ONLY when a real print
    crosses STRICTLY through our price (at-price prints are assumed to have
    filled the real book ahead of us — we are last in queue). Returns filled
    shares."""
    if quote_sz <= 0:
        return 0.0
    if side == "BUY" and print_side == "SELL" and print_px < quote_px:
        return min(quote_sz, print_sz)
    if side == "SELL" and print_side == "BUY" and print_px > quote_px:
        return min(quote_sz, print_sz)
    return 0.0


def sporty(title):
    t = (title or "").lower()
    return any(k in t for k in SPORTY)


def unreal(pos, mid):
    """Mark-to-mid P&L of the open (possibly short) paper position."""
    return pos["sh"] * (mid - pos["cost"]) if pos["sh"] else 0.0


def quote_sizes(sh, cap_sh, base):
    """Inventory-aware arming: a side that would grow |inventory| past the cap
    stays dark. Without this, paper inventory compounds one direction and the
    experiment measures directional luck instead of LP economics."""
    bid = base if sh < cap_sh else 0.0
    ask = base if -sh < cap_sh else 0.0
    return bid, ask


def avg_cost_pnl(pos, px, sz, side):
    """Average-cost ledger step. pos = {'sh': signed shares, 'cost': avg price}.
    Returns realized P&L for the closing part of this fill and mutates pos."""
    realized = 0.0
    signed = sz if side == "BUY" else -sz
    if pos["sh"] * signed >= 0:  # extending the same direction
        tot = abs(pos["sh"]) + sz
        pos["cost"] = (pos["cost"] * abs(pos["sh"]) + px * sz) / tot if tot else 0.0
        pos["sh"] += signed
        return 0.0
    close = min(abs(pos["sh"]), sz)  # closing against the position
    realized = close * (px - pos["cost"]) * (1 if pos["sh"] > 0 else -1)
    if side == "SELL":
        realized = close * (px - pos["cost"])
    else:
        realized = close * (pos["cost"] - px)
    pos["sh"] += signed
    if pos["sh"] * signed > 0:  # flipped through zero: remainder opens anew
        pos["cost"] = px
    elif pos["sh"] == 0:
        pos["cost"] = 0.0
    return realized


# ---- persistence ---------------------------------------------------------------
def load_config():
    global PRIVATE_KEY_MEM
    if not CONFIG_FILE.exists():
        return
    c = json.loads(CONFIG_FILE.read_text())
    for k in ("paper_bankroll", "n_markets", "quote_dist_c", "requote_c",
              "poll_seconds", "min_days_out", "reward_cal",
              "live", "funder", "live_bankroll", "kill_end_days", "kill_move_c", "sig_type"):
        if k in c:
            globals()[k.upper()] = type(globals()[k.upper()])(c[k])
    k = c.get("private_key") or ""
    migrated = bool(k) and _vault_save(k)  # same discipline as the copybot
    k = k or _vault_load()
    if k:
        PRIVATE_KEY_MEM = k
    if migrated:
        save_config()


def save_config():
    pk_out = "" if (PRIVATE_KEY_MEM and _vault_save(PRIVATE_KEY_MEM)) else (PRIVATE_KEY_MEM or "")
    CONFIG_FILE.write_text(json.dumps({
        "paper_bankroll": PAPER_BANKROLL, "n_markets": N_MARKETS,
        "quote_dist_c": QUOTE_DIST_C, "requote_c": REQUOTE_C,
        "poll_seconds": POLL_SECONDS, "min_days_out": MIN_DAYS_OUT,
        "reward_cal": REWARD_CAL,
        "live": LIVE, "funder": FUNDER, "live_bankroll": LIVE_BANKROLL,
        "kill_end_days": KILL_END_DAYS, "kill_move_c": KILL_MOVE_C,
        "sig_type": SIG_TYPE, "private_key": pk_out}, indent=2))


def load_state():
    if not STATE_FILE.exists():
        return
    s = json.loads(STATE_FILE.read_text())
    for k in ("fills", "rewards_usd", "spread_pnl", "day_start", "born",
              "watched", "last_scan"):
        STATE[k] = s.get(k, STATE[k])
    for cid, m in s.get("markets", {}).items():
        STATE["markets"][cid] = m


def save_state():
    with LOCK:
        data = {"fills": STATE["fills"][-500:], "rewards_usd": STATE["rewards_usd"],
                "spread_pnl": STATE["spread_pnl"], "day_start": STATE["day_start"],
                "born": STATE["born"], "markets": STATE["markets"],
                "watched": STATE["watched"], "last_scan": STATE["last_scan"]}
    STATE_FILE.write_text(json.dumps(data))


def daily_rollup():
    """Once per UTC day: append the day's numbers to the jsonl the write-up
    will be built from, then reset the day counters (cumulative stays)."""
    now = time.time()
    with LOCK:
        if STATE["day_start"] == 0.0:
            STATE["day_start"] = now
            return
        if now - STATE["day_start"] < 86400:
            return
        marks = sum(m.get("inv_value", 0.0) for m in STATE["markets"].values())
        row = {"date": time.strftime("%Y-%m-%d", time.gmtime(STATE["day_start"])),
               "rewards_usd": round(STATE["rewards_usd"], 4),
               "spread_pnl": round(STATE["spread_pnl"], 4),
               "inventory_marks": round(marks, 2),
               "fills": len(STATE["fills"]),
               "bankroll": PAPER_BANKROLL}
        STATE["day_start"] = now
    with DAILY_FILE.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")
    logline(kind="live", note=f"daily rollup written: {row['date']}")


# ---- market universe ------------------------------------------------------------
def jget(url, **params):
    r = requests.get(url, params=params, timeout=15)
    r.raise_for_status()
    return r.json()


def scan_universe():
    """Rank open reward-paying markets by expected share×rate for OUR size,
    long-horizon + non-sports only. Returns the chosen list of market dicts."""
    out, seen = [], set()
    for offset in (0, 250):
        try:
            rows = jget(f"{GAMMA}/markets", closed="false", limit=250, offset=offset,
                        order="volume24hr", ascending="false")
        except Exception as ex:
            logline(kind="error", note=f"universe scan failed: {str(ex)[:90]}")
            return None
        for m in rows:
            try:
                cid = m.get("conditionId")
                if not cid or cid in seen:
                    continue
                seen.add(cid)
                min_sz = float(m.get("rewardsMinSize") or 0)
                max_sp = float(m.get("rewardsMaxSpread") or 0)
                if min_sz <= 0 or max_sp <= 0 or sporty(m.get("question")):
                    continue
                rate = max((float(r.get("rewardsDailyRate") or 0)
                            for r in (m.get("clobRewards") or [])), default=0.0)
                if rate <= 0:
                    continue
                end = (m.get("endDate") or "").replace("Z", "+00:00")
                if not end:
                    continue
                from datetime import datetime
                days = (datetime.fromisoformat(end).timestamp() - time.time()) / 86400
                if days < MIN_DAYS_OUT:
                    continue
                tids = json.loads(m.get("clobTokenIds") or "[]")
                if len(tids) != 2:
                    continue
                bid, ask = float(m.get("bestBid") or 0), float(m.get("bestAsk") or 0)
                if not (0 < bid < ask < 1):
                    continue
                mid = (bid + ask) / 2
                # do we even qualify? per-side budget must cover min_size shares
                side_usd = PAPER_BANKROLL / max(N_MARKETS, 1) / 2
                if side_usd / mid < min_sz:
                    continue
                out.append({"cid": cid, "tid": tids[0], "tid_no": tids[1],
                            "q": m.get("question"),
                            "rate": rate, "min_size": min_sz, "max_spread": max_sp,
                            "mid": mid, "days": round(days, 1),
                            "end_ts": time.time() + days * 86400})
            except Exception:
                continue
    # expected share for our size, against the live book (top candidates only)
    ranked = []
    for m in sorted(out, key=lambda m: -m["rate"])[:12]:
        try:
            bids, asks = merged_yes_book(jget(f"{CLOB}/book", token_id=m["tid"]),
                                         jget(f"{CLOB}/book", token_id=m["tid_no"]))
            mid = m["mid"]
            side_usd = PAPER_BANKROLL / max(N_MARKETS, 1) / 2
            sz = side_usd / mid
            ours = two_sided(order_score(sz, QUOTE_DIST_C, m["max_spread"], m["min_size"]),
                             order_score(sz, QUOTE_DIST_C, m["max_spread"], m["min_size"]))
            bookq = two_sided(book_score(bids, mid, m["max_spread"], m["min_size"]),
                              book_score(asks, mid, m["max_spread"], m["min_size"]))
            share = our_share(ours, bookq)
            ranked.append({**m, "share": share, "exp_usd": share * m["rate"] * REWARD_CAL})
            time.sleep(0.15)
        except Exception:
            continue
    ranked.sort(key=lambda m: -m["exp_usd"])
    final, seenq = [], set()
    for m in ranked:  # one variant per market family — twins ("$80 in July" /
        key = (m["q"] or "")[:24].lower()  # "$85 in July") are correlated data
        if key in seenq:
            continue
        seenq.add(key)
        final.append(m)
    return final[:N_MARKETS]


# ---- the paper quoting engine -----------------------------------------------------
def market_card(cid):
    with LOCK:
        return STATE["markets"].setdefault(cid, {
            "pos": {"sh": 0.0, "cost": 0.0}, "fills": 0, "requotes": 0,
            "qualified_ticks": 0, "ticks": 0, "seen": [], "inv_value": 0.0})


def poll_market(m):
    """One pass over one market: refresh book/mid, (re)place virtual quotes,
    replay new tape prints against them, accrue reward share."""
    card = market_card(m["cid"])
    try:
        book_yes = jget(f"{CLOB}/book", token_id=m["tid"])
        book_no = jget(f"{CLOB}/book", token_id=m["tid_no"]) if m.get("tid_no") else {}
        bids, asks = merged_yes_book(book_yes, book_no)
        if not bids or not asks:
            return
        mid = (max(p for p, _ in bids) + min(p for p, _ in asks)) / 2
    except Exception as ex:
        logline(kind="error", note=f"book poll failed {m['q'][:30]}: {str(ex)[:60]}")
        return

    side_usd = PAPER_BANKROLL / max(N_MARKETS, 1) / 2
    d = QUOTE_DIST_C / 100.0
    want_bid, want_ask = round(mid - d, 3), round(mid + d, 3)
    if "bid" not in card or abs(mid - card.get("mid_at_quote", 0)) * 100 >= REQUOTE_C:
        if "bid" in card:
            card["requotes"] += 1
        card["bid"], card["ask"] = want_bid, want_ask
        base = round(side_usd / mid, 1)
        card["bid_sz"], card["ask_sz"] = quote_sizes(card["pos"]["sh"], INV_CAP_X * base, base)
        card["mid_at_quote"] = mid
    card["mid"] = mid
    hist = card.setdefault("mid_hist", [])
    hist.append((time.time(), mid))
    del hist[:-90]  # ~12 min at the default poll — enough for the 10-min move detector

    # tape replay (both outcome tokens, normalized onto YES)
    try:
        tape = jget(f"{DATA_API}/trades", market=m["cid"], limit=50)
    except Exception:
        tape = []
    seen = set(card["seen"])
    for t in tape if isinstance(tape, list) else []:
        k = f'{t.get("transactionHash")}:{t.get("asset")}:{t.get("side")}:{t.get("size")}'
        if k in seen or float(t.get("timestamp") or 0) < STATE["started"]:
            continue
        seen.add(k)
        pside, ppx, psz = yes_print(t.get("asset"), t.get("price"), t.get("size"),
                                    t.get("side"), m["tid"])
        for side, qpx, qsz_key in (("BUY", card["bid"], "bid_sz"), ("SELL", card["ask"], "ask_sz")):
            got = fill_against(qpx, card.get(qsz_key, 0), side, pside, ppx, psz)
            if got <= 0:
                continue
            realized = avg_cost_pnl(card["pos"], qpx, got, side)
            with LOCK:
                STATE["spread_pnl"] += realized
                STATE["fills"].append({"t": time.time(), "cid": m["cid"], "q": m["q"][:60],
                                       "side": side, "px": qpx, "sz": round(got, 1),
                                       "mid": mid, "markout": None})
            card["fills"] += 1
            card[qsz_key] = 0.0  # that side is consumed until the next requote
            logline(kind="live", note=f"paper fill: {side} {got:.0f} @ {qpx:.3f} "
                                      f"({m['q'][:40]}) — requoting")
            card["mid_at_quote"] = 0.0  # force requote next pass
    card["seen"] = list(seen)[-300:]

    # reward accrual for this tick (per-minute pie, sampled at our poll cadence)
    ours = two_sided(
        order_score(card.get("bid_sz", 0), abs(mid - card["bid"]) * 100, m["max_spread"], m["min_size"]),
        order_score(card.get("ask_sz", 0), abs(mid - card["ask"]) * 100, m["max_spread"], m["min_size"]))
    bookq = two_sided(book_score(bids, mid, m["max_spread"], m["min_size"]),
                      book_score(asks, mid, m["max_spread"], m["min_size"]))
    share = our_share(ours, bookq)
    card["ticks"] += 1
    if ours > 0:
        card["qualified_ticks"] += 1
    card["share"] = share
    dt_days = POLL_SECONDS / 86400.0
    with LOCK:
        STATE["rewards_usd"] += share * m["rate"] * REWARD_CAL * dt_days
    card["inv_value"] = card["pos"]["sh"] * mid
    card["q"], card["rate"] = m["q"], m["rate"]


def score_markouts():
    """MARKOUT_MIN minutes after each fill, score adverse selection: how far the
    mid ran against our fill price. Negative markout = we were picked off."""
    now = time.time()
    with LOCK:
        pending = [f for f in STATE["fills"] if f["markout"] is None
                   and now - f["t"] >= MARKOUT_MIN * 60]
        mids = {cid: c.get("mid") for cid, c in STATE["markets"].items()}
    for f in pending:
        mid = mids.get(f["cid"])
        if mid is None:
            continue
        gain = (mid - f["px"]) if f["side"] == "BUY" else (f["px"] - mid)
        f["markout"] = round(gain * f["sz"], 4)  # $ at MARKOUT_MIN, signed
        logline(kind="skip", note=f"markout {MARKOUT_MIN}m: {f['side']} {f['q'][:34]} "
                                  f"→ {f['markout']:+.2f}$")


# ---- PHASE 1: the live quoting engine (flag-gated; paper keeps running) -------
# A fresh wallet owns no outcome tokens, so it cannot rest a SELL. Two-sided
# quoting is therefore two BUYS: YES at (mid−d) and NO at 1−(mid+d) — both
# cash-collateralized, and exactly what the rewards formula counts as a
# two-sided book. Filled pairs sum to $1 at resolution.
def _vault_save(k):
    try:
        import keyring
        keyring.set_password("quotebot", "private_key", k)
        return keyring.get_password("quotebot", "private_key") == k
    except Exception:
        return False


def _vault_load():
    try:
        import keyring
        return keyring.get_password("quotebot", "private_key") or ""
    except Exception:
        return ""


PRIVATE_KEY_MEM = None
CLIENT = None
ORDERS = {}        # cid -> {"yes": {"id","px","sz"}, "no": {...}} — our resting GTC orders
LIVE_LEDGER = {"fills": 0, "rewards_paid": 0.0, "positions": [], "cash": None, "at": 0.0}


def live_ok():
    """The one gate every live action passes through: flag + creds, nothing else
    trades. Paper mode never reaches the client."""
    return bool(LIVE and FUNDER and (PRIVATE_KEY_MEM or _vault_load()))


def get_client():
    global CLIENT, PRIVATE_KEY_MEM
    if CLIENT is None:
        from py_clob_client_v2.client import ClobClient
        PRIVATE_KEY_MEM = PRIVATE_KEY_MEM or _vault_load()
        CLIENT = ClobClient("https://clob.polymarket.com", key=PRIVATE_KEY_MEM,
                            chain_id=137, signature_type=SIG_TYPE, funder=FUNDER)
        CLIENT.set_api_creds(CLIENT.create_or_derive_api_key())
    return CLIENT


def desired_quotes(mid, dist_c, size_sh):
    """The two live orders a market should carry: (yes_price, no_price, size).
    None when the mid sits too close to an edge for a symmetric two-sided quote."""
    d = dist_c / 100.0
    yes_px, no_px = round(mid - d, 3), round(1.0 - (mid + d), 3)
    if not (0.01 <= yes_px <= 0.99 and 0.01 <= no_px <= 0.99):
        return None
    return yes_px, no_px, round(size_sh, 1)


def needs_replace(resting, want_px, want_sz, tol_c=REQUOTE_C):
    """Replace a resting order only when the desired price drifted ≥ tol —
    churning cancels burns rate limits and queue position."""
    if not resting:
        return True
    return abs(resting["px"] - want_px) * 100 >= tol_c or resting["sz"] != want_sz


def live_notional():
    """$ committed right now: resting order notional + held inventory cost."""
    orders = sum(o["px"] * o["sz"] for m in ORDERS.values() for o in m.values() if o)
    inv = sum(float(p.get("initialValue") or 0) for p in LIVE_LEDGER["positions"])
    return orders + inv


def kill_reasons(m, card, now=None):
    """Why a market must NOT be live-quoted right now (empty list = clear)."""
    now = now or time.time()
    out = []
    if m.get("end_ts") and m["end_ts"] - now < KILL_END_DAYS * 86400:
        out.append(f"ends in {(m['end_ts'] - now) / 86400:.1f}d (< {KILL_END_DAYS:g}d)")
    hist = card.get("mid_hist") or []
    old = next((px for ts, px in hist if now - ts >= 600), None)
    if old is not None and abs(card.get("mid", old) - old) * 100 > KILL_MOVE_C:
        card["paused_until"] = now + 1800
        out.append(f"mid moved {abs(card['mid'] - old) * 100:.1f}c in 10min — news pause")
    if card.get("paused_until", 0) > now:
        out.append("paused after a recent move")
    return out


def _cancel(cid, side_key):
    o = ORDERS.get(cid, {}).get(side_key)
    if not o:
        return
    try:
        get_client().cancel(order_id=o["id"])
        logline(kind="live", note=f"live: cancelled {side_key} order @ {o['px']} ({cid[:10]}…)")
    except Exception as ex:
        logline(kind="error", note=f"live cancel failed: {str(ex)[:70]}")
    ORDERS.setdefault(cid, {})[side_key] = None


def cancel_all_live(reason="manual"):
    for cid in list(ORDERS):
        for side_key in ("yes", "no"):
            _cancel(cid, side_key)
    logline(kind="live", note=f"live: all orders cancelled ({reason})")


def _post(cid, side_key, tid, px, sz):
    """One GTC BUY. The ONLY order type this file ever sends; there is no
    market-order, no sell, no transfer anywhere in the live engine."""
    try:
        from py_clob_client_v2.clob_types import OrderArgs, OrderType
        cl = get_client()
        resp = cl.post_order(cl.create_order(OrderArgs(
            token_id=tid, price=px, size=sz, side="BUY")), OrderType.GTC)
        err = (resp.get("errorMsg") or "") if isinstance(resp, dict) else ""
        oid = (resp.get("orderID") or "") if isinstance(resp, dict) else ""
        if err or not oid:
            logline(kind="error", note=f"live post rejected: {err[:70] or 'no order id'}")
            return None
        logline(kind="live", note=f"live: resting {side_key.upper()} BUY {sz} @ {px} ({cid[:10]}…)")
        return {"id": oid, "px": px, "sz": sz}
    except Exception as ex:
        logline(kind="error", note=f"live post failed: {str(ex)[:70]}")
        return None


def live_sync(m, card):
    """Bring one market's real resting orders in line with the paper engine's
    desired quotes — same math, real money, hard caps. Called AFTER poll_market
    so the paper measurement stays untouched."""
    cid = m["cid"]
    mine = ORDERS.setdefault(cid, {"yes": None, "no": None})
    kills = kill_reasons(m, card)
    if kills:
        if mine["yes"] or mine["no"]:
            _cancel(cid, "yes")
            _cancel(cid, "no")
            logline(kind="live", note=f"live: standing down on {m['q'][:34]} — {kills[0]}")
        return
    mid = card.get("mid")
    if not mid:
        return
    side_usd = LIVE_BANKROLL / max(N_MARKETS, 1) / 2
    want = desired_quotes(mid, QUOTE_DIST_C, max(side_usd / mid, m["min_size"]))
    if not want:
        return
    yes_px, no_px, sz = want
    for side_key, tid, px in (("yes", m["tid"], yes_px), ("no", m["tid_no"], no_px)):
        if not needs_replace(mine[side_key], px, sz):
            continue
        _cancel(cid, side_key)
        add = px * sz
        if live_notional() + add > LIVE_BANKROLL + 1e-9:
            logline(kind="error", note=f"live: cap ${LIVE_BANKROLL:g} would be exceeded — not posting")
            continue
        mine[side_key] = _post(cid, side_key, tid, px, sz)


def poll_live_ledger():
    """The real account, from public endpoints: positions, cash, actual REWARD
    payouts (the CAL calibration number this whole experiment exists for)."""
    if not FUNDER:
        return
    try:
        pos = jget(f"{DATA_API}/positions", user=FUNDER, limit=100)
        LIVE_LEDGER["positions"] = [p for p in pos if float(p.get("size") or 0) > 0] \
            if isinstance(pos, list) else []
    except Exception:
        pass
    try:
        rows = jget(f"{DATA_API}/activity", user=FUNDER, limit=500)
        LIVE_LEDGER["rewards_paid"] = sum(float(r.get("usdcSize") or 0) for r in rows
                                          if isinstance(r, dict) and r.get("type") == "REWARD")
        LIVE_LEDGER["fills"] = sum(1 for r in rows if isinstance(r, dict) and r.get("type") == "TRADE")
    except Exception:
        pass
    LIVE_LEDGER["at"] = time.time()


WATCHED = []


def bot_loop():
    global WATCHED
    if STATE["watched"] and time.time() - STATE["last_scan"] < SCAN_EVERY_H * 3600:
        WATCHED = STATE["watched"]  # restart ≠ rescan: keep the dataset continuous
        with LOCK:
            STATE["scan_note"] = "resumed — watching " + ", ".join(w["q"][:28] for w in WATCHED)
        logline(kind="live", note=f"resumed {len(WATCHED)} watched markets from state (scan not due)")
    while True:
        if not WATCHED or time.time() - STATE["last_scan"] > SCAN_EVERY_H * 3600:
            got = scan_universe()
            with LOCK:
                STATE["last_scan"] = time.time()
            if got is not None:
                WATCHED = got
                with LOCK:
                    STATE["watched"] = got
                    STATE["scan_note"] = (time.strftime("%H:%M") + " — watching "
                                          + ", ".join(w["q"][:28] for w in WATCHED))
                logline(kind="live", note=f"universe scan: {len(WATCHED)} reward markets chosen")
        for m in list(WATCHED):
            poll_market(m)
            if live_ok():
                live_sync(m, market_card(m["cid"]))
            time.sleep(0.2)
        if live_ok() and time.time() - LIVE_LEDGER["at"] > 60:
            poll_live_ledger()
        score_markouts()
        daily_rollup()
        with LOCK:
            STATE["last_poll"] = time.time()
        save_state()
        time.sleep(POLL_SECONDS)


# ---- dashboard --------------------------------------------------------------------
def _fmt(v, plus=False):
    s = f"{v:+,.2f}" if plus else f"{v:,.2f}"
    return s


def render():
    with LOCK:
        mkts = {k: dict(v) for k, v in STATE["markets"].items()}
        fills = list(STATE["fills"])[-15:][::-1]
        log = list(STATE["log"])[:25]
        rewards, spread = STATE["rewards_usd"], STATE["spread_pnl"]
        last_poll, scan_note = STATE["last_poll"], STATE["scan_note"]
    inv = sum(m.get("inv_value", 0.0) for m in mkts.values())
    unrl = sum(unreal(m.get("pos", {"sh": 0, "cost": 0}), m.get("mid", 0)) for m in mkts.values())
    scored = [f["markout"] for f in STATE["fills"] if f.get("markout") is not None]
    adverse = sum(scored)  # diagnostic of fill quality, NOT added to net (it
    #                        would double-count what unrealized P&L already holds)
    net = rewards + spread + unrl
    up = time.time() - STATE["born"]
    days = max(up / 86400, 1e-9)
    # annualizing minutes of data is numerology — earn a day first
    apr = (f"{net / days / PAPER_BANKROLL * 365 * 100:+.0f}%/yr"
           if PAPER_BANKROLL and up >= 86400 else "—/yr (needs 24h)")

    rows = ""
    for m in (WATCHED or []):
        c = mkts.get(m["cid"], {})
        upt = 100 * c.get("qualified_ticks", 0) / max(c.get("ticks", 1), 1)
        rows += (f'<tr><td>{html.escape((m["q"] or "?")[:52])}</td>'
                 f'<td class=r>${m["rate"]:g}/d</td><td class=r>{m["min_size"]:g}</td>'
                 f'<td class=r>{m["max_spread"]:g}¢</td>'
                 f'<td class=r>{c.get("mid", 0):.3f}</td>'
                 f'<td class=r>{c.get("bid", 0):.3f}/{c.get("ask", 0):.3f}</td>'
                 f'<td class=r>{100 * c.get("share", 0):.1f}%</td>'
                 f'<td class=r>{upt:.0f}%</td><td class=r>{c.get("fills", 0)}</td>'
                 f'<td class=r>{c.get("pos", {}).get("sh", 0):+.0f}</td></tr>')
    rows = rows or '<tr><td colspan=10 class=dim>scanning the universe…</td></tr>'

    frows = ""
    for f in fills:
        mo = "…" if f.get("markout") is None else f"{f['markout']:+.2f}$"
        frows += (f'<tr><td class=dim>{time.strftime("%m-%d %H:%M", time.localtime(f["t"]))}</td>'
                  f'<td style="color:var({"--ok" if f["side"] == "BUY" else "--bad"})">{f["side"]}</td>'
                  f'<td>{html.escape(f["q"])}</td><td class=r>{f["sz"]:g}</td>'
                  f'<td class=r>@{f["px"]:.3f}</td><td class=r>{mo}</td></tr>')
    frows = frows or '<tr><td colspan=6 class=dim>no paper fills yet — that is normal: conservative queue model, slow markets</td></tr>'

    lrows = "".join(
        f'<div class=lr><span class=dim>{e.get("t")}</span> '
        f'<span style="color:var({ {"live": "--ok", "error": "--bad"}.get(e.get("kind"), "--dim") })">'
        f'{html.escape(str(e.get("note") or ""))}</span></div>' for e in log)

    poll_age = int(time.time() - last_poll) if last_poll else None
    live = live_ok()
    mode_badge = ('<span style="color:var(--bad)">● LIVE — real orders</span>' if live
                  else '<span style="color:var(--ok)">◌ paper — no money</span>')
    return f"""<!doctype html><html><head><meta charset=utf-8>
<title>Quotebot — {'LIVE' if live else 'paper'} LP</title>
<script>/* flicker-free refresh: DOM-parse the fetched page and swap the body —
   never regex raw HTML (a regex here once matched its own source and injected it) */
setInterval(async () => {{
  const a = document.activeElement;
  if (a && (a.tagName === "INPUT" || a.tagName === "TEXTAREA")) return;  // don't nuke a form you're typing in
  try {{
    const t = await (await fetch("/")).text();
    const doc = new DOMParser().parseFromString(t, "text/html");
    if (doc.body) document.body.innerHTML = doc.body.innerHTML;
  }} catch (e) {{}}
}}, 6000);
</script><style>
:root{{--bg:#0b0e14;--card:#131824;--ink:#e6e9f0;--dim:#8b93a7;--line:#232a3b;
--ok:#4ade80;--bad:#f87171;--warn:#fbbf24;--info:#60a5fa;--accent:#a78bfa}}
body{{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 system-ui,Segoe UI,sans-serif;padding:20px}}
.wrap{{max-width:1100px;margin:0 auto}} h1{{font-size:20px;margin:0 0 2px}}
.sub{{color:var(--dim);font-size:13px;margin-bottom:14px}}
.cards{{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:14px}}
.c{{background:var(--card);border:1px solid var(--line);border-radius:9px;padding:10px 16px;min-width:130px}}
.c .k{{font-size:11.5px;color:var(--dim)}} .c .v{{font-size:19px;font-weight:700}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:9px;padding:14px;margin-bottom:14px}}
h2{{font-size:13px;margin:0 0 8px;color:var(--dim);text-transform:uppercase;letter-spacing:.05em}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
th{{text-align:left;color:var(--dim);font-weight:600;padding:3px 8px 3px 0}}
td{{padding:3px 8px 3px 0;border-top:1px solid var(--line)}} .r{{text-align:right}}
.dim{{color:var(--dim)}} .lr{{font-size:12.5px;padding:1px 0}}
a{{color:var(--info)}}
input,button{{font:inherit}} label{{display:block;font-size:12px;color:var(--dim);margin:6px 0 2px}}
.in{{background:#0e1420;border:1px solid var(--line);border-radius:7px;color:var(--ink);padding:6px 9px;font-size:13px}}
button.go{{background:var(--accent);color:#0b0e14;border:0;border-radius:7px;padding:7px 14px;font-weight:700;cursor:pointer}}
button.kill{{background:#2a1520;color:var(--bad);border:1px solid var(--bad);border-radius:7px;padding:7px 14px;font-weight:700;cursor:pointer}}
#toast{{position:fixed;left:50%;bottom:20%;transform:translateX(-50%);background:var(--card);border:1px solid var(--line);
border-radius:10px;padding:11px 20px;font-weight:600;opacity:0;pointer-events:none;transition:opacity .25s;z-index:9;box-shadow:0 8px 28px rgba(0,0,0,.5)}}
#toast.show{{opacity:1}}</style></head><body><div class=wrap>
<h1>quotebot <span class=dim style=font-size:13px>— Polymarket LP-rewards bot</span> {mode_badge}</h1>
<div class=sub>up {int(up // 3600)}h{int(up % 3600 // 60)}m · polled {poll_age if poll_age is not None else "—"}s ago ·
paper bankroll ${PAPER_BANKROLL:g}{' · LIVE cap $' + format(LIVE_BANKROLL, 'g') if live else ''} ·
{html.escape(scan_note or "first scan pending")}</div>
{_live_card() if (LIVE or FUNDER) else ''}
<div class=cards>
<div class=c><div class=k>simulated rewards</div><div class=v style=color:var(--ok)>${_fmt(rewards)}</div></div>
<div class=c><div class=k>realized spread P&L</div><div class=v>{_fmt(spread, True)}$</div></div>
<div class=c><div class=k>unrealized (open inventory)</div><div class=v style="color:var({'--bad' if unrl < 0 else '--ok'})">{_fmt(unrl, True)}$</div></div>
<div class=c><div class=k>inventory value (not P&L)</div><div class="v dim">{_fmt(inv, True)}$</div></div>
<div class=c><div class=k>fill quality ({MARKOUT_MIN}m markouts, diagnostic)</div><div class=v style="color:var({'--bad' if adverse < 0 else '--ok'})">{_fmt(adverse, True)}$</div></div>
<div class=c><div class=k>current reward pace</div><div class=v>${sum(m["rate"] * mkts.get(m["cid"], {}).get("share", 0) for m in (WATCHED or [])):,.2f}/day</div></div>
<div class=c><div class=k>net → annualized on ${PAPER_BANKROLL:g}</div><div class=v style="color:var({'--ok' if net >= 0 else '--bad'})">{_fmt(net, True)}$ · {apr}</div></div>
</div>
<div class=sub style=margin-top:-6px>net = rewards + realized + unrealized. These are the <b>paper</b> simulation numbers
(real book, real tape, last-in-queue fills) — the phase-1 go/no-go signal. {'Live orders run in parallel and are shown in the LIVE panel above.' if live else 'Live quoting is OFF; enable it in Settings once the paper number is green.'}</div>
<div class=card><h2>Watched reward markets (long-horizon, non-sports — copybot-disjoint by rule)</h2>
<table><tr><th>market</th><th class=r>reward</th><th class=r>min sz</th><th class=r>band</th>
<th class=r>mid</th><th class=r>our bid/ask</th><th class=r>share</th><th class=r>uptime</th>
<th class=r>fills</th><th class=r>inv sh</th></tr>{rows}</table>
<div class=dim style=margin-top:6px>share = our score ÷ (ours + whole qualifying book) per the published
formula — exact. The $ number multiplies it by the market's daily rate × CAL={REWARD_CAL:g}
(calibrated against a real payout in phase 1).</div></div>
<div class=card><h2>Paper fills &amp; markouts</h2><table>
<tr><th>when</th><th>side</th><th>market</th><th class=r>shares</th><th class=r>at</th>
<th class=r>markout {MARKOUT_MIN}m</th></tr>{frows}</table></div>
<div class=card><h2>Log</h2>{lrows or '<span class=dim>—</span>'}</div>
{_settings_card()}
<div class=dim style=font-size:12px>Paper mode measures rewards share (exact) + fill rate + adverse selection
against the real book and tape (last-in-queue fill model) — the phase-1 go/no-go. Live mode rests real GTC
two-sided BUY orders, hard-capped, with a news-move + near-expiry kill switch; no withdrawal/transfer code
exists in this file. Repo: <a href=https://github.com/kamilch1k/polymarket-quotebot>polymarket-quotebot</a> ·
sibling of <a href=https://github.com/kamilch1k/polymarket-copybot>polymarket-copybot</a>.</div>
</div>
<script>
// action forms submit in place (no reload / scroll jump) with a centered toast
function toast(m){{var t=document.getElementById('toast');if(!t){{t=document.createElement('div');t.id='toast';document.body.appendChild(t);}}
t.textContent=m;t.classList.add('show');clearTimeout(toast._h);toast._h=setTimeout(function(){{t.classList.remove('show');}},3000);}}
document.addEventListener('submit',function(e){{var fm=e.target;if(!fm.matches('form[action^="/"]'))return;
e.preventDefault();var b=fm.querySelector('button'),lb=b?b.textContent.trim():'saved';
if(fm.getAttribute('action')==='/panic'&&!confirm('Cancel all live orders and switch to paper?'))return;
fetch(fm.getAttribute('action'),{{method:'POST',body:new URLSearchParams(new FormData(fm))}})
.then(function(){{toast('✓ '+lb);return fetch('/');}}).then(function(r){{return r.text();}})
.then(function(h){{var d=new DOMParser().parseFromString(h,'text/html');document.body.innerHTML=d.body.innerHTML;}})
.catch(function(){{toast('✗ failed');}});}});
</script>
</body></html>"""


def _live_card():
    """The real-money panel: only shown once a funder is set. Positions, cash,
    ACTUAL reward payouts (the CAL number), resting orders, and a kill switch."""
    live = live_ok()
    L = LIVE_LEDGER
    posval = sum(float(p.get("currentValue") or 0) for p in L["positions"])
    orders_n = sum(1 for m in ORDERS.values() for o in m.values() if o)
    cash = f"${L['cash']:,.2f}" if L.get("cash") is not None else "—"
    body = (f'<div class=cards style=margin-bottom:8px>'
            f'<div class=c><div class=k>status</div><div class=v style="color:var({"--bad" if live else "--dim"})">'
            f'{"● quoting live" if live else "◌ configured, not live"}</div></div>'
            f'<div class=c><div class=k>resting orders</div><div class=v>{orders_n}</div></div>'
            f'<div class=c><div class=k>inventory value</div><div class=v>${posval:,.2f}</div></div>'
            f'<div class=c><div class=k>REAL rewards paid</div><div class=v style=color:var(--ok)>'
            f'${L["rewards_paid"]:,.2f}</div><div class=k>the CAL calibration number</div></div>'
            f'<div class=c><div class=k>live cap</div><div class=v>${LIVE_BANKROLL:g}</div></div></div>')
    kill = ('<form method=post action=/panic style=display:inline>'
            '<button class=kill title="cancel every resting order and switch to paper">'
            'PANIC — cancel all &amp; go paper</button></form>' if live else '')
    return (f'<div class=card style="border-color:{"var(--bad)" if live else "var(--line)"}">'
            f'<h2>Live account — {FUNDER[:6]}…{FUNDER[-4:] if FUNDER else ""} '
            f'(separate wallet from the copybot)</h2>{body}{kill}</div>')


def _settings_card():
    live = live_ok()
    return f"""<div class=card><h2>Settings</h2>
<form method=post action=/settings>
<div style="display:flex;gap:16px;flex-wrap:wrap">
<div><label>paper bankroll $</label><input class=in name=paper_bankroll value="{PAPER_BANKROLL:g}" style=width:90px></div>
<div><label>markets quoted</label><input class=in name=n_markets value="{N_MARKETS}" style=width:70px></div>
<div><label>quote distance ¢</label><input class=in name=quote_dist_c value="{QUOTE_DIST_C:g}" style=width:70px></div>
<div><label>min days out</label><input class=in name=min_days_out value="{MIN_DAYS_OUT}" style=width:70px></div>
<div><label>reward CAL</label><input class=in name=reward_cal value="{REWARD_CAL:g}" style=width:70px></div>
</div>
<button class=go style=margin-top:10px>Save paper settings</button></form>
<hr style="border:0;border-top:1px solid var(--line);margin:14px 0">
<h2 style="color:var(--bad)">Live quoting — real money (phase 1)</h2>
<div class=sub>Off by default. Enabling rests real GTC BUY orders on both sides, hard-capped at the live bankroll.
<b>Use a fresh wallet, funded small, separate from the copybot.</b> There is no withdrawal or transfer code —
selling to cash and moving funds stay on polymarket.com. Not financial advice.</div>
<form method=post action=/live-config>
<div style="display:flex;gap:16px;flex-wrap:wrap;align-items:end">
<div><label>funder wallet (0x…)</label><input class=in name=funder value="{FUNDER}" placeholder=0x… style=width:340px></div>
<div><label>private key {'(saved ✓ — blank keeps it)' if (PRIVATE_KEY_MEM or _vault_load()) else '(→ OS vault)'}</label>
<input class=in name=private_key type=password value="" placeholder="{'••••' if (PRIVATE_KEY_MEM or _vault_load()) else '0x…'}" style=width:200px></div>
<div><label>live cap $</label><input class=in name=live_bankroll value="{LIVE_BANKROLL:g}" style=width:80px></div>
</div>
<label style="margin-top:10px"><input type=checkbox name=live value=1 {'checked' if LIVE else ''}> enable live quoting</label>
<button class=go style=margin-top:8px>Save live settings</button></form></div>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = render().encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        global LIVE, FUNDER, PRIVATE_KEY_MEM, CLIENT
        path = urlparse(self.path).path
        n = int(self.headers.get("Content-Length", 0) or 0)
        f = parse_qs(self.rfile.read(n).decode()) if n else {}
        g = lambda k, d="": f.get(k, [d])[0]

        def num(k):
            try:
                return float(g(k))
            except ValueError:
                return None
        if path == "/settings":
            for k in ("paper_bankroll", "quote_dist_c", "min_days_out", "reward_cal"):
                v = num(k)
                if v is not None:
                    globals()[k.upper()] = v
            v = num("n_markets")
            if v:
                globals()["N_MARKETS"] = int(v)
            save_config()
        elif path == "/live-config":
            fn = g("funder").strip()
            if fn.startswith("0x") and len(fn) == 42:
                FUNDER = fn
            pk = g("private_key").strip()
            if pk:
                PRIVATE_KEY_MEM = pk
                CLIENT = None
            v = num("live_bankroll")
            if v is not None:
                globals()["LIVE_BANKROLL"] = v
            new_live = g("live") == "1"
            if LIVE and not new_live:  # turning live OFF pulls every order first
                cancel_all_live("live disabled in settings")
            LIVE = new_live
            save_config()
            logline(kind="live", note=f"live quoting {'ENABLED' if live_ok() else 'set (still missing funder/key)' if LIVE else 'DISABLED'}")
        elif path == "/panic":
            cancel_all_live("PANIC button")
            LIVE = False
            save_config()
            logline(kind="error", note="PANIC: live quoting OFF, all orders cancelled")
        self.send_response(303)
        self.send_header("Location", "/")
        self.end_headers()

    def log_message(self, *a):
        pass


# ---- self-test -----------------------------------------------------------------
def _check():
    globals()["CONFIG_FILE"] = Path(os.environ.get("TEMP", ".")) / "quotebot_check_config.json"
    globals()["STATE_FILE"] = Path(os.environ.get("TEMP", ".")) / "quotebot_check_state.json"
    # scoring: quadratic closeness, hard qualification gates
    assert order_score(100, 0.0, 3.0, 50) == 100.0            # at mid, full weight
    assert order_score(100, 1.5, 3.0, 50) == 25.0             # half band → ¼ weight
    assert order_score(100, 3.1, 3.0, 50) == 0.0              # outside the band
    assert order_score(49, 0.0, 3.0, 50) == 0.0               # below min size
    assert two_sided(30.0, 10.0) == 10.0                      # one-sided earns nothing extra
    assert our_share(25.0, 75.0) == 0.25 and our_share(0, 0) == 0.0
    bq = book_score([(0.50, 100), (0.48, 200), (0.40, 999)], 0.51, 3.0, 50)
    assert abs(bq - (100 * (2 / 3) ** 2)) < 1e-9              # 1¢ off counts, 3¢+ and dust don't
    # tape normalization: NO prints mirror onto YES
    assert yes_print("YES", 0.60, 5, "BUY", "YES") == ("BUY", 0.60, 5.0)
    s, p, z = yes_print("NO", 0.42, 7, "BUY", "YES")
    assert s == "SELL" and abs(p - 0.58) < 1e-9 and z == 7.0
    # complement books fold onto YES: a NO ask at 0.62 is a YES bid at 0.38
    mb, ma = merged_yes_book({"bids": [{"price": "0.40", "size": "10"}], "asks": []},
                             {"bids": [{"price": "0.55", "size": "7"}],
                              "asks": [{"price": "0.62", "size": "5"}]})
    assert (0.40, 10.0) in mb and (0.38, 5.0) in mb and (0.45, 7.0) in ma and len(ma) == 1
    mb2, ma2 = merged_yes_book({"bids": [], "asks": []}, {})  # missing NO book tolerated
    assert mb2 == [] and ma2 == []
    # last-in-queue fill model: strict cross only
    assert fill_against(0.50, 100, "BUY", "SELL", 0.499, 30) == 30.0
    assert fill_against(0.50, 100, "BUY", "SELL", 0.500, 30) == 0.0   # at-price = queued behind
    assert fill_against(0.52, 100, "SELL", "BUY", 0.53, 500) == 100.0  # capped at our size
    assert fill_against(0.52, 0, "SELL", "BUY", 0.53, 500) == 0.0
    # avg-cost ledger: open, extend, partial close, flip
    pos = {"sh": 0.0, "cost": 0.0}
    assert avg_cost_pnl(pos, 0.50, 10, "BUY") == 0.0 and pos == {"sh": 10.0, "cost": 0.50}
    avg_cost_pnl(pos, 0.60, 10, "BUY")
    assert abs(pos["cost"] - 0.55) < 1e-9
    r = avg_cost_pnl(pos, 0.65, 20, "SELL")
    assert abs(r - 2.0) < 1e-9 and pos["sh"] == 0.0            # (0.65−0.55)×20
    r = avg_cost_pnl(pos, 0.40, 5, "SELL")
    assert r == 0.0 and pos["sh"] == -5.0 and pos["cost"] == 0.40
    r = avg_cost_pnl(pos, 0.30, 5, "BUY")
    assert abs(r - 0.5) < 1e-9 and pos["sh"] == 0.0            # short covered lower
    # structural copybot separation: sports are refused
    assert sporty("France vs. Spain: Team to Advance") and sporty("Yankees O/U 8.5")
    assert not sporty("Will the Fed decrease interest rates in September?")
    # unrealized P&L: longs gain up, shorts gain down; flat is flat
    assert abs(unreal({"sh": 10, "cost": 0.40}, 0.50) - 1.0) < 1e-9
    assert abs(unreal({"sh": -10, "cost": 0.40}, 0.30) - 1.0) < 1e-9
    assert unreal({"sh": 0.0, "cost": 0.0}, 0.99) == 0.0
    # inventory cap: the growing side goes dark at the cap, the shrinking side stays
    assert quote_sizes(0, 300, 100) == (100, 100)
    assert quote_sizes(300, 300, 100) == (0.0, 100)
    assert quote_sizes(-300, 300, 100) == (100, 0.0)
    # LIVE ENGINE (phase 1) — all pure, no network:
    # two-sided quote = two BUYS, YES at mid−d and NO at 1−(mid+d)
    yp, np_, sz = desired_quotes(0.50, 2.0, 100)
    assert yp == 0.48 and np_ == 0.48 and sz == 100  # symmetric at mid 0.50
    yp, np_, _ = desired_quotes(0.30, 2.0, 100)
    assert abs(yp - 0.28) < 1e-9 and abs(np_ - 0.68) < 1e-9  # NO side = 1−0.32
    assert desired_quotes(0.995, 2.0, 100) is None    # too close to the edge → no quote
    # requote hysteresis: only replace on a real drift
    assert needs_replace(None, 0.48, 100) is True
    assert needs_replace({"px": 0.48, "sz": 100}, 0.48, 100) is False
    assert needs_replace({"px": 0.48, "sz": 100}, 0.485, 100, tol_c=0.5) is True
    # kill switch: near-expiry and a 10-min news move both stand the market down
    _now = 1_000_000.0
    assert any("ends in" in r for r in kill_reasons({"end_ts": _now + 3 * 86400}, {}, _now))
    moved = kill_reasons({"end_ts": _now + 60 * 86400},
                         {"mid": 0.60, "mid_hist": [(_now - 700, 0.50)]}, _now)
    assert any("news pause" in r for r in moved)
    assert kill_reasons({"end_ts": _now + 60 * 86400},
                        {"mid": 0.505, "mid_hist": [(_now - 700, 0.50)]}, _now) == []  # 0.5c = calm
    # the live gate: nothing trades without flag AND funder AND key
    globals()["LIVE"], globals()["FUNDER"], globals()["PRIVATE_KEY_MEM"] = False, "", None
    assert not live_ok()
    globals()["LIVE"] = True
    assert not live_ok()                              # flag alone is not enough
    globals()["FUNDER"], globals()["PRIVATE_KEY_MEM"] = "0x" + "a" * 40, "0x" + "b" * 32
    assert live_ok()
    globals()["LIVE"], globals()["FUNDER"], globals()["PRIVATE_KEY_MEM"] = False, "", None
    # live notional cap accounting: resting orders + inventory cost
    ORDERS.clear()
    ORDERS["m"] = {"yes": {"id": "1", "px": 0.5, "sz": 100}, "no": {"id": "2", "px": 0.4, "sz": 100}}
    LIVE_LEDGER["positions"] = [{"initialValue": "20"}]
    assert abs(live_notional() - (50 + 40 + 20)) < 1e-9
    ORDERS.clear()
    LIVE_LEDGER["positions"] = []
    # watched universe survives a restart (restart ≠ rescan ≠ dataset gap)
    STATE["watched"] = [{"cid": "w1", "tid": "t", "tid_no": "tn", "q": "Q?", "rate": 9,
                         "min_size": 50, "max_spread": 4.5, "mid": 0.5, "days": 30.0}]
    STATE["last_scan"] = time.time()
    save_state()
    STATE["watched"], STATE["last_scan"] = [], 0.0
    load_state()
    assert STATE["watched"][0]["cid"] == "w1" and STATE["last_scan"] > 0
    STATE["watched"], STATE["last_scan"] = [], 0.0
    STATE_FILE.unlink(missing_ok=True)
    # dashboard renders without a network in sight; DOM-parsed refresh (the raw-regex
    # version once matched its own source and injected it into the page)
    page = render()  # default state = paper: the badge and disclaimer say so
    assert "paper — no money" in page and "Live quoting is OFF" in page
    assert "http-equiv=refresh" not in page and "DOMParser" in page
    assert "[\\s\\S]" not in page and "unrealized" in page
    # grep-true SAFETY INVARIANTS of the live engine (the honest phase-1 claims).
    # Needles are split so this scan never counts its own literals.
    src = Path(__file__).read_text(encoding="utf-8")
    n = lambda a, b: src.count(a + b)
    #  1) the one and only OrderArgs is a GTC BUY — no sell, no market order
    assert n("Order", "Args(") == 1 and n('side="B', 'UY"') == 1
    assert n('side="S', 'ELL"') == 0 and n("Market", "OrderArgs") == 0
    assert n("OrderType.G", "TC") == 1 and n("OrderType.F", "AK") == 0
    #  2) exactly one post_order and one create_order call site (inside _post)
    assert n("post_", "order(") == 1 and n("create_", "order(") == 1
    #  3) no fund-movement CODE — the CLOB client's transfer/withdraw call shapes
    #  (English 'transfer'/'withdraw' in prose is fine; these are the API tokens)
    for a, b in (("eth_sendRaw", "Transaction"), ("send_", "transaction"),
                 (".trans", "fer("), (".with", "draw("), ("create_", "withdrawal")):
        assert n(a, b) == 0, f"fund-movement call '{a + b}' must not exist"
    #  4) every real order flows through the live_ok gate (flag+funder+key)
    assert "def live_ok" in src and "if live_ok():" in src
    print("self-check OK")


if __name__ == "__main__":
    if "--check" in sys.argv:
        _check()
        sys.exit()
    HEADLESS = "--headless" in sys.argv
    url = f"http://127.0.0.1:{PORT}"
    ThreadingHTTPServer.allow_reuse_address = False
    try:
        server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:
        sys.exit("quotebot: port in use — already running")
    load_config()
    save_config()
    load_state()
    threading.Thread(target=bot_loop, daemon=True).start()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"quotebot: {url}")
    if HEADLESS:
        while True:
            time.sleep(3600)
    try:
        import webview  # native desktop window, same shell as the copybot
        webview.create_window("Quotebot — paper LP", url, width=1180, height=900)
        webview.start()  # returns when the window is closed
        # window closed = process exits; the QuotebotWatchdog task revives the
        # paper run headless within 10 min, so the dataset keeps growing
        os._exit(0)
    except ImportError:
        webbrowser.open(url)  # no pywebview: a browser tab is the window
        while True:
            time.sleep(3600)
