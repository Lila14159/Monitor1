# Verification record

Verified locally on 25 October 2026 using Python 3.14.0 and Git 2.50.1. The GitHub test workflow targets Python 3.12; that hosted workflow has not yet been run for a deployed repository.

**99 automated tests passed.** Run them from the repository root:

```sh
python3 -m unittest discover -s tests -v
```

The suite covers timezone/DST slots, late runs, quiet periods, dynamic universe changes, strict evidence gates, stale/false/duplicate observations, actual provider-to-detector depeg and ETF shapes, source outages, material flow revisions, incident recovery, pending-event supersession, digest batching, daily limits, credential redaction, URL restrictions, XML parsing, Discord acknowledgement ambiguity, and durable state replay prevention.

Persistence integration uses temporary local Git remotes. It exercises the real CLI plan, verified Git checkpoint, simulated Discord delivery, acknowledgement persistence, and replay rejection. No webhook is contacted by tests. Both workflow files passed YAML parsing; source compilation and CLI help were also checked. Documentation links within the repository were checked.

Primary API documentation and selected public feed responses were inspected. Paid CoinGlass account entitlement and full live collection from GitHub-hosted runners remain deployment checks. No real Discord posting, GitHub repository deployment, scheduled run, or trading operation occurred during this build. Existing ChatGPT tasks were not modified.

Before enabling scheduling, follow [setup](setup.md): run a read-only preview, inspect coverage, initialize state once, and then enable the repository variable. Public feed/API access can vary by network, plan, and provider changes; a passing offline test is not proof of current live availability.
