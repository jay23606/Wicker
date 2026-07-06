"""Tests for env interpolation and the web<->sidecar env bridge.

Sidecar rendering now lives in Canasta (the `canasta sidecar` primitive);
Wicker only resolves ${VAR} interpolation and generates the global settings
fragment that re-exports instance env to the web tier's getenv().
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import wicker  # noqa: E402


class TestEnvInterpolation:
    def test_resolve_variants(self, monkeypatch):
        monkeypatch.setenv("SET", "val")
        monkeypatch.delenv("UNSET", raising=False)
        monkeypatch.setenv("EMPTY", "")
        r = wicker.resolve_env_value
        assert r("${SET}") == "val"
        assert r("${UNSET}") == ""
        assert r("${UNSET:-d}") == "d"
        assert r("${EMPTY:-d}") == "d"     # :- falls back on empty too
        assert r("${EMPTY-d}") == ""       # - only falls back when unset
        assert r("${UNSET-d}") == "d"
        assert r("pre-${SET}-post") == "pre-val-post"
        assert r("plain") == "plain"


class TestWebEnvBridge:
    def test_bridge_reexports_each_key_for_getenv(self):
        php = wicker.render_env_bridge_php({"SHARED_SECRET": "s3cr3t", "FOO": "bar"})
        assert php.startswith("<?php")
        # putenv makes getenv() work; $_ENV/$_SERVER cover code that reads those.
        assert "putenv('SHARED_SECRET=s3cr3t');" in php
        assert "$_ENV['SHARED_SECRET'] = 's3cr3t';" in php
        assert "$_SERVER['SHARED_SECRET'] = 's3cr3t';" in php
        assert "putenv('FOO=bar');" in php

    def test_bridge_escapes_quotes_and_backslashes(self):
        php = wicker.render_env_bridge_php({"K": "a'b\\c"})
        # single quote and backslash escaped for a PHP single-quoted string
        assert r"putenv('K=a\'b\\c');" in php

    def test_bridge_empty_is_just_header(self):
        php = wicker.render_env_bridge_php({})
        assert "putenv(" not in php
