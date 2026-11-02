# Operations and recovery

## Expected behavior

The target digest slots are 09:00 and 21:00 in `Asia/Taipei`; hourly checks run at minute 17. An eligible digest slot can catch up within three hours, using a durable local date/hour ID. Each slot reserves at most one bounded digest batch; overflow waits for the next slot. If alerts consume the whole run allowance before any digest is reserved, a later run within the grace window can still try. Empty or unchanged digests are silent. Hourly checks post only newly detected, high-confidence material events. Alerts are not repeated in the next digest. There is no periodic “nothing happened” Discord message.

GitHub scheduling is best effort. Congestion can delay or drop a run; public-repository schedules may be disabled after 60 days without repository activity. Repository billing limits or runner problems can prevent a run from starting. GitHub workflow-failure notifications and periodic checks of the Actions page are the health channel; the monitor sends no extra Discord health messages. [GitHub schedule limitations](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule).

Initialization quietly seeds existing news, incidents, numerical observations, and a price baseline. It requires a complete current market/exchange inventory, and it cannot overwrite existing state. Existing history is not announced as a new event. Newly entering top-50 assets receive a baseline when first seen.

## Strict gates and silence

| Type | Default requirement for an hourly alert |
|---|---|
| Regulatory / ETF decision | Configured primary regulator feed; final crypto rule, explicit ban, material enforcement of at least $100m, or an explicit BTC/ETH ETF approval/rejection |
| Institutional purchase | Configured official institutional/ETF feed; explicitly completed BTC/ETH purchase of at least $1bn; none is configured by default |
| Hack / security | Configured primary security/project source; explicit breach with at least $10m loss tied to that incident or a core-service halt |
| Exchange incident | Current top-10 exchange, reviewed official status feed, active major/critical incident or material escalation |
| Stablecoin depeg | Two fresh price feeds, same-direction >=1% deviation, confirmed twice at least 30 minutes apart; >=5% critical |
| Liquidations | Actual completed one-hour totals, BTC >=$100m / ETH >=$50m for configured venues; 3× critical |

News alerts also need a valid timestamp no older than six hours. Proposals, speculation, uncertainty language, old-event title references, and insufficient evidence do not pass the strict news gate. This uses conservative English text rules, not semantic fact verification; unusual phrasing or other languages can be missed, and it is not a human verification service. Non-alert eligible developments and filtered secondary Decrypt leads may appear in the next digest. Numeric daily ETF data always remains provisional/digest-only.

Price changes alone are digest context: defaults are 5% for BTC/ETH and 10% for other tracked assets from the retained material-digest baseline. Small changes accumulate against that baseline until material; this is not necessarily a 12-hour or 24-hour return. The baseline advances when a material market item is reserved for delivery. A reservation does not establish that Discord received it.

## Inspect a run

Open **Actions → Crypto monitor → a run → Summary**. It shows counts, source coverage, bounded escaped coverage details, the reviewed universe, and proposed message previews. These counts and messages describe the prepared plan before delivery, so newly planned records are included in its unresolved count. Check the **Deliver** and **Persist delivery results** steps and durable state for final outcomes; a preview is not a delivery receipt.

| Observation | Meaning and action |
|---|---|
| Green run, zero messages | Normal during quiet periods, initialization, unchanged data, or no eligible digest slot |
| Coverage unavailable | Source is disabled, lacks credentials/entitlement, or failed validation/request; inspect the named adapter and source documentation |
| Coverage degraded | Partial source scope, missing rows/issuer values, or provisional data; do not infer full coverage |
| Failed load/save/verify | No new Discord POST should proceed; check repository access, state integrity, and branch rules |
| Failed delivery | Inspect the channel and durable ledger before considering any manual recovery |
| Nonzero unresolved/uncertain count | Earlier intent was not confirmed; it will not automatically replay |
| Expired/superseded queue count | Stale unsent items expired or were replaced by a newer state; review if unexpected |
| No run starts | Check schedule opt-in, default branch, Actions settings, billing, and GitHub status |

The run report is written before initialization inventory checks so source failures remain visible. Source failures never silently reset state. A loss of all coin or exchange data makes the collection step fail and prevents delivery in that run. Partial coverage is displayed explicitly.

## Durable state and delivery semantics

