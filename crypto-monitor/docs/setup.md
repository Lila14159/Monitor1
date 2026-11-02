# Deploy in your own dedicated repository

These steps create a separate monitor. Existing ChatGPT tasks remain unchanged. Nothing has been deployed or posted by the delivered repository itself.

## 1. Create a fresh private GitHub repository

Create a new empty repository, for example `crypto-monitor`, with no initial README or license. Private is a sensible default because delivery metadata and configuration will live there. Copy the **contents** of this folder, including `.github`, into the repository root. Do not put the monitor one directory below the root.

Using Git from this folder, replace `YOUR_ACCOUNT` with your account or organization:

```sh
git init -b main
git add .
git commit -m "Add public-data crypto monitor"
git remote add origin https://github.com/YOUR_ACCOUNT/crypto-monitor.git
git push -u origin main
```

Authenticate using your normal GitHub client. The deployed monitor uses GitHub's automatically issued `GITHUB_TOKEN`; you do not need to create a personal access token for scheduled state writes.

## 2. Run the offline checks

In GitHub → **Actions**, confirm the **Tests** workflow passes. To run the same tests locally with Python 3.12 or newer:

```sh
python3 -m unittest discover -s tests -v
```

No package installation or API keys are needed for tests. Review `config.json` and [source coverage](sources.md) before enabling live runs.

## 3. Create a Discord incoming webhook

