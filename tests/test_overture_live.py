"""Opt-in live smoke test against the real Overture bucket.

Run with: OPENEXPLEE_LIVE=1 scripts/test.sh -m live
"""
import os
import re
import time

import pytest

from app.engine import overture

pytestmark = pytest.mark.live


def test_live_tampa_dental_clinics():
    if os.environ.get("OPENEXPLEE_LIVE") != "1":
        pytest.skip("set OPENEXPLEE_LIVE=1 to hit the real Overture bucket")

    release = overture.get_release()
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}\.\d+", release)

    areas, unresolved = overture.resolve_areas(
        [overture.AreaSpec("Tampa", "locality", 1, "US-FL")], release
    )
    assert unresolved == []
    assert len(areas) == 1

    start = time.monotonic()
    rows = overture.fetch_places(release, ["dental_clinic"], areas, 0.5)
    elapsed = time.monotonic() - start
    print(f"\nlive: release={release} rows={len(rows)} elapsed={elapsed:.1f}s")
    assert len(rows) > 100
    assert elapsed < 60
