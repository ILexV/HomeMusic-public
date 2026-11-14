"""The PWA build id the Hub reports (/healthz web_build): pages reload themselves when it changes."""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

from app.api import system

INDEX = '<!doctype html><html><head><meta charset="UTF-8" />\n    <meta name="hm-build" content="{id}" />\n</head></html>'


def test_web_build_reads_meta(tmp_path):
    (tmp_path / "index.html").write_text(INDEX.format(id="20261019120000123"), encoding="utf-8")
    assert system.web_build(tmp_path) == "20261019120000123"


def test_web_build_follows_a_new_build(tmp_path):
    index = tmp_path / "index.html"
    index.write_text(INDEX.format(id="aaa"), encoding="utf-8")
    assert system.web_build(tmp_path) == "aaa"
    index.write_text(INDEX.format(id="bbbb"), encoding="utf-8")
    st = index.stat()
    os.utime(index, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))  # same-second rewrite on coarse clocks
    assert system.web_build(tmp_path) == "bbbb"


def test_web_build_none_without_build_or_meta(tmp_path):
    assert system.web_build(tmp_path) is None
    (tmp_path / "index.html").write_text("<html><head></head></html>", encoding="utf-8")
    assert system.web_build(tmp_path) is None


def test_healthz_reports_web_build(tmp_path, monkeypatch):
    (tmp_path / "index.html").write_text(INDEX.format(id="b1"), encoding="utf-8")

    async def nd_must_not_be_called():
        raise AssertionError("liveness must not contact upstream")

    monkeypatch.setattr(system, "_navidrome_ok", nd_must_not_be_called)
    monkeypatch.setattr(system.health_checks, "navidrome_cached", None)
    monkeypatch.setattr(system, "get_settings", lambda: SimpleNamespace(static_dir=tmp_path))
    out = asyncio.run(system.healthz())
    assert out.ok and out.navidrome is None and out.web_build == "b1"
