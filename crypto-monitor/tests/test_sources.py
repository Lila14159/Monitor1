import copy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from crypto_monitor import engine, sources

NOW = datetime(2026, 10, 21, 10, 7, tzinfo=timezone.utc)


def coin(coin_id="bitcoin", rank=1, **changes):
    value = {"id": coin_id, "symbol": "btc" if coin_id == "bitcoin" else coin_id,
             "name": "Bitcoin" if coin_id == "bitcoin" else coin_id.title(), "market_cap_rank": rank,
             "current_price": 100.0, "price_change_percentage_24h": 1.0,
             "last_updated": sources.iso(NOW - timedelta(minutes=2))}
    value.update(changes)
    return value


def exchange(exchange_id="gdax", rank=1):
    return {"id": exchange_id, "name": exchange_id, "trust_score_rank": rank,
            "trade_volume_24h_btc": 100.0, "url": f"https://{exchange_id}.example/"}


def complete_coins():
    return [coin("bitcoin", 1), coin("ethereum", 2)] + [coin(f"coin-{rank}", rank) for rank in range(3, 51)]


def complete_exchanges():
    return [exchange("gdax", 1)] + [exchange(f"exchange-{rank}", rank) for rank in range(2, 11)]


class FakeClient:
    def __init__(self, markets=None, exchanges=None, routes=None, raw=None):
        self.markets = complete_coins() if markets is None else markets
        self.exchanges = complete_exchanges() if exchanges is None else exchanges
        self.routes = routes or {}
        self.raw = raw or {}
        self.calls = []

    def get_json(self, url, headers=None):
        self.calls.append((url, headers))
        path = urlsplit(url).path
        query = parse_qs(urlsplit(url).query)
        if "ids" in query and path.endswith("coins/markets"):
            value = self.routes.get("pinned", [coin(c, rank=70 + i) for i, c in enumerate(query["ids"][0].split(","))])
        elif path.endswith("coins/markets"):
            value = self.markets
        elif path.endswith("exchanges"):
            value = self.exchanges
        else:
            value = self.routes.get(url, self.routes.get(path, ValueError("unconfigured fixture")))
        if isinstance(value, Exception):
            raise value
        return copy.deepcopy(value)

    def get_bytes(self, url, headers=None):
        self.calls.append((url, headers))
        value = self.raw.get(url, ValueError("unconfigured fixture"))
        if isinstance(value, Exception):
            raise value
        return value


FEED = {"name": "Regulator", "url": "https://official.example/feed.xml",
        "item_domains": ["official.example"], "primary": True, "category": "regulatory"}