In the intended Discord server, open **Server Settings → Integrations → Webhooks**, create a webhook, choose the target text channel, and copy the webhook URL. You need permission to manage webhooks. A private test channel is useful during setup. [Discord setup instructions](https://support.discord.com/hc/en-us/articles/228383668-Intro-to-Webhooks).

Use the ordinary webhook URL ending in its token. Do not append `/github`: this application sends Discord's native JSON format. Keep the URL private; it grants permission to post to the selected channel. No Discord bot or user token is needed.

## 4. Add GitHub Actions secrets

Open **Repository → Settings → Secrets and variables → Actions → Secrets → New repository secret**:

| Secret name | Value | Needed for |
|---|---|---|
| `DISCORD_WEBHOOK_URL` | The complete incoming webhook URL | Discord posting |
| `COINGECKO_DEMO_API_KEY` | Your CoinGecko Demo API key | Recommended stable public-market API access |
| `COINGLASS_API_KEY` | Your entitled CoinGlass V4 API key | Optional real liquidation data and provisional daily ETF flows |
| `SEC_USER_AGENT` | Your application name and your real contact email, supplied by you | Optional SEC request identification; recommended for its official feed |

Obtain the Demo key from your [CoinGecko API dashboard](https://www.coingecko.com/en/developers/dashboard). CoinGlass's configured **one-hour liquidation interval requires Startup or higher** under the current endpoint documentation; Hobbyist is limited to intervals of four hours or longer. The daily ETF endpoints currently support Hobbyist and higher. Verify your account's current entitlement before subscribing. [Liquidation interval limits](https://docs.coinglass.com/reference/aggregated-liquidation-history), [Bitcoin ETF access](https://docs.coinglass.com/reference/etf-flows-history), [Ethereum ETF access](https://docs.coinglass.com/reference/ethereum-etf-flows-history).

Adding the CoinGlass secret alone does not enable those adapters. In `config.json`, set `sources.liquidations.enabled` and/or `sources.etf_flows.enabled` to `true`, commit that change, and run a dry run. Both default to `false`. Missing or disabled optional sources are shown as unavailable; ETF values stay labeled provisional and cannot cause strict alerts. The default setup has no exchange trading credentials and no OpenAI key.

`SEC_USER_AGENT` is an identifying header, not an API key. Supply your own application identity and a contact address you control; no identity is prefilled. It is sent only for the SEC feed request. The SEC can still return HTTP 403 because of its access policies or a shared runner IP; the monitor reports unavailable coverage and does not bypass the denial. [SEC automated-access guidance](https://www.sec.gov/about/webmaster-frequently-asked-questions).

Only the delivery step receives the Discord secret. Source keys are supplied only to the collection step. Do not put any key in `config.json`, workflow YAML, a command-line argument, or a committed `.env` file. [GitHub Actions secrets](https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/use-secrets).

## 5. Allow the state branch to be written

In **Settings → Actions → General**, allow the pinned GitHub-owned `actions/checkout` and `actions/setup-python` actions. Repository/organization policies must allow this monitor job's explicit `contents: write` permission. The Tests workflow stays `contents: read`.

The helper writes only one JSON file on `monitor-state`, using the repository's short-lived GitHub token. The `contents: write` permission itself applies to repository contents and is **not technically restricted to that branch**; use a dedicated repository and review all code changes. Keep normal review/branch-protection rules on `main`. Rules requiring pull requests or signed commits on **all** branches can block automatic state commits; scope those rules to the code branch or explicitly permit this state writer. A blocked state write stops delivery.

The monitor accepts manual runs only on the repository's default branch and checks out that branch explicitly. Scheduled workflows also require the workflow file on the default branch. [GitHub scheduled event behavior](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule).

## 6. Preview initialization

Leave `MONITOR_ENABLED` unset for now. Open **Actions → Crypto monitor → Run workflow**, choose `main` (or your configured default branch), and set:

| Input | Value |
|---|---|
| `dry_run` | `true` |
| `initialize` | `true` |

This reads public data and summarizes coverage without writing the remote state or posting. Confirm the run succeeds, the market universe includes at least 50 coins and 10 exchanges, and the expected sources have coverage. Degraded/unavailable status should be understood from [coverage limitations](sources.md). Core market/exchange failure stops initialization.

## 7. Initialize durable state quietly

Run **Crypto monitor** again, now with `dry_run=false` and `initialize=true`. This intentionally creates `monitor-state` and seeds existing news/incidents. **Initialization does not post a backlog or a test message.** Confirm the job succeeds and the `monitor-state` branch contains `state.json`.

Initialization is one-time. Reusing `initialize=true` when state exists is rejected to prevent accidental history loss. For later previews, use `dry_run=true`, `initialize=false`.

## 8. Enable the schedule

In **Settings → Secrets and variables → Actions → Variables**, add the repository variable:

```text
MONITOR_ENABLED = true
```

The exact lowercase string `true` enables scheduled runs. The workflow requests:

```yaml
schedule:
  - cron: '0 9,21 * * *'
    timezone: Asia/Taipei
  - cron: '17 * * * *'
    timezone: Asia/Taipei
```

Taiwan local 09:00 / 21:00 correspond to 01:00 / 13:00 UTC under its current time-zone rules. Both GitHub and Python use `Asia/Taipei`, not your computer's zone or a manually adjusted DST offset. If moving to another time zone later, update both schedule entries and `config.json` and rerun scheduler tests. [GitHub IANA scheduling](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#onschedule).

Expect 26 scheduled invocations per day: 24 hourly checks plus two digest checks. The hourly check at 09:17/21:17 can catch a delayed digest slot; persisted slot IDs prevent another post for that slot. Catch-up is bounded to three hours. GitHub may delay/drop runs, so this is not an exact-time or real-time service.

## 9. Confirm operation and maintain it

Review the next **Crypto monitor** run's coverage table and counters. A successful run can send nothing: unchanged information is intentionally silent, and quiet initialization seeds a baseline. Ordinary market changes appear in a digest only when configured materiality is reached. A manual run with `dry_run=false`, `initialize=false` may send eligible new messages immediately even while the schedule variable is off; it is an explicit live run, not a forced test message.

Keep GitHub workflow-failure notifications enabled and check the Actions page periodically. If no runner starts, this monitor cannot send an outage notice about its own absence. Review API usage and your GitHub Actions spending limit. See [operations and recovery](operations.md).

To pause, set `MONITOR_ENABLED=false` or disable **Crypto monitor** in Actions. Wait for any active run to finish; avoid interrupting the persistence/delivery sequence. To revoke posting immediately, delete or regenerate the Discord webhook and update the secret when ready. Pausing this repository has no effect on ChatGPT tasks.
