"""Build the *built-in* git merge history used by the demo page.

The repository is created deterministically on first launch.  Its commits
contain a tiny shop-pricing project plus a stable ``qa/run_check.py`` that
reads / imports the files of *that exact commit* and exits:

    0   -> test passed
    1   -> test failed
    77  -> skipped (qa/SKIP marker, sandbox unavailable)

Topology (all side branches start at the same known-good baseline):

    r0 ─ r1 ─ r2 [baseline-good]
          │
          ├─ A1 ─ A2(bug) ─ A3 ─┐
          ├─ B1 ─ B2(bug) ─ B3 ─┼─ M1(merge A) ─ M2(merge B) ─ M3(merge D) ─ h1 [main: known bad]
          └─ D1 ─ D2(skip) ─ D3(bug) ───────────────────────────┘

    conflict line (separate ref, not ancestor of main):
          └─ Q1(fail) ─ Q2(pass/fix) ─ Q3(fail/regression) [conflict-bad]

A main run therefore ends with four possible earliest failures
``{A2, B2, D2(skipped), D3}``; the conflict preset has exactly two
candidate commits between the seeds, so both start immediately and the
Q1(fail)-ancestor / Q2(pass)-descendant contradiction is guaranteed to be
measured.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Dict, List, Optional

FIXTURE_VERSION = "v3"
VERSION_TAG = "fixture-version-" + FIXTURE_VERSION


PRICING_PY = '''\
"""Member pricing - core contract stays correct across all revisions."""

RATES = {"normal": 0.0, "silver": 0.05, "gold": 0.10}


def member_price(price, level):
    """Final price after the membership discount."""
    return round(float(price) * (1.0 - RATES[level]), 2)
'''

INIT_README = """# Shop Pricing Service

A tiny repository with a deliberately non-linear merge history used by the
Git regression localization page.

* `main` contains three parallel feature lines merged together.
* `conflict-line` contains a fail -> pass -> fail pocket.
* Tag `baseline-good` is the known-passing ancestor for both presets.
"""

# Stable across ALL revisions: every check is conditional on the file being
# present, so the same script works on the root commit and every branch.
RUN_CHECK_PY = r'''\
#!/usr/bin/env python3
"""QA contract checker for the built-in fixture.

