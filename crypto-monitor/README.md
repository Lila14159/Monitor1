# Crypto monitor → Discord

A dedicated Python 3.12+ repository for **09:00 and 21:00 Asia/Taipei digests** and an **hourly strict major-event watch**. It reads public market data and official announcements, keeps durable deduplication state, and sends only new material information to your Discord incoming webhook. It does not connect to or modify ChatGPT tasks.

**Start here: [step-by-step deployment](docs/setup.md).** No Python packages, trading account, exchange credentials, Discord bot, or OpenAI API key are required. A CoinGecko Demo key is recommended; measured liquidation alerts and numeric BTC/ETH ETF-flow digests require optional entitled CoinGlass access.

| Requirement | Behavior |
|---|---|
| Twice-daily digests | Local 09:00 / 21:00 slots; catch up within 3 hours; no duplicate slot and no unchanged digest |
| Hourly major events | Poll at minute 17 each hour; evidence and materiality gates; no “all clear” posts |
| Market universe | CoinGecko market-cap top 50 refreshed each run, with BTC and ETH retained explicitly |
| Exchange universe | CoinGecko trust-score top 10 refreshed each run; explicit incident-feed gaps |
| Priorities | Regulation, ETF/institutional announcements, hacks/security, USD stablecoin depegs, measured liquidations, exchange incidents |
| Verifiability | Strict news uses configured primary sources; secondary Decrypt leads stay digest-only; dated source links and identified numeric-data providers |
| Persistence | Dedicated `monitor-state` branch, serialized writers, verified intent before Discord POST, bounded JSON |
| Permissions | Public data GETs; Discord webhook POSTs; repository state commits required for persistence |
| Safe default | Scheduling off until `MONITOR_ENABLED=true`; manual runs default to dry run |

Coverage is explicit, not universal. Rankings do not imply that an official incident feed exists for every top-10 exchange or project. Official feeds can miss events or omit impact details; strict detection favors silence when evidence is insufficient. Default coverage includes relevant official announcements; optional CoinGlass adapters add measured BTC/ETH liquidations and **provisional daily BTC/ETH ETF flows for digests only**. There is no comprehensive institutional, treasury, OTC, or on-chain fund-flow feed. See [sources, keys, limits, and coverage gaps](docs/sources.md) before enabling it.

GitHub Actions is a best-effort scheduler: runs can be delayed or dropped. The two digest times are target local times, and the hourly watch is polling rather than real-time delivery. Runtime `zoneinfo` checks the local slot and retained slot IDs prevent repeats. GitHub now supports IANA `timezone` scheduling, used directly in the workflow. [GitHub schedule syntax](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#onschedule), [schedule limitations](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule).

## Local verification

From the repository folder:

```sh
python3 --version  # Python 3.12 or newer
python3 -m unittest discover -s tests -v
```

All 99 tests passed locally; see the [verification record](docs/verification.md). Tests use fixtures, fake HTTP clients, and temporary local Git repositories. They do not contact Discord or GitHub. Runtime and tests use the Python standard library; Ubuntu GitHub-hosted runners include the IANA time-zone database.

## How a run works

1. Load and validate the durable state. A missing, corrupt, inaccessible, or concurrently changed state stops the run; only explicit first-time initialization may create it.
2. Refresh the market/exchange universe, collect supported official sources, and evaluate fresh evidence. Initialization seeds existing history quietly.
3. Prepare a bounded plan, including pending Discord delivery records and digest slot reservations. A dry run writes only local preview/report files.
4. Commit and verify the exact planned state on `monitor-state` before any webhook request.
5. Send only newly planned messages and persist outcomes. Existing uncertain pending records are withheld from automatic replay.

Discord cannot participate in an atomic transaction with Git. A timeout may mean “received but acknowledgment lost.” This repository therefore favors duplicate suppression: ambiguous attempts are not automatically retried. A crash after intent persistence can also suppress an unsent message. It does **not** promise exactly-once or guaranteed delivery. Recovery guidance is in the [operations runbook](docs/operations.md).

## Files

| Path | Purpose |
|---|---|
| `config.json` | Public source configuration, thresholds, schedule, and safety bounds |
| `crypto_monitor/sources.py` | Read-only source adapters and coverage reporting |
| `crypto_monitor/engine.py` | Pure event detection and deduplication |
| `crypto_monitor/scheduler.py` | Time-zone-aware digest eligibility |
| `crypto_monitor/cli.py` | Prepare plans and deliver exact persisted plans |
| `crypto_monitor/http.py` / `delivery.py` | Bounded GETs and the dedicated Discord POST path |
| `scripts/state_branch.py` | Isolated, fast-forward-only state persistence |
| `.github/workflows/monitor.yml` | Opt-in schedule and manual dry run/initialization |
| `.github/workflows/ci.yml` | Offline tests without secrets and read-only GitHub permissions |
| `tests/` | Detection, scheduling, delivery, source, and persistence checks |

The monitor has no buy/sell, withdrawal, signing, wallet, account-management, or arbitrary external-action code. Configuration contains no credentials. Action dependencies are pinned to verified commits; updates should be reviewed and tested.
