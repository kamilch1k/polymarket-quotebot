#!/usr/bin/env python3
"""
Monte-Carlo evaluation of the quotebot strategy WITHOUT waiting two weeks.

Synthetic-but-calibrated episodes: mid paths, order flow, news jumps and maker
competition are simulated; the LP logic itself is NOT — every scoring, fill,
inventory and P&L decision calls the same pure functions quotebot.py runs live
(order_score / two_sided / our_share / avg_cost_pnl / quote_sizes / unreal).

Calibration (measured 2026-07-13 from the four live watched markets — see
README): reward budgets $100–625/day, tape 47–837 prints/day, median clip
$5–21 (p90 $46–408), mid volatility 2.6–7.2 c/day, worst 1-hour jump seen in a
week: 12c. The two knobs paper trading can't observe quickly — competition and
jump frequency — are swept as scenario axes instead of guessed.

Usage:
  python sim.py            full sweep (~1 min)
  python sim.py --check    5-episode smoke test (offline, fast)
"""
import math
import random
import statistics
import sys

from quotebot import (order_score, two_sided, our_share, avg_cost_pnl,
                      quote_sizes, unreal, INV_CAP_X, QUOTE_DIST_C)

# ---- calibrated market profiles (measured from the live watched set) ----------
PROFILES = [
    # name            $/day  min_sz prints/d med$  p90$  sig c/d  mid
    ("LeBron-type",    625,   200,   647,     10,   90,   5.8,   0.23),
    ("Hormuz-type",    100,    50,   210,      5,   46,   7.2,   0.68),
    ("Fed-type",       258,    50,    47,     21,  408,   5.0,   0.38),
    ("Invade-type",    500,    50,   837,      6,  163,   2.6,   0.30),
]
MAX_SPREAD_C = 4.5      # observed on all four
DEPTH_MEAN_C = 0.7      # how deep a print reaches past the touch (exponential);
                        # most volume trades at the touch — crossing 2c is rare
JUMP_MED_C = 5.0        # news jump size: lognormal, median 5c, p95 ~ 12c
JUMP_SIG = 0.53
DT_MIN = 10             # simulation step (minutes)
DAYS = 14               # one episode = the two-week phase-0 window
N_MKTS = 2              # markets quoted per episode (bankroll split across them)


def lognorm(rng, med, p90):
    """Sample a $ print size from a lognormal fitted to (median, p90)."""
    sig = max(0.1, math.log(p90 / med) / 1.2816)
    return med * math.exp(rng.gauss(0, sig))


def episode(rng, bankroll, competition, jumps_per_day, toxic_c=0.0):
    """One 14-day paper life. Returns dict of outcome components (in $).
    toxic_c = expected adverse mid drift (cents) right after a normal fill —
    the informed-flow component the live markouts measure. 0 = naive fills,
    2 = the 2c quoted edge is exactly eaten, 3 = flow is outright toxic.
    Note: jump losses flow into realized/unrealized through the position ledger;
    the separate `jump` figure is a diagnostic decomposition, never re-added."""
    steps = int(DAYS * 24 * 60 / DT_MIN)
    picks = rng.sample(PROFILES, N_MKTS)
    side_usd = bankroll / N_MKTS / 2
    rewards = realized = jump_loss = 0.0
    end_unreal = 0.0
    skipped = 0
    for name, rate, min_sz, prints_d, med, p90, sig_cd, mid0 in picks:
        mid = mid0
        base = side_usd / mid
        if base < min_sz:      # can't post a qualifying quote at this bankroll
            skipped += 1
            continue
        pos = {"sh": 0.0, "cost": 0.0}
        # competition in units of one 100-share two-sided quoter at our distance
        unit = 100 * ((MAX_SPREAD_C - QUOTE_DIST_C) / MAX_SPREAD_C) ** 2
        book_q = competition * unit
        sig_step = sig_cd / 100 / math.sqrt(24 * 60 / DT_MIN)
        p_jump = jumps_per_day * DT_MIN / (24 * 60)
        n_prints_step = prints_d * DT_MIN / (24 * 60)
        d = QUOTE_DIST_C / 100
        for _ in range(steps):
            bid_sz, ask_sz = quote_sizes(pos["sh"], INV_CAP_X * base, base)
            bid, ask = mid - d, mid + d
            # news jump: a stale quote is crossed BEFORE we can requote
            if rng.random() < p_jump:
                jc = JUMP_MED_C * math.exp(rng.gauss(0, JUMP_SIG))
                direction = rng.choice((-1, 1))
                if jc > QUOTE_DIST_C:
                    if direction < 0 and bid_sz > 0:      # crash: our bid is hit
                        realized += avg_cost_pnl(pos, bid, bid_sz, "BUY")
                        jump_loss -= (jc - QUOTE_DIST_C) / 100 * bid_sz
                    elif direction > 0 and ask_sz > 0:    # spike: our ask lifts
                        realized += avg_cost_pnl(pos, ask, ask_sz, "SELL")
                        jump_loss -= (jc - QUOTE_DIST_C) / 100 * ask_sz
                mid = min(0.97, max(0.03, mid + direction * jc / 100))
                continue  # this step's rewards are lost to the requote scramble
            # normal flow: Poisson prints, exponential depth past the touch
            for _ in range(int(n_prints_step) + (rng.random() < n_prints_step % 1)):
                usd = lognorm(rng, med, p90)
                depth = rng.expovariate(1 / DEPTH_MEAN_C)
                if depth <= QUOTE_DIST_C:
                    continue                       # didn't reach us (queue ahead)
                # toxicity = an EV haircut per filled share, NOT a price shove:
                # shoving the mid after iid fills manufactures oscillation the
                # grid then harvests (we tried — toxicity came out *profitable*,
                # which is how you know a model is lying). Real toxicity is
                # trending flow; only the live markouts can measure it, so here
                # it enters as the transparent expected cost it is.
                sh = min(usd / mid, base)
                if rng.random() < 0.5 and bid_sz > 0:
                    got = min(sh, bid_sz)
                    realized += avg_cost_pnl(pos, bid, got, "BUY") - got * toxic_c / 100
                    bid_sz = 0.0                   # consumed until next step
                elif ask_sz > 0:
                    got = min(sh, ask_sz)
                    realized += avg_cost_pnl(pos, ask, got, "SELL") - got * toxic_c / 100
                    ask_sz = 0.0
            # reward accrual, exactly the bot's math
            ours = two_sided(order_score(bid_sz, QUOTE_DIST_C, MAX_SPREAD_C, min_sz),
                             order_score(ask_sz, QUOTE_DIST_C, MAX_SPREAD_C, min_sz))
            rewards += our_share(ours, book_q) * rate * DT_MIN / (24 * 60)
            # diffusion
            mid = min(0.97, max(0.03, mid + rng.gauss(0, sig_step)))
        end_unreal += unreal(pos, mid)
    return {"rewards": rewards, "realized": realized, "unreal": end_unreal,
            "jump": jump_loss,  # diagnostic only — already inside realized/unreal
            "net": rewards + realized + end_unreal, "skipped": skipped}