It deliberately reads files from the commit that was extracted around it,
so running it for different shas tests different contents.
Exit codes: 0 pass, 1 fail, 77 skip.
"""
import os
import sys
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

SKIP_MARKER = os.path.join(ROOT, "qa", "SKIP")
if os.path.exists(SKIP_MARKER):
    with open(SKIP_MARKER, "r", encoding="utf-8") as fh:
        reason = fh.read().strip()
    print("SKIP: " + (reason or "qa/SKIP marker present"))
    sys.exit(77)

failures = []
read_files = []


def record(rel):
    read_files.append(rel)
    print("READ " + rel)


def check(name, rel, fn):
    path = os.path.join(ROOT, rel)
    if not os.path.exists(path):
        return
    record(rel)
    try:
        fn()
        print("OK   " + name)
    except Exception as exc:  # noqa: BLE001
        failures.append(name + ": " + str(exc))
        print("FAIL " + name + ": " + str(exc))
        traceback.print_exc()


def check_pricing():
    from shop import pricing
    record("shop/pricing.py")
    cases = [
        (100.0, "normal", 100.0),
        (50.0, "silver", 47.5),
        (200.0, "gold", 180.0),
    ]
    for price, level, want in cases:
        got = pricing.member_price(price, level)
        print("pricing.member_price(%r, %r) = %r" % (price, level, got))
        assert abs(got - want) < 0.01, (price, level, got, want)


def check_coupon():
    from shop import coupon
    record("shop/coupon.py")
    cases = [
        (200.0, "WELCOME10", 180.0),
        (100.0, "VIP20", 80.0),
        (64.0, "UNKNOWN", 64.0),
    ]
    for price, code, want in cases:
        got = coupon.apply_coupon(price, code)
        print("coupon.apply_coupon(%r, %r) = %r" % (price, code, got))
        assert abs(got - want) < 0.01, (price, code, got, want)


def check_shipping():
    from shop import shipping
    record("shop/shipping.py")
    cases = [
        (2.0, "domestic", 8.0),
        (6.0, "domestic", 10.0),
        (1.0, "eu", 14.0),
        (3.0, "world", 22.0),
    ]
    for weight, region, want in cases:
        got = shipping.shipping_fee(weight, region)
        print("shipping.shipping_fee(%r, %r) = %r" % (weight, region, got))
        assert abs(got - want) < 0.01, (weight, region, got, want)


def check_loyalty():
    from shop import loyalty
    record("shop/loyalty.py")
    assert loyalty.loyalty_points(259) == 25, loyalty.loyalty_points(259)
    assert loyalty.loyalty_points(1000) == 100
    assert loyalty.tier(1200) == "gold"
    assert loyalty.tier(300) == "silver"
    assert loyalty.tier(50) == "bronze"
    print("loyalty contract OK")


def check_legacy():
    from shop import legacy
    record("shop/legacy.py")
    cases = [
        (200.0, 5, 190.0),
        (100.0, 20, 85.0),
        (80.0, 0, 80.0),
    ]
    for price, years, want in cases:
        got = legacy.legacy_discount(price, years)
        print("legacy.legacy_discount(%r, %r) = %r" % (price, years, got))
        assert abs(got - want) < 0.01, (price, years, got, want)


check("pricing", os.path.join("shop", "pricing.py"), check_pricing)
check("coupon", os.path.join("shop", "coupon.py"), check_coupon)
check("shipping", os.path.join("shop", "shipping.py"), check_shipping)
check("loyalty", os.path.join("shop", "loyalty.py"), check_loyalty)
check("legacy", os.path.join("shop", "legacy.py"), check_legacy)

print("READ qa/run_check.py")
if failures:
    print("RESULT FAIL: %d contract failure(s)" % len(failures))
    sys.exit(1)
print("RESULT PASS")
sys.exit(0)
'''

SHOP_INIT = '"""Shop package."""\n'

COUPON_GOOD = '''\
"""Coupon calculation - cyberpay line."""


def apply_coupon(price, code):
    if code == "WELCOME10":
        return round(float(price) * 0.90, 2)
    if code == "VIP20":
        return round(float(price) * 0.80, 2)
    return price
'''

COUPON_BAD = '''\
"""Coupon calculation - cyberpay line."""


def apply_coupon(price, code):
    # Refactor: promote the welcome offer to 20% off.
    if code == "WELCOME10":
        return round(float(price) * 0.80, 2)
    if code == "VIP20":
        return round(float(price) * 0.80, 2)
    return price
'''

SHIPPING_GOOD = '''\
"""Shipping fees - freight line."""

_BASE = {"domestic": 8.0, "eu": 14.0, "world": 22.0}


def shipping_fee(weight_kg, region):
    fee = _BASE[region]
    if weight_kg > 5:
        fee += (weight_kg - 5) * 2.0
    return round(fee, 2)
'''

SHIPPING_BAD = '''\
"""Shipping fees - freight line."""

# New carrier contract: domestic base moved to 10.
_BASE = {"domestic": 10.0, "eu": 14.0, "world": 22.0}


def shipping_fee(weight_kg, region):
    fee = _BASE[region]
    if weight_kg > 5:
        fee += (weight_kg - 5) * 2.0
    return round(fee, 2)
'''

LOYALTY_GOOD = '''\
"""Loyalty points - members line."""


def loyalty_points(amount):
    return int(float(amount)) // 10


def tier(points):
    if points >= 1000:
        return "gold"
    if points >= 200:
        return "silver"
    return "bronze"
'''

LOYALTY_BAD = '''\
"""Loyalty points - members line."""


def loyalty_points(amount):
    # Cheaper points accrual after the finance review.
    return int(float(amount)) // 20


def tier(points):
    if points >= 1000:
        return "gold"
    if points >= 200:
        return "silver"
    return "bronze"
'''

LEGACY_GOOD = '''\
"""Legacy discount kept for old enterprise contracts."""


def legacy_discount(price, years):
    rate = min(int(years), 15) * 0.01
    return round(float(price) * (1.0 - rate), 2)
'''

LEGACY_BAD = '''\
"""Legacy discount kept for old enterprise contracts."""


def legacy_discount(price, years):
    # Double the yearly loyalty rate.
    rate = min(int(years), 15) * 0.02
    return round(float(price) * (1.0 - rate), 2)
'''


class RepoBuilder:
    def __init__(self, path: os.PathLike | str):
        self.path = Path(path)
        self._n = 0

    def git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        env.update(
            {
                "GIT_AUTHOR_NAME": "Release Bot",
                "GIT_AUTHOR_EMAIL": "release-bot@example.test",
                "GIT_COMMITTER_NAME": "Release Bot",
                "GIT_COMMITTER_EMAIL": "release-bot@example.test",
                "GIT_AUTHOR_DATE": f"2026-01-01T00:{self._n:02d}:00+0000",
                "GIT_COMMITTER_DATE": f"2026-01-01T00:{self._n:02d}:00+0000",
                "GIT_CONFIG_NOSYSTEM": "1",
            }
        )
        proc = subprocess.run(
            ["git", *args], cwd=self.path, env=env,
            text=True, capture_output=True,
        )
        if check and proc.returncode != 0:
            raise RuntimeError(
                f"git {' '.join(args)} failed ({proc.returncode}):\n"
                f"STDOUT:{proc.stdout}\nSTDERR:{proc.stderr}"
            )
        return proc

    def write(self, rel: str, content: str) -> "RepoBuilder":
        p = self.path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return self

    def rm(self, rel: str) -> "RepoBuilder":
        p = self.path / rel
        if p.exists():
            p.unlink()
        return self

    def commit(self, message: str, *tags: str) -> str:
        self._n += 1
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message, "--allow-empty")
        for tag in tags:
            self.git("tag", "-f", tag)
        return self.sha()

    def sha(self, ref: str = "HEAD") -> str:
        return self.git("rev-parse", ref).stdout.strip()

    def branch(self, name: str) -> "RepoBuilder":
        self.git("checkout", "-q", "-b", name)
        return self

    def checkout(self, ref: str) -> "RepoBuilder":
        self.git("checkout", "-q", ref)
        return self


def is_builtin_repo(path: os.PathLike | str) -> bool:
    p = Path(path)
    if not (p / ".git").exists():
        return False
    proc = subprocess.run(
        ["git", "-C", str(p), "rev-parse", "-q", "--verify",
         f"refs/tags/{VERSION_TAG}"],
        text=True, capture_output=True,
    )
    return proc.returncode == 0


def build_builtin_repo(path: os.PathLike | str, *, force: bool = False) -> str:
    """Create (or reuse) the deterministic fixture repository."""
    target = Path(path)
    if is_builtin_repo(target) and not force:
        return str(target)
    if target.exists():
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.mkdir(parents=True, exist_ok=True)

    b = RepoBuilder(target)
    b.git("init", "-q", "-b", "main")
    b.git("config", "commit.gpgsign", "false")

    # ---- root + baseline -------------------------------------------------
    b.write("README.md", INIT_README)
    b.write("shop/__init__.py", SHOP_INIT)
    b.write("shop/pricing.py", PRICING_PY)
    b.write("qa/run_check.py", RUN_CHECK_PY)
    b.commit("chore: initial shop pricing service with QA contract")

    b.write("docs/release-notes.md", "# Release notes\n\n- 0.9 baseline\n")
    b.commit("docs: add release notes")

    b.write("CHANGELOG.md", "# Changelog\n\n## 0.9.0\n- stable pricing\n")
    r2 = b.commit("chore: mark known-good regression baseline", "baseline-good")

    # ---- parallel branch A: coupon bug ----------------------------------
    b.branch("cyberpay")
    b.write("shop/coupon.py", COUPON_GOOD)
    b.commit("feat(coupon): add WELCOME10 and VIP20 coupons")
    b.write("shop/coupon.py", COUPON_BAD)
    a2 = b.commit("refactor(coupon): align WELCOME10 with spring campaign")
    b.write("docs/coupon.md", "# Coupons\n\nWELCOME10 / VIP20\n")
    a3 = b.commit("docs(coupon): document coupon codes")

    # ---- parallel branch B: shipping bug --------------------------------
    b.checkout(r2)
    b.branch("freight")
    b.write("shop/shipping.py", SHIPPING_GOOD)
    b.commit("feat(shipping): regional shipping fee table")
    b.write("shop/shipping.py", SHIPPING_BAD)
    b2 = b.commit("chore(shipping): renegotiate domestic carrier contract")
    b.write("shop/shipping.py",
            SHIPPING_BAD + "\n# rounding is handled by round() below\n")
    b3 = b.commit("chore(shipping): clarify fee rounding comment")

    # ---- parallel branch D: skip then bug -------------------------------
    b.checkout(r2)
    b.branch("loyalty")
    b.write("shop/loyalty.py", LOYALTY_GOOD)
    b.commit("feat(loyalty): points and membership tiers")
    b.write("qa/SKIP", "loyalty IT sandbox unreachable in CI image")
    d2 = b.commit("ci(qa): skip contract run while loyalty sandbox is down")
    b.rm("qa/SKIP")
    b.write("shop/loyalty.py", LOYALTY_BAD)
    d3 = b.commit("feat(loyalty): cheaper points accrual after finance review")

    # ---- conflict line: fail -> pass -> fail -----------------------------
    b.checkout(r2)
    b.branch("conflict-line")
    b.write("shop/legacy.py", LEGACY_BAD)
    q1 = b.commit("feat(legacy): enterprise loyalty discount")
    b.write("shop/legacy.py", LEGACY_GOOD)
    q2 = b.commit("fix(legacy): use documented 1% per contract year")
    b.write("shop/legacy.py", LEGACY_BAD)
    q3 = b.commit("refactor(legacy): double yearly rate for renewals",
                  "conflict-bad")

    # ---- merge the three feature lines into main --------------------------
    b.checkout("main")
    b.git("merge", "--no-ff", a3, "-m",
          "merge: cyberpay coupon line into main")
    b.git("merge", "--no-ff", b.sha("freight"), "-m",
          "merge: freight shipping line into main")
    b.git("merge", "--no-ff", d3, "-m",
          "merge: loyalty members line into main")
    b.write("CHANGELOG.md",
            "# Changelog\n\n## 1.0.0\n- coupons, shipping, loyalty\n")
    h1 = b.commit("release: 1.0.0 merge train")
    b.git("tag", "-f", "main-bad", h1)
    b.git("tag", "-f", VERSION_TAG)

    # Return to main for convenience.
    b.checkout("main")

    expected = {
        "r2": r2, "a2": a2, "b2": b2, "d2": d2, "d3": d3,
        "q1": q1, "q2": q2, "q3": q3, "head": h1,
    }
    (target / "BUILTIN_SHAS").write_text(
        "\n".join(f"{k} {v}" for k, v in expected.items()),
        encoding="utf-8",
    )
    return str(target)


def ensure_builtin_repo(path: Optional[str] = None) -> str:
    path = path or os.environ.get(
        "HISTORY_REPO", "/tmp/gsb-bisect/history-repo")
    return build_builtin_repo(path)