`monitor-state` contains only `state.json`. It is a separate branch; no code checkout is switched during a run. One concurrency group serializes scheduled and manual monitor jobs. The helper compares the remote tip before writing and uses ordinary fast-forward pushes with verification. It never force-pushes, merges state, or overwrites a concurrent writer.

Delivery follows this order:

1. Prepare new events and assign message IDs in a local state copy.
2. Persist and verify the exact intent on the state branch.
3. POST each newly assigned message once, requesting a Discord acknowledgment.
4. Persist the acknowledgment or uncertainty result, including after a delivery failure where the job can still run cleanup.

| Ledger status | Interpretation |
|---|---|
| `pending` | Durable reserved intent; a crash may have happened before or after transmission |
| `attempted` | Local delivery began; there may be no reliable acknowledgment |
| `sent` | Discord returned a message ID and the result was saved |
| `uncertain` | Request failed, timed out, was rate limited, or acknowledgment was unusable |

Neither pending nor uncertain historical records are automatically resent. Discord and Git do not share a transaction: a crash can suppress an unsent message, and a timeout can occur after Discord accepted it. This deliberately favors avoiding duplicates and is not an exactly-once or guaranteed-delivery system. Discord acknowledgments are requested using `wait=true`. [Discord webhook execution](https://discord.com/developers/docs/resources/webhook#execute-webhook).

Event/message/digest-slot history is retained for 30 days. Unassigned events are queued for up to 72 hours, with a hard 1,000-item queue bound. Alerts older than six hours are downgraded to dated digest items; superseding resolution/escalation can replace an unsent older item. First exposure to a source may quietly seed old history. Deduplication is bounded to retained history and conservative link/title/event matching; it cannot guarantee that differently worded reports of the same event are always merged.

## Recover without erasing history

Set `MONITOR_ENABLED=false`, wait for any active run to finish, and inspect the Discord channel and `monitor-state/state.json`. In the GitHub file view, look at the `outbox` message's `status`, `created_at`, `event_ids`, and, when present, `discord_message_id`. Do not paste the webhook URL or API keys into an issue or log.

For an uncertain message, preserve its existing ledger entry. Decide manually whether the event is still relevant and whether it already appears in the channel. There is intentionally no “retry all pending” switch. If a human needs to resend it, that is a separate explicit action with duplicate risk; the monitor will not do so automatically.

For a transient state write failure, fix access/network/branch rules and run again with `initialize=false`. A new run loads the current remote state. For corruption, keep the existing branch and its history, export the evidence, and repair the latest state in a reviewed change. Restoring an old state can replay events or lose reservations; deleting the state branch or reinitializing is not routine recovery. The helper intentionally offers no reset/force option.

For a compromised Discord URL, regenerate or delete the webhook in Discord and update `DISCORD_WEBHOOK_URL` in GitHub. Rotate a provider key at its provider, then replace its GitHub secret. Secrets never belong in the repository or state file. A dry run is appropriate after source/configuration changes; it performs reads and local previews only.

## Safety and operating bounds

| Bound | Default / hard behavior |
|---|---|
| Application requests | HTTPS public-data GETs to reviewed hosts; no redirects or private/local IP destinations |
| Data-request budget | 60 attempts/run, 240-second collection deadline, 10-second request timeout, one bounded retry for transient GET failures |
| Provider response size | 4 MB per data response |
| Discord messages | At most 5/run and 20/local calendar day; reservations count, including uncertain deliveries |
| Discord payload | At most 1,900 characters, mentions disabled and embeds suppressed |
| Discord POST | 15-second timeout; no automatic retry, including HTTP 429 |
| Delivery plan | Valid for 30 minutes and only against the exact persisted state hash |
| Remote state | 2 MiB maximum; invalid schema or credentials in state rejected |
| Job duration | 12-minute GitHub Actions timeout |

Daily/run caps retain eligible overflow for later runs within queue/freshness bounds. Lower limits may be configured; raising config above the hard Discord caps does not increase them. Limits do not guarantee API-provider availability or stop Git history from growing over time. Monitor repository size and usage; state history is durable Git history, not an expiring cache.

The only application write destination is the explicitly enabled Discord incoming webhook. Git state commits are infrastructure persistence. There are no wallet, trade, withdrawal, transaction-signing, account, or arbitrary external-action capabilities. Protect the default branch and review workflow/configuration changes because the scheduled job runs reviewed repository code with access to its configured secrets.