def cell(bankroll, competition, jumps_per_day, n, seed, toxic_c=0.0):
    rng = random.Random(seed)
    outs = [episode(rng, bankroll, competition, jumps_per_day, toxic_c) for _ in range(n)]
    nets = sorted(o["net"] for o in outs)
    q = lambda t: nets[min(n - 1, int(t * n))]
    return {"med": q(.5), "p10": q(.1), "p90": q(.9),
            "green": 100 * sum(1 for v in nets if v > 0) / n,
            "rw": statistics.mean(o["rewards"] for o in outs),
            "jl": statistics.mean(o["jump"] for o in outs),
            "sp": statistics.mean(o["realized"] + o["unreal"] for o in outs)}


def main():
    fast = "--check" in sys.argv
    n = 5 if fast else 150
    print(f"quotebot strategy Monte-Carlo — {n} episodes/cell, {DAYS}d each, "
          f"${400:g} bankroll, {N_MKTS} markets/episode")
    print("(logic under test = quotebot.py's own functions; flow/jumps/competition synthetic, calibrated)")
    print()
    print("NOTE: absolute dollars hinge on two facts only live trading can pin — the payout")
    print("units/thresholds of rewards_daily_rate (CAL) and real fill toxicity. A $153k LP")
    print("account we measured earns ~0.1%/day on capital: expect the truth near the")
    print("bottom-right cells. The live paper run measures toxicity (markouts) within days;")
    print("one ~$50 real quote for 48h pins CAL. Read this grid as sensitivity, not forecast.")
    print()
    print("14-day net on $400, jumps fixed at 0.3/day — competition × fill-toxicity grid:")
    print("| competition \\ toxicity | 0¢ (naive) | 1¢ | 2¢ (edge eaten) | 3¢ (toxic) |")
    print("|---|---|---|---|---|")
    for comp, label in ((4, "4 hobby makers"), (30, "measured secondary books"),
                        (120, "one pro (5k sh @ 1¢)"), (500, "saturated pro band")):
        row = f"| {label} |"
        for tox in (0.0, 1.0, 2.0, 3.0):
            c = cell(400, comp, 0.3, n, seed=hash((comp, tox)) & 0xffff, toxic_c=tox)
            row += f" {c['med']:+.0f}$ ({c['green']:.0f}% green) |"
        print(row)
    if fast:
        print("smoke OK")
        return
    print()
    print("decomposition at the honest base case (measured books, toxicity 2¢):")
    c = cell(400, 30, 0.3, n, seed=7, toxic_c=2.0)
    print(f"  median net {c['med']:+.0f}$  [p10 {c['p10']:+.0f}$ … p90 {c['p90']:+.0f}$]  "
          f"rewards {c['rw']:+.0f}$ · spread {c['sp']:+.0f}$ · jump cost {c['jl']:+.0f}$")
    print()
    print("capital scaling (measured books, toxicity 2¢):")
    print("| bankroll | median net / 14d | annualized | P(green) |")
    print("|---|---|---|---|")
    for bk in (150, 400, 2000, 10000):
        c = cell(bk, 30, 0.3, n, seed=bk, toxic_c=2.0)
        apr = c["med"] / bk / 14 * 365 * 100
        print(f"| ${bk:,} | {c['med']:+.0f}$ | {apr:+.0f}%/yr | {c['green']:.0f}% |")


if __name__ == "__main__":
    main()
