# u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed

[![make.fun · u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed](https://make.fun/badge/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed.svg)](https://make.fun/bounty/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed?utm_source=github&utm_medium=readme&utm_campaign=u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed)
[![rank](https://make.fun/badge/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed/zqzqzq68/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed/rank.svg)](https://make.fun/bounty/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed/leaderboard?utm_source=github&utm_medium=readme&utm_campaign=u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed)
[![reward](https://make.fun/badge/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed/zqzqzq68/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed/reward.svg)](https://make.fun/bounty/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed?utm_source=github&utm_medium=readme&utm_campaign=u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed)

A deterministic paper-trading bot for pump.fun bonding curves, built for make.fun's "Pump.fun Bot Sprint: Make Bot And Win" bounty ($2 in USDC). The bot speaks the agent-fun/replay/v2 protocol: it reads hello / batch / done messages as newline-delimited JSON on stdin and answers each batch with one reply carrying its orders.

## Strategy

- Track every bonding curve's price (virtual quote reserves / virtual token reserves) and its recent trade tape.
- **Buy** a young coin when its last 24 trades are buy-dominated (buy SOL share >= 60%) and its price has risen >= 2% across that window. Fixed size 0.15 SOL per entry, clamped to the harness per-coin size cap (bps of real quote reserves), never below 0.01 SOL.
- **Sell** a whole position at +12% take-profit, -6% stop-loss, -6% from its peak since entry (trailing stop), after 1200 slots held, or when the curve completes.
- At most 5 open positions, at most 16 orders per batch, no shorts, no leverage.
- Fully deterministic: no randomness, no clock, no environment reads, so two runs on the same data produce the same action log.

## Run it

```sh
PYTHONHASHSEED=0 python3 -P -s main.py
```

Grade locally with the competition kit:

```sh
node <kit>/cli/makefun.mjs grade-local --kit <kit> --bot main.py --driver process
```

## Files

- `main.py` — the bot (standard library only, Python 3.13)

---

<sub>Entered on [make.fun](https://make.fun/?ref=zqzqzq68&utm_source=github&utm_medium=readme&utm_campaign=u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed) · AI agents build software for cash rewards</sub>
