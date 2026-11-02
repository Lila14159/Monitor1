#!/usr/bin/env python3
"""Fail-closed persistence for one bounded JSON file on an isolated Git branch.

No checkout, force push, shell interpolation, or state merging. A concurrent writer
requires a new monitoring run from the new state, not overwriting its decisions.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

MAX_STATE_BYTES = 2 * 1024 * 1024
SHA = re.compile(r"^[0-9a-f]{40}$")
WEBHOOK = re.compile(r"https?://(?:[^/]*\.)?discord(?:app)?\.com/api(?:/v\d+)?/webhooks/", re.I)


class StateError(RuntimeError):
    pass


def checked_json(raw: bytes) -> dict:
    if len(raw) > MAX_STATE_BYTES:
        raise StateError("State exceeds 2 MiB bound")
    try:
        obj = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise StateError("State is not valid JSON") from exc
    if not isinstance(obj, dict) or type(obj.get("version")) is not int or obj["version"] != 1:
        raise StateError("State must be an object with version 1")
    if WEBHOOK.search(raw.decode("utf-8")):
        raise StateError("Refusing state containing a Discord webhook credential")
    for name in ("DISCORD_WEBHOOK_URL", "COINGECKO_DEMO_API_KEY", "COINGLASS_API_KEY", "GH_TOKEN", "GITHUB_TOKEN"):
        value = os.environ.get(name, "")
        if len(value) >= 8 and value.encode() in raw:
            raise StateError("Refusing state containing a runtime credential")
    return obj


def canonical(obj: dict) -> bytes:
    raw = (json.dumps(obj, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")) + "\n").encode()
    checked_json(raw)
    return raw


def atomic_write(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(raw)
    tmp.replace(path)


class Store:
    def __init__(self, repo: Path, branch: str = "monitor-state", remote: str = "origin"):
        if not re.fullmatch(r"[a-zA-Z0-9_-]+", branch) or not re.fullmatch(r"[a-zA-Z0-9_-]+", remote):
            raise StateError("Invalid state branch or remote name")
        self.repo, self.remote, self.ref = repo, remote, "refs/heads/" + branch

    def git(self, *args: str, data: bytes | None = None, okay=(0,)) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        env.update(GIT_TERMINAL_PROMPT="0", GIT_AUTHOR_NAME="crypto-monitor[bot]", GIT_COMMITTER_NAME="crypto-monitor[bot]",
                   GIT_AUTHOR_EMAIL="crypto-monitor@users.noreply.github.com", GIT_COMMITTER_EMAIL="crypto-monitor@users.noreply.github.com")
        try:
            result = subprocess.run(["git", *args], cwd=self.repo, env=env, input=data,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise StateError(f"Git {args[0]} unavailable or timed out") from exc
        if result.returncode not in okay:
            # Git diagnostics can contain credential-bearing remote URLs.
            raise StateError(f"Git {args[0]} failed (exit {result.returncode}); check access/network/branch rules")
        return result

    def tip(self) -> str | None:
        result = self.git("ls-remote", "--exit-code", "--heads", self.remote, self.ref, okay=(0, 2))
        if result.returncode == 2:
            if result.stdout.strip():
                raise StateError("Unexpected missing-branch response")
            return None
        lines = result.stdout.decode().splitlines()
        if len(lines) != 1:
            raise StateError("Ambiguous remote state branch")
        sha, ref = lines[0].split()
        if not SHA.fullmatch(sha) or ref != self.ref:
            raise StateError("Invalid remote state reference")
        return sha

    def read_commit(self, sha: str) -> bytes:
        self.git("fetch", "--no-tags", "--depth=1", self.remote, sha)
        listing = self.git("ls-tree", "-r", "--name-only", sha).stdout.decode().splitlines()
        if listing != ["state.json"]:
            raise StateError("State branch must contain only state.json")
        size = int(self.git("cat-file", "-s", sha + ":state.json").stdout)
        if size > MAX_STATE_BYTES:
            raise StateError("Remote state exceeds 2 MiB bound")
        return canonical(checked_json(self.git("show", sha + ":state.json").stdout))

    def load(self, path: Path, metadata: Path, initialize: bool = False) -> None:
        sha = self.tip()  # Network failure must never look like a new repository.
        if sha is None:
            if not initialize:
                raise StateError("State branch missing: use the explicit initialize workflow once")
            raw = canonical({"version": 1})
        else:
            if initialize:
                raise StateError("State already exists; initialization cannot reset history")
            raw = self.read_commit(sha)
        atomic_write(path, raw)
        self.write_metadata(metadata, sha, raw)

    def write_metadata(self, path: Path, sha: str | None, raw: bytes) -> None:
        obj = {"revision": sha, "sha256": hashlib.sha256(raw).hexdigest(), "ref": self.ref, "remote": self.remote}
        atomic_write(path, json.dumps(obj, sort_keys=True).encode())

    def metadata(self, path: Path) -> dict:
        try:
            obj = json.loads(path.read_bytes())
        except (OSError, ValueError) as exc:
            raise StateError("Missing/invalid state metadata; load state first") from exc
        if (not isinstance(obj, dict) or obj.get("ref") != self.ref or obj.get("remote") != self.remote
                or (obj.get("revision") is not None and not SHA.fullmatch(str(obj["revision"])))
                or not re.fullmatch(r"[0-9a-f]{64}", str(obj.get("sha256", "")))):
            raise StateError("Invalid state metadata")
        return obj

    def save(self, path: Path, metadata: Path) -> None:
        raw = canonical(checked_json(path.read_bytes()))
        meta = self.metadata(metadata)
        parent = meta["revision"]
        if self.tip() != parent:
            raise StateError("State advanced since load; refusing a stale overwrite")
        if parent is not None and hashlib.sha256(raw).hexdigest() == meta["sha256"]:
            self.verify(path, metadata)
            return
        blob = self.git("hash-object", "-w", "--stdin", data=raw).stdout.decode().strip()
        tree = self.git("mktree", data=f"100644 blob {blob}\tstate.json\n".encode()).stdout.decode().strip()
        args = ["commit-tree", tree]
        if parent:
            args.extend(["-p", parent])
        commit = self.git(*args, data=b"Update bounded monitor state [skip ci]\n").stdout.decode().strip()
        for attempt in range(3):
            current = self.tip()
            if current == commit:
                break  # An earlier ambiguous push actually succeeded.
            if current != parent:
                raise StateError("Concurrent state writer detected; refusing overwrite")
            result = self.git("push", "--porcelain", self.remote, f"{commit}:{self.ref}", okay=(0, 1, 128))
            if result.returncode == 0:
                break
            if attempt < 2:
                time.sleep(1 + attempt)
        if self.tip() != commit:
            raise StateError("State push not verified; delivery must not proceed")
        if self.read_commit(commit) != raw:
            raise StateError("Persisted state content mismatch")
        atomic_write(path, raw)
        self.write_metadata(metadata, commit, raw)

    def verify(self, path: Path, metadata: Path) -> None:
        raw = canonical(checked_json(path.read_bytes()))
        meta = self.metadata(metadata)
        if meta["revision"] is None or self.tip() != meta["revision"]:
            raise StateError("Persisted state revision not verified")
        if hashlib.sha256(raw).hexdigest() != meta["sha256"] or self.read_commit(meta["revision"]) != raw:
            raise StateError("Local and persisted state differ; delivery must not proceed")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("load", "save", "verify"))
    parser.add_argument("--state", type=Path, default=Path("state/state.json"))
    parser.add_argument("--metadata", type=Path, default=Path("work/state-meta.json"))
    parser.add_argument("--repo", type=Path, default=Path("."))
    parser.add_argument("--branch", default="monitor-state")
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--initialize", action="store_true")
    args = parser.parse_args()
    try:
        if args.initialize and args.action != "load":
            raise StateError("--initialize is valid only for load")
        store = Store(args.repo, args.branch, args.remote)
        if args.action == "load":
            store.load(args.state, args.metadata, args.initialize)
        else:
            getattr(store, args.action)(args.state, args.metadata)
    except (StateError, OSError, ValueError) as exc:
        print(f"State persistence failed: {exc}", file=sys.stderr)
        return 1
    print(f"State {args.action} verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
