"""Rule tests use deterministic public-data fixtures and never perform I/O."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import math
import unittest

from crypto_monitor.engine import canonical_url, evaluate

NOW = datetime(2026, 10, 21, 1, 0, tzinfo=timezone.utc)


def iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


def config():
    return {"sources": {
        "news_feeds": [
            {"name": "SEC", "primary": True, "category": "regulatory", "item_domains": ["www.sec.gov"]},
            {"name": "Protocol", "primary": True, "category": "security", "item_domains": ["example.org"]},
            {"name": "Treasury", "primary": True, "category": "institutional", "item_domains": ["treasury.example.org"]},
            {"name": "News", "primary": False, "category": "market", "item_domains": ["news.example.org"]},
        ],
        "stablecoins": {"tether": {"peg": 1.0, "kraken_pair": "USDTUSD"}},
        "exchange_status": {"coinbase-exchange": {"name": "Coinbase status", "item_domains": ["status.coinbase.com"]}},
    }}


def snapshot(observations=(), now=NOW):
    return {"as_of": iso(now), "coins": [
        {"id": "bitcoin", "symbol": "btc", "name": "Bitcoin", "market_cap_rank": 1},
        {"id": "ethereum", "symbol": "eth", "name": "Ethereum", "market_cap_rank": 2},
        {"id": "tether", "symbol": "usdt", "name": "Tether", "market_cap_rank": 3},
        {"id": "solana", "symbol": "sol", "name": "Solana", "market_cap_rank": 5},
    ], "exchanges": [{"id": "coinbase-exchange", "name": "Coinbase Exchange", "trust_score_rank": 2}],
        "observations": list(observations), "coverage": {}, "errors": []}


def baseline():
    state, events = evaluate(snapshot(), {"version": 1}, config(), NOW)
    assert events == []
    return state


def news(title="SEC adopts final rules for digital asset custody", source="SEC", now=NOW, **kw):
    hosts = {"SEC": "www.sec.gov", "Protocol": "example.org", "Treasury": "treasury.example.org", "News": "news.example.org"}
    obs = {"id": "story1", "kind": "news", "source": source,
           "source_url": f"https://{hosts[source]}/news/story1", "published_at": iso(now),
           "title": title, "summary": "", "assets": [], "primary": source != "News", "data": {}}
    obs.update(kw)
    return obs


def depeg(price=.98, other=.98, now=NOW):
    return {"id": "tether-price", "kind": "depeg", "source": "CoinGecko + Kraken",
            "source_url": "https://www.coingecko.com/en/coins/tether", "published_at": iso(now),
            "title": "Tether prices", "summary": "", "assets": ["tether"], "primary": False,
            "data": {"coin_id": "tether", "price": price, "independent_price": other, "peg": 1.0,
                     "observed_at": iso(now), "price_observed_at": iso(now), "independent_observed_at": iso(now), "independent_source_url": "https://api.kraken.com/0/public/Trades?pair=USDTUSD"}}


def liquidation(amount=100_000_000, symbol="BTC", now=NOW):
    return {"id": "liq", "kind": "liquidation", "source": "CoinGlass",
            "source_url": "https://www.coinglass.com/LiquidationData", "published_at": iso(now),
            "title": "Actual liquidations", "summary": "", "assets": [symbol], "primary": False,
            "data": {"symbol": symbol, "total_usd": amount, "window_seconds": 3600,
                     "window_start": iso(now - timedelta(hours=1)), "window_end": iso(now)}}


def incident(status="investigating", impact="major", now=NOW):
    return {"id": "x", "kind": "exchange_incident", "source": "Coinbase status",
            "source_url": "https://status.coinbase.com/incidents/x", "published_at": iso(now),
            "title": "Trading interruption", "summary": "Trading is unavailable.", "assets": [], "primary": True,
            "data": {"exchange_id": "coinbase-exchange", "incident_id": "x", "status": status, "impact": impact}}


class EngineTest(unittest.TestCase):
    def run_at(self, state, observations, minutes=0, cfg=None):
        now = NOW + timedelta(minutes=minutes)
        return evaluate(snapshot(observations, now), state, cfg or config(), now)

    def test_bootstrap_quiet_and_inputs_unchanged(self):
        snap, state, cfg = snapshot([news(), liquidation(), incident()]), {"version": 1}, config()
        originals = deepcopy((snap, state, cfg))
        result, events = evaluate(snap, state, cfg, NOW)
        self.assertEqual(events, [])
        self.assertTrue(result["engine"]["initialized"])
        self.assertEqual((snap, state, cfg), originals)
        self.assertEqual(evaluate(snap, result, cfg, NOW)[1], [])

    def test_invalid_initial_snapshot_does_not_initialize(self):
        state, events = evaluate({"as_of": "invalid"}, {}, config(), NOW)
        self.assertEqual((state, events), ({}, []))
        state, events = evaluate({"as_of": iso(NOW), "observations": []}, state, config(), NOW)
        self.assertFalse(state["engine"].get("initialized"))
        state, events = self.run_at(state, [news()])
        self.assertEqual(events, [])

    def test_aware_clock_required(self):
        with self.assertRaises(ValueError):
            evaluate(snapshot(), {}, config(), datetime(2026, 1, 1))

    def test_final_regulation_is_alert(self):
        _, events = self.run_at(baseline(), [news()])
        self.assertEqual(len(events), 1)
        self.assertTrue(events[0]["alert"])
        self.assertEqual(events[0]["sources"][0]["url"], "https://www.sec.gov/news/story1")

    def test_primary_flag_does_not_create_trust(self):
        for changes in ({"source": "Unlisted"}, {"source_url": "https://evil.test/story"},
                        {"source_url": "https://www.sec.gov.evil.test/story"}, {"primary": False}):
            with self.subTest(changes=changes):
                obs = news()
                obs.update(changes)
                _, events = self.run_at(baseline(), [obs])
                self.assertFalse(any(event["alert"] for event in events))

    def test_headline_cannot_override_publisher_role(self):
        obs = news("SEC approves Bitcoin spot ETF", source="Protocol", data={"category": "regulatory"})
        _, events = self.run_at(baseline(), [obs])
        self.assertEqual(len(events), 1)
        self.assertFalse(events[0]["alert"])

    def test_rumors_questions_denials_historical_howto_are_not_alerts(self):
        titles = [
            "SEC could adopt final rules for digital assets", "SEC approves Bitcoin spot ETF?",
            "Rumor: SEC approves Bitcoin ETF", "SEC denies it approved Bitcoin ETF",
            "How to understand SEC final rules for digital assets", "SEC adopts proposed rules for crypto",
            "SEC approves Bitcoin ETF: a 2024 recap", "SEC will approve Bitcoin ETF",
            "SEC considers final rules for crypto", "SEC seeks approval for Bitcoin ETF",
            "SEC approves Bitcoin ETF simulation", "SEC final crypto rules: explainer",
        ]
        for title in titles:
            with self.subTest(title=title):
                _, events = self.run_at(baseline(), [news(title)])
                self.assertFalse(any(event["alert"] for event in events))

    def test_ordinary_new_news_is_digest_only(self):
        _, events = self.run_at(baseline(), [news("Bitcoin ETF records strong institutional interest", source="News")])
        self.assertEqual(len(events), 1)
        self.assertFalse(events[0]["alert"])

    def test_secondary_generic_price_promotion_and_explainer_are_suppressed(self):
        for title in ("Bitcoin climbs three percent", "Bitcoin ETF explainer", "Opinion: Bitcoin institutional adoption",
                      "Sponsored: Bitcoin fund launches giveaway", "Bitcoin ETF price prediction for next year"):
            with self.subTest(title=title):
                self.assertEqual(self.run_at(baseline(), [news(title, source="News")])[1], [])
        _, events = self.run_at(baseline(), [news("Coinbase Exchange withdrawals suspended after exchange outage", source="News")])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["category"], "exchange")
        self.assertFalse(events[0]["alert"])

    def test_scope_top50_and_crypto_regulation(self):
        outside = news("Rarecoin security exploit drains $20 million", source="Protocol")
        inside = news("Solana security exploit drains $20 million", source="Protocol", source_url="https://example.org/inside")
        _, events = self.run_at(baseline(), [outside, inside])
        self.assertEqual(len(events), 1)
        self.assertTrue(events[0]["alert"])
        self.assertIn("Solana", events[0]["title"])

    def test_outage_retains_previous_universe(self):
        snap = snapshot([news("Solana exploit drains $20 million", source="Protocol")])
        snap["coins"] = []
        _, events = evaluate(snap, baseline(), config(), NOW)
        self.assertTrue(events[0]["alert"])

    def test_tracked_coins_outside_dynamic_top50_are_removed(self):
        state = baseline()
        snap = snapshot([news("Solana exploit drains $20 million", source="Protocol")])
        snap["coins"][-1]["market_cap_rank"] = 60
        _, events = evaluate(snap, state, config(), NOW)
        self.assertEqual(events, [])

    def test_deduplicate_tracking_url_and_same_title_across_sources(self):
        first = news(source_url="https://www.sec.gov/news/a?utm_source=x#top")
        duplicate = news(source_url="https://www.sec.gov/news/a/?utm_campaign=y")
        copied = news(source="News", source_url="https://news.example.org/copy")
        state, events = self.run_at(baseline(), [first, duplicate, copied])
        self.assertEqual(len(events), 1)
        self.assertEqual(self.run_at(state, [first, copied])[1], [])

    def test_low_confidence_can_upgrade_once_with_official_confirmation(self):
        headline = "SEC approves Bitcoin ETF"
        low = news(headline, source="News")
        high = news(headline)
        state, events = self.run_at(baseline(), [low])
        self.assertFalse(events[0]["alert"])
        self.assertTrue(events[0]["summary"].startswith("Secondary-source report; official confirmation not established by this monitor."))
        lead_id = events[0]["id"]
        self.assertNotIn("supersedes", events[0])
        state, events = self.run_at(state, [high])
        self.assertTrue(events[0]["alert"])
        self.assertEqual(events[0]["supersedes"], [lead_id])
        self.assertFalse(events[0]["summary"].startswith("Secondary-source report;"))
        self.assertEqual(self.run_at(state, [high])[1], [])

    def test_same_url_minor_text_revision_does_not_notify(self):
        state, _ = self.run_at(baseline(), [news("Bitcoin ETF shows mixed fund flows", source="News")])
        _, events = self.run_at(state, [news("Bitcoin ETF shows slightly mixed fund flows", source="News")])
        self.assertEqual(events, [])

    def test_old_news_digest_only_and_ancient_news_ignored(self):
        _, events = self.run_at(baseline(), [news(now=NOW - timedelta(hours=7))])
        self.assertFalse(events[0]["alert"])
        _, events = self.run_at(baseline(), [news(now=NOW - timedelta(hours=49))])
        self.assertEqual(events, [])

    def test_stale_future_and_missing_dates_fail_closed(self):
        for published in ("bad", None, "2026-10-21T01:00:00", iso(NOW + timedelta(minutes=3))):
            _, events = self.run_at(baseline(), [news(published_at=published)])
            self.assertEqual(events, [])
        snap = snapshot([news()], NOW - timedelta(minutes=21))
        self.assertEqual(evaluate(snap, baseline(), config(), NOW)[1], [])

    def test_regulatory_small_case_not_major(self):
        for amount, expected in (("$5 million", False), ("$100 million", True)):
            obs = news(f"SEC charges crypto exchange in {amount} fraud")
            _, events = self.run_at(baseline(), [obs])
            self.assertEqual(events[0]["alert"], expected)

    def test_security_materiality_and_denials(self):
        cases = [
            ("Solana exploit confirmed: $10 million stolen", "", True),
            ("Solana exploit confirmed: $1 million stolen", "", False),
            ("Solana security breach confirmed", "Trading suspended after the breach.", True),
            ("Solana investigating suspected hack", "Withdrawals suspended.", False),
            ("Solana hack denied", "$20 million moved; no funds were lost.", False),
            ("Solana security breach confirmed", "Our market cap is $20 million.", False),
        ]
        for title, summary, expected in cases:
            with self.subTest(title=title):
                _, events = self.run_at(baseline(), [news(title, source="Protocol", summary=summary)])
                self.assertEqual(events[0]["alert"], expected)

    def test_institutional_large_completed_purchase_only(self):
        for title, expected in (("Treasury purchased $1 billion in Bitcoin", True),
                                ("Treasury purchased $50 million in Bitcoin", False),
                                ("Treasury plans to purchase $1 billion in Bitcoin", False),
                                ("Bitcoin ETF sees $1 billion net inflows", False)):
            _, events = self.run_at(baseline(), [news(title, source="Treasury")])
            self.assertEqual(events[0]["alert"], expected)

    def test_large_unrelated_amount_is_not_transaction_or_security_loss(self):
        cases = [
            news("Solana exploit confirmed", source="Protocol", summary="Protocol market cap is $100 million."),
            news("Solana security breach confirmed", source="Protocol", summary="A $20 million bounty is available for stolen funds."),
            news("Treasury purchased Bitcoin", source="Treasury", summary="The company manages $2 billion."),
            news("Treasury with $2 billion purchased Bitcoin", source="Treasury"),
            news("Treasury purchased Bitcoin; market cap is $2 billion", source="Treasury"),
        ]
        for observation in cases:
            with self.subTest(title=observation["title"]):
                _, events = self.run_at(baseline(), [observation])
                self.assertFalse(events[0]["alert"])

    def test_official_confirmation_wins_over_copy_within_same_snapshot(self):
        _, events = self.run_at(baseline(), [news("SEC approves Bitcoin ETF", source="News"), news("SEC approves Bitcoin ETF")])
        self.assertEqual(len(events), 1)
        self.assertTrue(events[0]["alert"])
        self.assertEqual(events[0]["sources"][0]["name"], "SEC")

    def test_impact_is_cautious_explanation_not_severity_label(self):
        _, events = self.run_at(baseline(), [news(), incident(), liquidation()])
        for event in events:
            self.assertGreater(len(event["impact"]), 60)
            self.assertNotIn(event["impact"], {"major", "critical", "material"})

    def test_exchange_major_minor_and_status_updates(self):
        state, events = self.run_at(baseline(), [incident(impact="minor")])
        self.assertFalse(events[0]["alert"])
        state, events = self.run_at(state, [incident(impact="major")])
        self.assertTrue(events[0]["alert"])
        state, events = self.run_at(state, [incident(status="identified")])
        self.assertFalse(events[0]["alert"])
        state, events = self.run_at(state, [incident(status="identified", impact="critical")])
        self.assertTrue(events[0]["alert"])
        self.assertEqual(self.run_at(state, [incident(status="identified", impact="critical")])[1], [])
        state, events = self.run_at(state, [incident(status="resolved", impact="critical")])
        self.assertFalse(events[0]["alert"])
        _, events = self.run_at(state, [incident()])
        self.assertTrue(events[0]["alert"])

    def test_exchange_not_top10_or_not_official_is_rejected(self):
        for change in ({"primary": False}, {"source": "Someone"}, {"source_url": "https://example.org/x"}):
            obs = incident()
            obs.update(change)
            self.assertEqual(self.run_at(baseline(), [obs])[1], [])
        snap = snapshot([incident()])
        snap["exchanges"] = [{"id": "other", "name": "Other", "trust_score_rank": 1}]
        self.assertEqual(evaluate(snap, baseline(), config(), NOW)[1], [])

    def test_depeg_requires_two_sources_same_direction(self):
        for other in (1.0, 1.02, None, float("nan")):
            state, _ = self.run_at(baseline(), [depeg(other=other)])
            _, events = self.run_at(state, [depeg(other=other, now=NOW + timedelta(hours=1))], 60)
            self.assertEqual(events, [])

    def test_depeg_confirmed_once_after_30_minutes(self):
        state, events = self.run_at(baseline(), [depeg()])
        self.assertEqual(events, [])
        state, events = self.run_at(state, [depeg(now=NOW + timedelta(minutes=29))], 29)
        self.assertEqual(events, [])
        state, events = self.run_at(state, [depeg(now=NOW + timedelta(minutes=30))], 30)
        self.assertEqual(len(events), 1)
        self.assertTrue(events[0]["alert"])
        self.assertEqual(len(events[0]["sources"]), 2)
        self.assertEqual(self.run_at(state, [depeg(now=NOW + timedelta(minutes=90))], 90)[1], [])

    def test_depeg_bootstrap_seeds_confirmation(self):
        state, events = evaluate(snapshot([depeg()]), {}, config(), NOW)
        self.assertEqual(events, [])
        _, events = self.run_at(state, [depeg(now=NOW + timedelta(hours=1))], 60)
        self.assertTrue(events[0]["alert"])

    def test_depeg_old_quote_cannot_confirm_new_poll(self):
        state, _ = self.run_at(baseline(), [depeg()])
        self.assertEqual(self.run_at(state, [depeg()], 60)[1], [])
        self.assertEqual(self.run_at(state, [depeg()], 0)[1], [])

    def test_depeg_confirmation_gap_resets(self):
        state, _ = self.run_at(baseline(), [depeg()])
        state, events = self.run_at(state, [depeg(now=NOW + timedelta(minutes=121))], 121)
        self.assertEqual(events, [])
        _, events = self.run_at(state, [depeg(now=NOW + timedelta(minutes=181))], 181)
        self.assertTrue(events[0]["alert"])

    def test_depeg_recovery_requires_two_samples_rearms(self):
        state, _ = self.run_at(baseline(), [depeg()])
        state, _ = self.run_at(state, [depeg(now=NOW + timedelta(minutes=60))], 60)
        state, events = self.run_at(state, [depeg(1, 1, now=NOW + timedelta(minutes=120))], 120)
        self.assertEqual(events, [])
        state, events = self.run_at(state, [depeg(1, 1, now=NOW + timedelta(minutes=180))], 180)
        self.assertFalse(events[0]["alert"])
        state, events = self.run_at(state, [depeg(now=NOW + timedelta(minutes=240))], 240)
        self.assertEqual(events, [])
        _, events = self.run_at(state, [depeg(now=NOW + timedelta(minutes=300))], 300)
        self.assertTrue(events[0]["alert"])

    def test_depeg_outage_does_not_reset_active_episode(self):
        state, _ = self.run_at(baseline(), [depeg()])
        state, _ = self.run_at(state, [depeg(now=NOW + timedelta(minutes=60))], 60)
        state, _ = self.run_at(state, [], 180)
        _, events = self.run_at(state, [depeg(now=NOW + timedelta(minutes=240))], 240)
        self.assertEqual(events, [])

    def test_depeg_critical_escalation_needs_confirmation(self):
        state, _ = self.run_at(baseline(), [depeg()])
        state, _ = self.run_at(state, [depeg(now=NOW + timedelta(minutes=60))], 60)
        state, events = self.run_at(state, [depeg(.94, .94, NOW + timedelta(minutes=120))], 120)
        self.assertEqual(events, [])
        _, events = self.run_at(state, [depeg(.94, .94, NOW + timedelta(minutes=180))], 180)
        self.assertEqual(events[0]["severity"], "critical")

    def test_depeg_rejects_missing_mapping_untrusted_source_and_non_usd(self):
        for change in ({"source": "Fake"}, {"source_url": "https://fake.test/tether"}):
            obs = depeg()
            obs.update(change)
            state, _ = self.run_at(baseline(), [obs])
            self.assertEqual(state["engine"]["depegs"], {})
        for mapping in ({}, {"tether": {"peg": 1.1, "kraken_pair": "USDTUSD"}}, {"tether": {"peg": 1, "kraken_pair": None}}):
            cfg = config()
            cfg["sources"]["stablecoins"] = mapping
            state, _ = self.run_at(baseline(), [depeg()], cfg=cfg)
            self.assertEqual(state["engine"]["depegs"], {})

    def test_liquidations_actual_hour_and_huge_threshold_only(self):
        for symbol, amount, expected in (("BTC", 99_999_999, False), ("BTC", 100_000_000, True),
                                          ("ETH", 50_000_000, True), ("SOL", 1_000_000_000, False)):
            _, events = self.run_at(baseline(), [liquidation(amount, symbol)])
            self.assertEqual(bool(events), expected)
            if events:
                self.assertTrue(events[0]["alert"])

    def test_liquidation_missing_bad_window_or_nonfinite_rejected(self):
        invalids = [{"window_seconds": 86400}, {"window_end": None}, {"window_start": iso(NOW - timedelta(minutes=30))},
                    {"total_usd": float("nan")}, {"total_usd": float("inf")}, {"total_usd": -1},
                    {"window_end": iso(NOW - timedelta(minutes=21)), "window_start": iso(NOW - timedelta(minutes=81))}]
        for changes in invalids:
            obs = liquidation()
            obs["data"].update(changes)
            with self.subTest(changes=changes):
                self.assertEqual(self.run_at(baseline(), [obs])[1], [])

    def test_liquidation_dedup_escalation_recovery_and_rearm(self):
        state, events = self.run_at(baseline(), [liquidation()])
        self.assertTrue(events[0]["alert"])
        state, events = self.run_at(state, [liquidation(200_000_000, now=NOW + timedelta(minutes=60))], 60)
        self.assertEqual(events, [])
        state, events = self.run_at(state, [liquidation(300_000_000, now=NOW + timedelta(minutes=120))], 120)
        self.assertEqual(events[0]["severity"], "critical")
        state, events = self.run_at(state, [liquidation(10_000_000, now=NOW + timedelta(minutes=180))], 180)
        self.assertEqual(events, [])
        state, events = self.run_at(state, [liquidation(10_000_000, now=NOW + timedelta(minutes=240))], 240)
        self.assertFalse(events[0]["alert"])
        _, events = self.run_at(state, [liquidation(now=NOW + timedelta(minutes=300))], 300)
        self.assertTrue(events[0]["alert"])

    def test_liquidation_no_price_proxy(self):
        snap = snapshot()
        snap["coins"][0]["price_change_percentage_24h"] = -30
        self.assertEqual(evaluate(snap, baseline(), config(), NOW)[1], [])

    def test_liquidation_latest_completed_hour_valid_at_minute17_or_delayed(self):
        for minute in (17, 21, 59):
            _, events = self.run_at(baseline(), [liquidation()], minute)
            self.assertEqual(len(events), 1)
            self.assertTrue(events[0]["alert"])
        self.assertEqual(self.run_at(baseline(), [liquidation()], 60)[1], [])
        future = liquidation(now=NOW + timedelta(hours=1))
        self.assertEqual(self.run_at(baseline(), [future], 59)[1], [])

    def test_numeric_episodes_survive_30_day_outage(self):
        state, _ = self.run_at(baseline(), [liquidation()])
        state, _ = self.run_at(state, [], 31 * 24 * 60)
        obs = liquidation(now=NOW + timedelta(days=31))
        self.assertEqual(self.run_at(state, [obs], 31 * 24 * 60)[1], [])

    def test_state_retention_and_size_bounds(self):
        state = baseline()
        state["engine"]["seen"] = {f"old-{n}": {"updated_at": iso(NOW - timedelta(days=31)), "rank": 0} for n in range(10)}
        cfg = config()
        cfg["thresholds"] = {"max_seen_items": 5}
        observations = [news(f"Bitcoin update number {n}", source="News", source_url=f"https://news.example.org/{n}") for n in range(8)]
        state, _ = self.run_at(state, observations, cfg=cfg)
        self.assertLessEqual(len(state["engine"]["seen"]), 5)
        self.assertFalse(any(key.startswith("old-") for key in state["engine"]["seen"]))

    def test_threshold_cannot_be_lowered_unsafely(self):
        cfg = config()
        cfg["thresholds"] = {"liquidation_btc_usd": 1, "security_material_usd": 1}
        _, events = self.run_at(baseline(), [liquidation(10), news("Solana hack drains $2", source="Protocol")], cfg=cfg)
        self.assertFalse(any(event["alert"] for event in events))

    def test_common_symbols_do_not_match_ordinary_news(self):
        snap = snapshot([news("Link to final rule on near term stock issuance")])
        snap["coins"].extend([
            {"id": "chainlink", "symbol": "link", "name": "Chainlink", "market_cap_rank": 10},
            {"id": "near", "symbol": "near", "name": "NEAR Protocol", "market_cap_rank": 20},
        ])
        self.assertEqual(evaluate(snap, baseline(), config(), NOW)[1], [])

    def test_depeg_requires_both_original_price_timestamps(self):
        for field in ("price_observed_at", "independent_observed_at"):
            obs = depeg()
            obs["data"].pop(field)
            state, _ = self.run_at(baseline(), [obs])
            self.assertEqual(state["engine"]["depegs"], {})
            obs = depeg()
            obs["data"][field] = iso(NOW - timedelta(minutes=11))
            state, _ = self.run_at(baseline(), [obs])
            self.assertEqual(state["engine"]["depegs"], {})

    def test_etf_provisional_flow_digest_revisions_and_no_duplicates(self):
        cfg = config()
        cfg["sources"]["etf_flows"] = {"enabled": True}
        obs = {"kind": "etf_flow", "source": "CoinGlass", "source_url": "https://www.coinglass.com/bitcoin-etf",
               "published_at": "2026-10-20T00:00:00Z", "title": "BTC ETF flow", "assets": ["bitcoin"],
               "data": {"coin_id": "bitcoin", "date": "2026-10-20", "net_flow_usd": 1e9, "final": False, "missing_issuer_values": 2}}
        state, events = self.run_at(baseline(), [obs], cfg=cfg)
        self.assertEqual(len(events), 1)
        self.assertFalse(events[0]["alert"])
        self.assertIn("provisional", events[0]["title"])
        self.assertEqual(self.run_at(state, [obs], cfg=cfg)[1], [])
        obs["data"]["net_flow_usd"] = 1.1e9
        state, events = self.run_at(state, [obs], cfg=cfg)
        self.assertEqual(len(events), 1)
        self.assertFalse(events[0]["alert"])
        obs["data"]["final"] = True
        self.assertEqual(self.run_at(state, [obs], cfg=cfg)[1], [])

    def test_etf_missing_date_current_date_and_disabled_fail_closed(self):
        cfg = config()
        cfg["sources"]["etf_flows"] = {"enabled": True}
        for date in ("2026-10-21", "2026-09-20", "bad"):
            obs = {"kind": "etf_flow", "source": "CoinGlass", "source_url": "https://www.coinglass.com/bitcoin-etf",
                   "data": {"coin_id": "bitcoin", "date": date, "net_flow_usd": 1e9, "final": False, "missing_issuer_values": 0}}
            self.assertEqual(self.run_at(baseline(), [obs], cfg=cfg)[1], [])

    def test_etf_flow_materiality_and_accumulated_revisions(self):
        cfg = config()
        cfg["sources"]["etf_flows"] = {"enabled": True}
        for coin_id, threshold in (("bitcoin", 100_000_000), ("ethereum", 50_000_000)):
            with self.subTest(coin_id=coin_id):
                obs = {"kind": "etf_flow", "source": "CoinGlass", "source_url": "https://www.coinglass.com/bitcoin-etf",
                       "data": {"coin_id": coin_id, "date": "2026-10-20", "net_flow_usd": threshold - 1,
                                "final": False, "missing_issuer_values": 2}}
                state, events = self.run_at(baseline(), [obs], cfg=cfg)
                self.assertEqual(events, [])
                obs["data"]["net_flow_usd"] = threshold
                state, events = self.run_at(state, [obs], cfg=cfg)
                self.assertEqual(len(events), 1)
                first_id = events[0]["id"]
                self.assertFalse(events[0]["alert"])
                obs["data"]["missing_issuer_values"] = 0
                self.assertEqual(self.run_at(state, [obs], cfg=cfg)[1], [])
                for fraction in (1.1, 1.5, 1.9):
                    obs["data"]["net_flow_usd"] = threshold * fraction
                    state, events = self.run_at(state, [obs], cfg=cfg)
                    self.assertEqual(events, [])
                obs["data"]["net_flow_usd"] = threshold * 2
                state, events = self.run_at(state, [obs], cfg=cfg)
                self.assertEqual(len(events), 1)
                self.assertIn("revised", events[0]["title"])
                obs["data"]["net_flow_usd"] = threshold
                _, events = self.run_at(state, [obs], cfg=cfg)
                self.assertEqual(len(events), 1)
                self.assertNotEqual(first_id, events[0]["id"])

    def test_etf_disabled_untrusted_or_unsafe_threshold_cannot_emit(self):
        obs = {"kind": "etf_flow", "source": "CoinGlass", "source_url": "https://www.coinglass.com/bitcoin-etf",
               "data": {"coin_id": "bitcoin", "date": "2026-10-20", "net_flow_usd": 1e9,
                        "final": False, "missing_issuer_values": 0}}
        self.assertEqual(self.run_at(baseline(), [obs])[1], [])
        cfg = config()
        cfg["sources"]["etf_flows"] = {"enabled": True}
        bad = deepcopy(obs)
        bad["source_url"] = "https://www.coinglass.com.evil.example/bitcoin-etf"
        self.assertEqual(self.run_at(baseline(), [bad], cfg=cfg)[1], [])
        cfg["thresholds"] = {"etf_flow_btc_usd": 1}
        obs["data"]["net_flow_usd"] = 1
        self.assertEqual(self.run_at(baseline(), [obs], cfg=cfg)[1], [])

    def test_url_canonicalization_rejects_credentials_http_and_bad_port(self):
        for url in ("http://example.org/a", "https://user:pass@example.org/a", "https://example.org:8000/a", "https://example.org:bad/a", "javascript:alert(1)"):
            self.assertIsNone(canonical_url(url))
        self.assertEqual(canonical_url("https://EXAMPLE.org/a/?b=2&utm_source=x&a=1#frag"), "https://example.org/a?a=1&b=2")


if __name__ == "__main__":
    unittest.main()
