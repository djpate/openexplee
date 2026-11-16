import pathlib
import re
import shutil
import subprocess

import pytest

PAGE = pathlib.Path(__file__).resolve().parent.parent / "app" / "static" / "index.html"
HTML = PAGE.read_text(encoding="utf-8")


def test_footer_attribution():
    assert "Business data: Overture Maps Foundation (CDLA-Permissive-2.0 / Apache-2.0 / CC0)" in HTML


def test_refresh_and_batch_controls():
    assert "refreshSearch(" in HTML
    assert 'id="camp-batch"' in HTML
    assert "refresh-search" in HTML


def test_plan_block_uses_live_pool_counts():
    assert "pool_done" in HTML and "pool_skipped" in HTML and "pool_pending" in HTML
    assert "escapeHtml(" in HTML


def test_inline_js_parses(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    blocks = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", HTML, re.S)
    js = tmp_path / "page.js"
    js.write_text("\n;\n".join(blocks), encoding="utf-8")
    r = subprocess.run([node, "--check", str(js)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
