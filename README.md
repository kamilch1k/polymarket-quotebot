# polymarket-quotebot

The sibling of [polymarket-copybot](https://github.com/kamilch1k/polymarket-copybot),
built to answer the opposite question. The copybot pays the spread to inherit
other people's edges. This bot measures what the *other* side of that trade
earns: resting two-sided quotes on Polymarket's liquidity-rewards markets.

It exists because we analyzed a $153k-lifetime-profit account with an eerily
smooth curve (83% green days, $353 max drawdown on a ~$197k book) and found it
wasn't predicting anything — it held **both sides of the same 2028 market** at
50¢/50¢ and ground out spread + LP rewards on ~$18.9M of volume (0.8% per
dollar traded). You can't *copy* a market maker (your copy crosses the spread
he just earned — the sign flips at the boundary). But you can *become* one,
and Polymarket explicitly subsidizes it. The question is whether it pays at
hobby scale. This repo measures that instead of guessing.

## Phase 0 — paper, running now

**No orders, no key, no money.** The file contains no order-placement code at
all — the self-test greps itself to prove the claim stays true. What it does,
against fully public endpoints:

- **scans the universe** every 6h for reward-paying markets (`min_size`,
  `max_spread`, daily budget from the CLOB metadata), keeps only
  **long-horizon (≥14d), non-sports** ones — the profile the strategy wants,
  and structural non-overlap with the copybot
- **rests virtual two-sided quotes** at mid±2¢, requotes when the mid drifts
- **accrues simulated rewards** by the published scoring shape
  (size × ((S−d)/S)², two-sided minimum, our score ÷ whole qualifying book).
  The *share* is exact; the dollar figure multiplies it by the market's daily
  rate × a calibration constant that phase 1 pins against a real payout
- **simulates fills against the real trade tape** with a deliberately
  pessimistic queue model: we fill only when a print crosses *strictly through*
  our price — at-price prints are assumed to have filled the real book ahead
  of us
- **scores adverse selection**: every paper fill gets a 30-minute markout
  (how far the mid ran against us). This is the number naive LP math ignores
  and the one that decides everything:

```
net yield = rewards + captured spread − adverse selection
```

Dashboard at `http://127.0.0.1:8778` (auto-refreshing), daily rollups appended
to `quotebot_daily.jsonl` — the write-up will be built from that file after
~2 weeks of unattended paper trading.

First scan, for flavor: it independently chose the same Fed-decisions market a
$150k LP account we studied was quoting ($254/day reward budget), plus
Iran-Hormuz ($100/day) where at scan time **no other maker had a qualifying
two-sided quote** — the honest caveat being that empty bands rarely stay empty.

## What phase 1 would be (only if phase 0's number is green)

A separate tiny wallet ($100–300), GTC quotes in one or two low-minimum
markets, an event kill-switch, and calibration of the reward model against
real daily payouts. Hard rules already decided: **separate wallet from the
copybot** (its budget math reads whole-wallet balances), and **the two bots
never touch the same market** (self-matching between accounts you own is wash
trading — banned, and rightly so). Expected earnings at that scale: coffee
money. The deliverable is the measured yield curve, not the yield.

## Run

```
pip install requests
python quotebot.py --check     # offline self-test
python quotebot.py             # dashboard on :8778
python quotebot.py --headless  # no browser tab (service mode)
```

`quotebot_config.json` / `quotebot_state.json` persist next to the file
(gitignored). `watchdog_heal.ps1` + a 10-minute Task Scheduler entry keep the
paper run alive unattended (it kills/relaunches by command line, so it never
touches the copybot's process — and vice versa).

## Simulation (`sim.py`) — the two-week answer in one minute

`python sim.py` runs ~150 Monte-Carlo 14-day episodes per scenario through the
**same functions the live bot runs** (scoring, fills, inventory, ledger — only
flow/jumps/competition are synthetic, calibrated from the four live watched
markets: $100–625/day budgets, 47–837 prints/day, σ 2.6–7.2¢/day, 12¢ worst
weekly jump). Net per 14 days on $400, across the two axes that decide
everything:

| competition \ toxicity | 0¢ (naive) | 1¢ | 2¢ (edge eaten) | 3¢ (toxic) |
|---|---|---|---|---|
| 4 hobby makers | +$3,983 | +$3,885 | +$3,060 | +$2,370 |
| measured secondary books | +$1,812 | +$1,217 | +$603 | +$101 (64% green) |
| one pro (5k sh @ 1¢) | +$1,271 | +$652 | +$89 (73%) | −$377 (11%) |
| saturated pro band | +$1,027 | +$547 | −$17 (45%) | −$604 (3%) |

What the grid teaches: **rewards share is the whole game** (competition
compresses it 150×), **toxicity flips the sign** right around the quoted edge,
and jump risk is a rounding error at 2¢ distance. A $153k LP account we
measured earns ~0.1%/day on capital — consistent with equilibrium bands
sitting near the bottom-right, where new entrants earn ≈0. The upper-left
cells are transient first-mover niches; they are real (the scanner found one
live) and they do not last.

Two numbers the sim cannot know, and how they get pinned: **fill toxicity** —
the live paper markouts measure it within days; **CAL** (whether
`rewards_daily_rate` pays literal dollars, and above what thresholds) — one
~$50 real qualifying quote resting for 48h reads the actual payout. Model
honesty note: we first modeled toxicity as a post-fill price shove and the
grid said toxicity was *profitable* — iid flow turns a shove into oscillation
a grid quoter harvests. That model was discarded for a transparent per-fill EV
haircut; the failed version is documented in the source as a warning.

## Honest limitations

- the reward pool split can't see per-maker order pairing in the aggregate
  book, so the share estimate is approximate in crowded books (usually
  *understated* for us); phase 1 calibrates
- `rewards_daily_rate` units are assumed $/day (consistent with the $100–600/d
  budgets observed on majors); the CAL knob absorbs any surprise
- a paper quote can't move the queue or the market: real quoting attracts
  quote-competition and informed flow that a simulation can't fully price —
  paper results are an *upper bound*, which is exactly why a red paper number
  is a hard no-go

MIT. Not financial advice; at this scale, barely financial at all.