def feed_xml(link="https://official.example/story", date="Wed, 21 Oct 2026 09:00:00 +0000", title="Bitcoin ETF approved"):
    return f"""<rss><channel><item><title>{title}</title><link>{link}</link>
      <pubDate>{date}</pubDate><description>&lt;b&gt;Official statement&lt;/b&gt;</description>
      </item></channel></rss>""".encode()


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"COINGECKO_DEMO_API_KEY": "", "COINGLASS_API_KEY": "", "SEC_USER_AGENT": ""})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_dynamic_universe_changes_without_hardcoded_coin_ids(self):
        rows = complete_coins()
        rows[-1] = coin("newly-top-fifty", 50)
        client = FakeClient(markets=rows, exchanges=list(reversed(complete_exchanges())))
        result = sources.collect(client, {"sources": {}}, NOW)
        self.assertIn("newly-top-fifty", [c["id"] for c in result["coins"]])
        self.assertEqual(result["coverage"]["market_universe"]["status"], "ok")
        self.assertEqual([e["trust_score_rank"] for e in result["exchanges"]], list(range(1, 11)))
        self.assertEqual(len(client.calls), 2)

    def test_pin_btc_and_eth_even_if_they_drop_out_of_top50(self):
        rows = [coin(f"asset-{rank}", rank) for rank in range(1, 51)]
        client = FakeClient(markets=rows)
        result = sources.collect(client, {"sources": {}}, NOW)
        self.assertEqual(len(result["coins"]), 52)
        self.assertTrue({"bitcoin", "ethereum"} <= {c["id"] for c in result["coins"]})
        self.assertEqual(len(client.calls), 3)

    def test_missing_or_stale_market_data_is_degraded_and_not_zero(self):
        rows = complete_coins()
        rows[-1]["last_updated"] = sources.iso(NOW - timedelta(hours=2))
        rows[-2]["current_price"] = None
        result = sources.collect(FakeClient(markets=rows), {"sources": {}}, NOW)
        self.assertEqual(len(result["coins"]), 48)
        self.assertEqual(result["coverage"]["market_universe"]["status"], "degraded")

    def test_duplicate_exchange_rank_is_not_used_as_extra_top10_member(self):
        rows = complete_exchanges()
        rows[-1]["trust_score_rank"] = 9
        result = sources.collect(FakeClient(exchanges=rows), {"sources": {}}, NOW)
        self.assertEqual(len(result["exchanges"]), 9)
        self.assertEqual(result["coverage"]["exchange_universe"]["status"], "degraded")

    def test_provider_errors_do_not_leak_secret_or_stop_other_providers(self):
        client = FakeClient(markets=RuntimeError("secret api-key super-secret"))
        result = sources.collect(client, {"sources": {}}, NOW)
        self.assertEqual(result["coins"], [])
        self.assertEqual(len(result["exchanges"]), 10)
        self.assertNotIn("super-secret", json.dumps(result))
        self.assertEqual(result["coverage"]["market_universe"]["status"], "unavailable")

    def test_missing_paid_key_explicit_unavailable_not_empty_healthy(self):
        client = FakeClient()
        result = sources.collect(client, {"sources": {"liquidations": {"enabled": True}, "etf_flows": {"enabled": True}}}, NOW)
        self.assertEqual(result["coverage"]["liquidations"]["status"], "unavailable")
        self.assertEqual(result["coverage"]["etf_flows"]["status"], "unavailable")
        self.assertFalse(any("coinglass" in url for url, _ in client.calls))

    def test_api_key_only_sent_to_own_provider_header(self):
        with patch.dict(os.environ, {"COINGECKO_DEMO_API_KEY": "fixture-secret"}):
            client = FakeClient(raw={FEED["url"]: feed_xml()})
            sources.collect(client, {"sources": {"news_feeds": [FEED]}}, NOW)
        for url, headers in client.calls:
            self.assertNotIn("fixture-secret", url)
            self.assertEqual(headers, {"x-cg-demo-api-key": "fixture-secret"} if "api.coingecko.com" in url else None)

    def test_valid_rss_has_primary_domain_and_normalized_date(self):
        items, rejected = sources.parse_feed(feed_xml(), FEED, [coin()], NOW)
        self.assertEqual(rejected, 0)
        self.assertEqual(items[0]["published_at"], "2026-10-21T09:00:00Z")
        self.assertEqual(items[0]["assets"], ["bitcoin"])
        self.assertEqual(items[0]["summary"], "Official statement")

    def test_atom_link_and_timezones(self):
        atom = b'''<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>tag:official,1</id>
          <title>BTC update</title><link href="https://official.example/item" rel="alternate"/>
          <updated>2026-10-21T17:00:00+08:00</updated><summary>Details</summary></entry></feed>'''
        items, rejected = sources.parse_feed(atom, FEED, [coin()], NOW)
        self.assertEqual(rejected, 0)
        self.assertEqual(items[0]["published_at"], "2026-10-21T09:00:00Z")

    def test_feed_domain_lookalike_credentials_http_and_missing_time_rejected(self):
        for link in ["https://official.example.evil.test/story", "https://official.example@evil.test/story",
                     "http://official.example/story", "https://official.example:8443/story"]:
            with self.subTest(link=link):
                items, rejected = sources.parse_feed(feed_xml(link=link), FEED, [coin()], NOW)
                self.assertEqual(items, [])
                self.assertEqual(rejected, 1)
        self.assertEqual(sources.parse_feed(feed_xml(date=""), FEED, [], NOW)[1], 1)

    def test_entity_expansion_and_html_error_pages_rejected(self):
        for payload in [b'<!DOCTYPE rss [<!ENTITY x "test">]><rss><channel/></rss>',
                        '<!DOCTYPE rss [<!ENTITY x "test">]><rss><channel/></rss>'.encode("utf-16"),
                        '<!DOCTYPE rss [<!ENTITY x "test">]><rss><channel/></rss>'.encode("utf-32"),
                        b'<html><body>Blocked by provider</body></html>',
                        b"x" * (sources.MAX_FEED_BYTES + 1)]:
            with self.assertRaises(ValueError):
                sources.parse_feed(payload, FEED, [], NOW)

    def test_old_and_future_articles_do_not_trigger_observations(self):
        for date in ["2026-10-01T09:00:00Z", "2026-10-22T09:00:00Z"]:
            items, _ = sources.parse_feed(feed_xml(date=date), FEED, [], NOW)
            self.assertEqual(items, [])

    def test_missing_exchange_mapping_is_explicit(self):
        result = sources.collect(FakeClient(), {"sources": {}}, NOW)
        self.assertEqual(result["coverage"]["exchange_status:gdax"]["status"], "unavailable")
        self.assertNotIn("healthy", result["coverage"]["exchange_status:gdax"]["detail"])

    def test_status_incident_canonical_url_never_follows_untrusted_shortlink(self):
        setting = {"name": "Official status", "url": "https://status.example/api/v2/incidents.json", "item_domains": ["status.example"]}
        item = {"id": "incident123", "status": "identified", "impact": "major", "name": "Withdrawals unavailable",
                "updated_at": sources.iso(NOW - timedelta(minutes=5)), "shortlink": "https://evil.test/",
                "incident_updates": [{"body": "Investigating."}]}
        client = FakeClient(routes={setting["url"]: {"incidents": [item]}})
        result = sources.collect(client, {"sources": {"exchange_status": {"gdax": setting}}}, NOW)
        observation = result["observations"][0]
        self.assertEqual(observation["source_url"], "https://status.example/incidents/incident123")
        self.assertEqual(observation["data"]["exchange_id"], "gdax")
        self.assertFalse(any("evil.test" in url for url, _ in client.calls))

    def test_kraken_requires_named_pair_fresh_time_and_multiple_trades(self):
        stable = coin("tether", 3, current_price=.97)
        setting = {"peg": 1.0, "kraken_pair": "USDTUSD", "response_pairs": ["USDTZUSD"]}
        trades = [[str(price), "100", (NOW - timedelta(seconds=30+i)).timestamp()] for i, price in enumerate([.96,.97,.98])]
        client = FakeClient(routes={"/0/public/Trades": {"error": [], "result": {"USDTZUSD": trades, "last": "123"}}})
        result = {"observations": [], "coverage": {}}
        sources._depeg(client, result, stable, setting, NOW)
        observation = result["observations"][0]
        self.assertEqual(observation["data"]["independent_price"], .97)
        self.assertEqual(observation["data"]["observed_at"], stable["last_updated"])
        self.assertEqual(observation["data"]["price_observed_at"], stable["last_updated"])
        self.assertEqual(observation["data"]["independent_observed_at"], sources.iso(NOW - timedelta(seconds=32)))
        client.routes["/0/public/Trades"]["result"] = {"UNEXPECTED": trades}
        with self.assertRaises(ValueError):
            sources._depeg(client, result, stable, setting, NOW)

    def test_stale_or_missing_independent_quote_does_not_create_depeg(self):
        trades = [[".5", "100", (NOW - timedelta(hours=1)).timestamp()]] * 3
        client = FakeClient(routes={"/0/public/Trades": {"error": [], "result": {"USDCUSD": trades}}})
        with self.assertRaises(ValueError):
            sources._depeg(client, {"observations": [], "coverage": {}}, coin("usd-coin", 3),
                           {"peg": 1.0, "kraken_pair": "USDCUSD"}, NOW)

    def test_unsupported_stablecoin_no_fake_independent_price(self):
        result = {"observations": [], "coverage": {}}
        sources._depeg(FakeClient(), result, coin("usdd", 3), {"peg": 1.0, "kraken_pair": None}, NOW)
        self.assertEqual(result["observations"], [])
        self.assertEqual(result["coverage"]["depeg:usdd"]["status"], "unavailable")

    def test_liquidation_uses_completed_hour_not_current_partial_bucket(self):
        end = NOW.replace(minute=0, second=0, microsecond=0)
        rows = [{"time": int((end - timedelta(hours=1)).timestamp()*1000), "aggregated_long_liquidation_usd": 10,
                 "aggregated_short_liquidation_usd": 20},
                {"time": int(end.timestamp()*1000), "aggregated_long_liquidation_usd": 100000000,
                 "aggregated_short_liquidation_usd": 200000000}]
        event = sources.parse_liquidations({"code": "0", "data": rows}, "BTC", ["Binance"], NOW)
        self.assertEqual(event["data"]["total_usd"], 30)
        self.assertEqual(event["data"]["window_end"], sources.iso(end))
        self.assertEqual(event["data"]["window_seconds"], 3600)

    def test_liquidation_missing_leg_old_hour_or_nan_are_not_zero(self):
        end = NOW.replace(minute=0, second=0, microsecond=0)
        valid = {"time": int((end - timedelta(hours=1)).timestamp()*1000), "aggregated_long_liquidation_usd": 10,
                 "aggregated_short_liquidation_usd": 20}
        for replacement in [{"aggregated_short_liquidation_usd": None}, {"aggregated_long_liquidation_usd": float("nan")},
                            {"time": int((end - timedelta(hours=3)).timestamp()*1000)}]:
            with self.subTest(replacement=replacement), self.assertRaises(ValueError):
                sources.parse_liquidations({"code": "0", "data": [{**valid, **replacement}]}, "ETH", ["OKX"], NOW)

    def test_etf_flow_is_explicitly_dated_provisional_and_missing_issuer_is_not_zero(self):
        day = NOW.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
        response = {"code": "0", "data": [{"timestamp": int(day.timestamp()*1000), "flow_usd": 123000000,
                      "etf_flows": [{"etf_ticker": "IBIT", "flow_usd": 123000000}, {"etf_ticker": "GBTC"}]}]}
        client = FakeClient(routes={"/api/etf/bitcoin/flow-history": response})
        result = {"observations": [], "coverage": {}, "errors": []}
        sources._etf_flows(client, result, {"assets": ["bitcoin"]}, NOW, "test-key")
        flow = result["etf_flows"][0]
        self.assertEqual(flow["date"], "2026-10-20")
        self.assertFalse(flow["final"])
        self.assertEqual(flow["source_url"], "https://www.coinglass.com/etf/bitcoin")
        self.assertEqual(flow["missing_issuer_values"], 1)
        self.assertEqual(result["coverage"]["etf_flows:bitcoin"]["status"], "degraded")
        self.assertEqual(result["observations"][0]["kind"], "etf_flow")
        self.assertEqual(result["observations"][0]["data"], flow)

    def test_etf_invalid_or_empty_breakdown_and_duplicate_date_fail_closed(self):
        day = NOW.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
        valid = {"timestamp": int(day.timestamp()*1000), "flow_usd": 123000000,
                 "etf_flows": [{"etf_ticker": "IBIT", "flow_usd": 123000000}]}
        for rows in [[{**valid, "etf_flows": []}], [valid, valid], [{**valid, "flow_usd": float("nan")}]]:
            with self.subTest(rows=rows):
                result = {"observations": [], "coverage": {}, "errors": []}
                client = FakeClient(routes={"/api/etf/bitcoin/flow-history": {"code": "0", "data": rows}})
                sources._etf_flows(client, result, {"assets": ["bitcoin"]}, NOW, "test-key")
                self.assertEqual(result["observations"], [])
                self.assertEqual(result["coverage"]["etf_flows:bitcoin"]["status"], "unavailable")

    def test_collect_to_engine_confirms_real_depeg_schema_across_two_snapshots(self):
        config = {"sources": {"stablecoins": {"tether": {"peg": 1.0, "kraken_pair": "USDTUSD",
                                                       "response_pairs": ["USDTZUSD"]}}}}
        state = {}
        for index, when in enumerate([NOW, NOW + timedelta(hours=1)]):
            rows = complete_coins()
            rows[2] = coin("tether", 3, current_price=.97)
            for row in rows:
                row["last_updated"] = sources.iso(when - timedelta(minutes=2))
            trades = [[".97", "100", (when - timedelta(seconds=30+i)).timestamp()] for i in range(3)]
            client = FakeClient(markets=rows, routes={"/0/public/Trades": {"error": [], "result": {"USDTZUSD": trades}}})
            snapshot = sources.collect(client, config, when)
            state, events = engine.evaluate(snapshot, state, config, when)
            if index == 0:
                self.assertEqual(events, [])
            else:
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0]["category"], "depeg")
                self.assertTrue(events[0]["alert"])
                self.assertEqual(len(events[0]["sources"]), 2)

    def test_collect_to_engine_emits_etf_flow_for_digest_and_suppresses_unchanged(self):
        config = {"sources": {"etf_flows": {"enabled": True, "assets": ["bitcoin"]}}}
        initial = sources.collect(FakeClient(), config, NOW)
        state, events = engine.evaluate(initial, {}, config, NOW)
        self.assertEqual(events, [])
        day = NOW.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
        response = {"code": "0", "data": [{"timestamp": int(day.timestamp()*1000), "flow_usd": 123000000,
                    "etf_flows": [{"etf_ticker": "IBIT", "flow_usd": 123000000}]}]}
        client = FakeClient(routes={"/api/etf/bitcoin/flow-history": response})
        with patch.dict(os.environ, {"COINGLASS_API_KEY": "test-key"}):
            snapshot = sources.collect(client, config, NOW)
        state, events = engine.evaluate(snapshot, state, config, NOW)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["category"], "etf")
        self.assertFalse(events[0]["alert"])
        self.assertIn("provisional", events[0]["title"])
        _, repeated = engine.evaluate(snapshot, state, config, NOW)
        self.assertEqual(repeated, [])

    def test_secondary_feed_provenance_remains_secondary_through_engine(self):
        feed = {**FEED, "primary": False, "category": "market"}
        config = {"sources": {"news_feeds": [feed]}}
        state, _ = engine.evaluate(sources.collect(FakeClient(), {"sources": {}}, NOW), {}, config, NOW)
        client = FakeClient(raw={feed["url"]: feed_xml(title="Bitcoin hack: $20 million stolen")})
        snapshot = sources.collect(client, config, NOW)
        self.assertFalse(snapshot["observations"][0]["primary"])
        _, events = engine.evaluate(snapshot, state, config, NOW)
        self.assertEqual(len(events), 1)
        self.assertFalse(events[0]["alert"])


if __name__ == "__main__":
    unittest.main()
