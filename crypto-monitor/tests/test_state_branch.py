"""Integration tests use temporary local Git repositories, never GitHub."""
import importlib.util
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch

_spec = importlib.util.spec_from_file_location("state_branch", Path(__file__).parents[1] / "scripts/state_branch.py")
state_branch = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(state_branch)


class StateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.remote = self.base / "remote.git"
        self.repo = self.base / "repo"
        subprocess.run(["git", "init", "--bare", str(self.remote)], check=True, capture_output=True)
        subprocess.run(["git", "init", str(self.repo)], check=True, capture_output=True)
        subprocess.run(["git", "remote", "add", "origin", str(self.remote)], cwd=self.repo, check=True, capture_output=True)
        self.store = state_branch.Store(self.repo)
        self.path, self.meta = self.base / "state.json", self.base / "metadata.json"

    def initialize(self):
        self.store.load(self.path, self.meta, initialize=True)
        self.store.save(self.path, self.meta)

    def test_absent_branch_needs_explicit_initialize(self):
        with self.assertRaises(state_branch.StateError):
            self.store.load(self.path, self.meta)
        self.initialize()
        self.store.verify(self.path, self.meta)
        self.assertEqual(json.loads(self.path.read_text()), {"version": 1})
        with self.assertRaises(state_branch.StateError):
            self.store.load(self.path, self.meta, initialize=True)

    def test_round_trip_and_unchanged_state_does_not_commit(self):
        self.initialize()
        first = self.store.tip()
        self.store.save(self.path, self.meta)
        self.assertEqual(first, self.store.tip())
        self.path.write_text(json.dumps({"version": 1, "engine": {"seen": ["event-a"]}}))
        self.store.save(self.path, self.meta)
        self.assertNotEqual(first, self.store.tip())
        other = self.base / "other.json"
        self.store.load(other, self.base / "other-meta.json")
        self.assertEqual(json.loads(other.read_text()), json.loads(self.path.read_text()))

    def test_stale_writer_cannot_overwrite(self):
        self.initialize()
        stale_path, stale_meta = self.base / "stale.json", self.base / "stale-meta.json"
        self.store.load(stale_path, stale_meta)
        self.path.write_text('{"version":1,"run":2}')
        self.store.save(self.path, self.meta)
        stale_path.write_text('{"version":1,"run":3}')
        with self.assertRaisesRegex(state_branch.StateError, "advanced"):
            self.store.save(stale_path, stale_meta)
        self.store.load(self.path, self.meta)
        self.assertEqual(json.loads(self.path.read_text())["run"], 2)

    def test_modified_unpersisted_intent_fails_verification(self):
        self.initialize()
        self.path.write_text('{"version":1,"unsaved":true}')
        with self.assertRaisesRegex(state_branch.StateError, "differ"):
            self.store.verify(self.path, self.meta)

    def test_network_or_access_error_does_not_initialize(self):
        subprocess.run(["git", "remote", "set-url", "origin", str(self.base / "missing")], cwd=self.repo, check=True)
        with self.assertRaises(state_branch.StateError):
            self.store.load(self.path, self.meta, initialize=True)
        self.assertFalse(self.path.exists())

    def test_invalid_state_and_credentials_are_rejected(self):
        for raw in (b"{}", b"null", b"{", b'{"version":true}', b'{"version":1,"url":"https://discord.com/api/webhooks/123/token"}'):
            with self.assertRaises(state_branch.StateError):
                state_branch.checked_json(raw)
        with patch.dict(os.environ, {"COINGECKO_DEMO_API_KEY": "private-key-example"}):
            with self.assertRaises(state_branch.StateError):
                state_branch.checked_json(b'{"version":1,"accident":"private-key-example"}')
        with self.assertRaises(state_branch.StateError):
            state_branch.checked_json(b" " * (state_branch.MAX_STATE_BYTES + 1))

    def test_rejected_push_does_not_change_metadata(self):
        self.initialize()
        original = self.meta.read_bytes()
        hook = self.remote / "hooks" / "pre-receive"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        self.path.write_text('{"version":1,"new":true}')
        with patch.object(state_branch.time, "sleep"):
            with self.assertRaisesRegex(state_branch.StateError, "not verified"):
                self.store.save(self.path, self.meta)
        self.assertEqual(self.meta.read_bytes(), original)

    def test_cli_plan_survives_durable_checkpoint_and_delivers_once(self):
        from crypto_monitor import cli
        self.initialize()
        cli.write_json(self.path, {"version": 1, "initialized": True, "engine": {"initialized": True}})
        self.store.save(self.path, self.meta)
        config = self.base / "config.json"
        config.write_text('{"sources":{},"schedule":{"timezone":"Asia/Taipei"}}')
        now = datetime.now(timezone.utc)
        event = {"id": "verified-incident-a", "category": "security", "title": "Confirmed incident",
                 "summary": "Official source confirms a material incident.", "assets": ["bitcoin"],
                 "impact": "Funds at risk", "sources": [{"name": "Official source", "url": "https://example.com/incident"}],
                 "published_at": now.isoformat(), "severity": "critical", "alert": True}
        snapshot = {"as_of": now.isoformat(), "coins": [{"id": "bitcoin", "symbol": "BTC", "current_price": 100,
                    "last_updated": now.isoformat(), "market_cap_rank": 1}], "exchanges": [{"id": "example"}],
                    "observations": [], "coverage": {}, "errors": []}
        plan, report = self.base / "plan.json", self.base / "report.json"
        with patch("crypto_monitor.sources.collect", return_value=snapshot), patch("crypto_monitor.engine.evaluate", side_effect=lambda snapshot, state, config, now: (state, [event])):
            result = cli.main(["plan", "--config", str(config), "--state", str(self.path), "--plan", str(plan), "--report", str(report)])
        self.assertEqual(result, 0)
        before = self.path.read_bytes()
        self.store.save(self.path, self.meta)
        self.store.verify(self.path, self.meta)
        self.assertEqual(before, self.path.read_bytes(), "Persistence must preserve the plan's state hash")
        sent = []
        fake_webhook = "https://discord.com/api/webhooks/123456789012345678/" + "x" * 40
        count = cli.deliver(self.path, plan, send=True, webhook=fake_webhook,
                            poster=lambda url, payload: (sent.append(payload) or "123456789012345678"))
        self.assertEqual(count, 1)
        self.store.save(self.path, self.meta)
        self.store.verify(self.path, self.meta)
        with self.assertRaises(ValueError):
            cli.deliver(self.path, plan, send=True, webhook=fake_webhook, poster=lambda *args: self.fail("must not replay"))
        self.assertEqual(len(sent), 1)

    def test_workflow_summary_renders_newlines_and_escapes_source_text(self):
        workflow = (Path(__file__).parents[1] / ".github/workflows/monitor.yml").read_text()
        script = textwrap.dedent(workflow.split("python - <<'PY'\n", 1)[1].rsplit("          PY", 1)[0])
        (self.base / "work").mkdir()
        hostile = "</pre><script>bad()</script><img src=x onerror=bad()>"
        (self.base / "work/report.json").write_text(json.dumps({
            "planned_messages": 1, "coverage": {"official|feed": {"status": "degraded", "detail": hostile}},
            "preview_messages": [{"content": hostile}], "universe": {"coins": [{"name": hostile}]},
            "errors": [],
        }))
        summary = self.base / "summary.md"
        result = subprocess.run([sys.executable, "-c", script], cwd=self.base, capture_output=True,
                                env={**os.environ, "GITHUB_STEP_SUMMARY": str(summary), "DRY_RUN": "true"})
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        rendered = summary.read_text()
        self.assertTrue(rendered.startswith("## Crypto monitor\n\nDry run: yes\n\n"))
        self.assertIn("| official?feed | degraded |\n", rendered)
        self.assertIn("&lt;script&gt;bad()&lt;/script&gt;", rendered)
        self.assertNotIn("<script>", rendered)
        self.assertNotIn(hostile, rendered)


if __name__ == "__main__":
    unittest.main()
