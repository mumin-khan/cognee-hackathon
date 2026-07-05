"""Builds a small synthetic git repository with a scripted release history.

Used by tests to exercise the RCA pipeline (extract -> diff_sync -> query -> replay)
against known ground truth: which qualified symbols changed in which release, in
which files, and how (added/modified/deleted).

Everything here is deterministic: fixed author/committer identity and fixed
commit dates (via GIT_AUTHOR_* / GIT_COMMITTER_* env vars) so commit content is
stable across runs. Commit SHAs themselves are still derived from tree content
and parent SHAs by git, so callers must read shas off the returned ground-truth
object rather than hardcoding them.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

ChangeType = Literal["added", "modified", "deleted"]

# Fixed identity/dates so commits are reproducible byte-for-byte given the same tree.
_GIT_ENV_BASE = {
    "GIT_AUTHOR_NAME": "RCA Fixture Bot",
    "GIT_AUTHOR_EMAIL": "rca-fixture-bot@example.com",
    "GIT_COMMITTER_NAME": "RCA Fixture Bot",
    "GIT_COMMITTER_EMAIL": "rca-fixture-bot@example.com",
    # Disable any global/system git config bleeding into the fixture repo.
    "GIT_CONFIG_NOSYSTEM": "1",
    "HOME": "/nonexistent-rca-fixture-home",
}

_COMMIT_DATES = {
    "initial": "2024-01-01T09:00:00",
    "r1": "2024-02-01T09:00:00",
    "r2": "2024-03-01T09:00:00",
}


@dataclass
class ChangedSymbolGT:
    """Ground truth record for one symbol changed in one release."""

    qualified_name: str  # e.g. "app.payments.process_payment"
    file_path: str  # repo-relative path, e.g. "app/payments.py"
    change_type: ChangeType


@dataclass
class ReleaseGT:
    """Ground truth record for one scripted release."""

    tag: str
    commit_sha: str
    prev_sha: str
    timestamp: str  # ISO8601, matches the commit date used to create it
    changed_symbols: list[ChangedSymbolGT] = field(default_factory=list)


@dataclass
class FixtureRepo:
    """Full ground truth for the synthetic repo built by build_fixture_repo()."""

    repo_path: Path
    initial_sha: str
    initial_tag: str
    releases: list[ReleaseGT]  # in chronological order: [R1, R2]

    @property
    def r1(self) -> ReleaseGT:
        return self.releases[0]

    @property
    def r2(self) -> ReleaseGT:
        return self.releases[1]

    def release_by_tag(self, tag: str) -> ReleaseGT:
        for r in self.releases:
            if r.tag == tag:
                return r
        raise KeyError(f"no release with tag {tag!r}")

    def all_shas(self) -> list[str]:
        return [self.initial_sha] + [r.commit_sha for r in self.releases]


def _run_git(repo_path: Path, *args: str, env: dict[str, str]) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo_path,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _write(repo_path: Path, rel_path: str, content: str) -> None:
    full = repo_path / rel_path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content)


def _commit_env(date_iso: str) -> dict[str, str]:
    import os

    env = dict(os.environ)
    env.update(_GIT_ENV_BASE)
    env["GIT_AUTHOR_DATE"] = date_iso
    env["GIT_COMMITTER_DATE"] = date_iso
    return env


# ---------------------------------------------------------------------------
# Source file contents, per revision.
# ---------------------------------------------------------------------------

_DB_PY_V1 = '''"""Database access helpers (fixture app)."""


def get_connection(dsn):
    """Return a fake DB connection handle for the given DSN."""
    return {"dsn": dsn, "open": True}


def save_record(table, data):
    """Persist a record into the given table (fixture no-op)."""
    conn = get_connection("sqlite:///fixture.db")
    return {"table": table, "data": data, "conn": conn}
'''

_UTILS_PY_V1 = '''"""Small formatting/retry helpers (fixture app)."""


def format_currency(cents):
    """Format integer cents as a dollar string."""
    return "${:.2f}".format(cents / 100)


def retry(fn, times):
    """Call fn up to `times` times, returning the first success."""
    last_err = None
    for _ in range(times):
        try:
            return fn()
        except Exception as err:  # noqa: BLE001 - fixture code
            last_err = err
    raise last_err
'''

_PAYMENTS_PY_V1 = '''"""Core payment processing flow (fixture app)."""

from app.db import save_record
from app.utils import format_currency


def validate_card(card):
    """Validate a card dict has the required fields."""
    return bool(card.get("number")) and bool(card.get("cvv"))


def charge_card(card, amount):
    """Charge a validated card for `amount` cents."""
    return {"card": card["number"][-4:], "amount": amount, "status": "charged"}


def process_payment(order):
    """Validate and charge the card attached to `order`, then persist it."""
    card = order["card"]
    if not validate_card(card):
        raise ValueError("invalid card")
    receipt = charge_card(card, order["amount"])
    receipt["display_amount"] = format_currency(order["amount"])
    save_record("payments", receipt)
    return receipt
'''

_API_PY_V1 = '''"""HTTP-ish entry points (fixture app)."""

from app.payments import process_payment


def handle_checkout(request):
    """Handle a checkout request and return a payment receipt."""
    order = request["order"]
    return process_payment(order)
'''

_HELPERS_JS_V1 = """// Formatting/logging helpers (fixture web app).

function formatPrice(cents) {
  return "$" + (cents / 100).toFixed(2);
}

function logEvent(name) {
  return { event: name, loggedAt: "fixture-time" };
}

module.exports = { formatPrice, logEvent };
"""

_CLIENT_JS_V1 = """// Order submission client (fixture web app).

const { formatPrice, logEvent } = require("./helpers.js");

function submitOrder(order) {
  logEvent("submit_order");
  const display = formatPrice(order.amount);
  return { order: order, display: display, status: "submitted" };
}

module.exports = { submitOrder };
"""


# ---------------------------------------------------------------------------
# R1 revisions: modify process_payment, validate_card, format_currency;
#               add refund_payment; delete retry.
# ---------------------------------------------------------------------------

_UTILS_PY_R1 = '''"""Small formatting/retry helpers (fixture app)."""


def format_currency(cents):
    """Format integer cents as a dollar string, now with thousands separators."""
    return "${:,.2f}".format(cents / 100)
'''

_PAYMENTS_PY_R1 = '''"""Core payment processing flow (fixture app)."""

from app.db import save_record
from app.utils import format_currency


def validate_card(card):
    """Validate a card dict has the required fields and a plausible length."""
    number = card.get("number", "")
    return bool(number) and bool(card.get("cvv")) and len(number) >= 12


def charge_card(card, amount):
    """Charge a validated card for `amount` cents."""
    return {"card": card["number"][-4:], "amount": amount, "status": "charged"}


def refund_payment(receipt):
    """Refund a previously charged payment receipt."""
    return {"card": receipt["card"], "amount": -receipt["amount"], "status": "refunded"}


def process_payment(order):
    """Validate and charge the card attached to `order`, then persist it.

    R1: now raises on non-positive amounts before attempting to charge.
    """
    card = order["card"]
    if not validate_card(card):
        raise ValueError("invalid card")
    if order["amount"] <= 0:
        raise ValueError("invalid amount")
    receipt = charge_card(card, order["amount"])
    receipt["display_amount"] = format_currency(order["amount"])
    save_record("payments", receipt)
    return receipt
'''


# ---------------------------------------------------------------------------
# R2 revision: modify charge_card only.
# ---------------------------------------------------------------------------

_PAYMENTS_PY_R2 = '''"""Core payment processing flow (fixture app)."""

from app.db import save_record
from app.utils import format_currency


def validate_card(card):
    """Validate a card dict has the required fields and a plausible length."""
    number = card.get("number", "")
    return bool(number) and bool(card.get("cvv")) and len(number) >= 12


def charge_card(card, amount):
    """Charge a validated card for `amount` cents.

    R2: applies a flat processing fee before recording the charge.
    """
    fee = 30
    return {"card": card["number"][-4:], "amount": amount + fee, "status": "charged"}


def refund_payment(receipt):
    """Refund a previously charged payment receipt."""
    return {"card": receipt["card"], "amount": -receipt["amount"], "status": "refunded"}


def process_payment(order):
    """Validate and charge the card attached to `order`, then persist it.

    R1: now raises on non-positive amounts before attempting to charge.
    """
    card = order["card"]
    if not validate_card(card):
        raise ValueError("invalid card")
    if order["amount"] <= 0:
        raise ValueError("invalid amount")
    receipt = charge_card(card, order["amount"])
    receipt["display_amount"] = format_currency(order["amount"])
    save_record("payments", receipt)
    return receipt
'''


def build_fixture_repo(tmp_path: Path) -> FixtureRepo:
    """Build the synthetic repo under `tmp_path` and return its ground truth.

    Layout:
        app/db.py         - get_connection, save_record
        app/utils.py      - format_currency, retry
        app/payments.py   - validate_card, charge_card, process_payment
        app/api.py        - handle_checkout
        web/helpers.js    - formatPrice, logEvent
        web/client.js     - submitOrder (imports web/helpers.js)

    History:
        initial commit, tagged v1.0.0
        R1 (tag v1.1.0): modifies process_payment, validate_card, format_currency;
                         adds refund_payment; deletes retry.
        R2 (tag v1.2.0): modifies charge_card.
    """
    repo_path = tmp_path / "fixture_repo"
    repo_path.mkdir(parents=True, exist_ok=True)

    env = _commit_env(_COMMIT_DATES["initial"])
    _run_git(repo_path, "init", "-q", "-b", "main", env=env)
    _run_git(repo_path, "config", "user.name", _GIT_ENV_BASE["GIT_AUTHOR_NAME"], env=env)
    _run_git(repo_path, "config", "user.email", _GIT_ENV_BASE["GIT_AUTHOR_EMAIL"], env=env)
    _run_git(repo_path, "config", "commit.gpgsign", "false", env=env)
    _run_git(repo_path, "config", "tag.gpgsign", "false", env=env)

    # ---- initial commit -------------------------------------------------
    _write(repo_path, "app/__init__.py", "")
    _write(repo_path, "app/db.py", _DB_PY_V1)
    _write(repo_path, "app/utils.py", _UTILS_PY_V1)
    _write(repo_path, "app/payments.py", _PAYMENTS_PY_V1)
    _write(repo_path, "app/api.py", _API_PY_V1)
    _write(repo_path, "web/helpers.js", _HELPERS_JS_V1)
    _write(repo_path, "web/client.js", _CLIENT_JS_V1)

    _run_git(repo_path, "add", "-A", env=env)
    _run_git(repo_path, "commit", "-q", "-m", "initial: fixture payment app", env=env)
    initial_sha = _run_git(repo_path, "rev-parse", "HEAD", env=env)
    _run_git(repo_path, "tag", "-a", "v1.0.0", "-m", "v1.0.0", env=env)

    # ---- R1 ---------------------------------------------------------------
    env_r1 = _commit_env(_COMMIT_DATES["r1"])
    _write(repo_path, "app/utils.py", _UTILS_PY_R1)  # modified format_currency, deleted retry
    _write(repo_path, "app/payments.py", _PAYMENTS_PY_R1)  # modified 2, added 1

    _run_git(repo_path, "add", "-A", env=env_r1)
    _run_git(
        repo_path,
        "commit",
        "-q",
        "-m",
        "R1: tighten card validation, add refunds, drop unused retry helper",
        env=env_r1,
    )
    r1_sha = _run_git(repo_path, "rev-parse", "HEAD", env=env_r1)
    _run_git(repo_path, "tag", "-a", "v1.1.0", "-m", "v1.1.0", env=env_r1)

    r1_gt = ReleaseGT(
        tag="v1.1.0",
        commit_sha=r1_sha,
        prev_sha=initial_sha,
        timestamp=_COMMIT_DATES["r1"],
        changed_symbols=[
            ChangedSymbolGT("app.payments.process_payment", "app/payments.py", "modified"),
            ChangedSymbolGT("app.payments.validate_card", "app/payments.py", "modified"),
            ChangedSymbolGT("app.utils.format_currency", "app/utils.py", "modified"),
            ChangedSymbolGT("app.payments.refund_payment", "app/payments.py", "added"),
            ChangedSymbolGT("app.utils.retry", "app/utils.py", "deleted"),
        ],
    )

    # ---- R2 ---------------------------------------------------------------
    env_r2 = _commit_env(_COMMIT_DATES["r2"])
    _write(repo_path, "app/payments.py", _PAYMENTS_PY_R2)  # modified charge_card only

    _run_git(repo_path, "add", "-A", env=env_r2)
    _run_git(repo_path, "commit", "-q", "-m", "R2: apply flat processing fee on charge", env=env_r2)
    r2_sha = _run_git(repo_path, "rev-parse", "HEAD", env=env_r2)
    _run_git(repo_path, "tag", "-a", "v1.2.0", "-m", "v1.2.0", env=env_r2)

    r2_gt = ReleaseGT(
        tag="v1.2.0",
        commit_sha=r2_sha,
        prev_sha=r1_sha,
        timestamp=_COMMIT_DATES["r2"],
        changed_symbols=[
            ChangedSymbolGT("app.payments.charge_card", "app/payments.py", "modified"),
        ],
    )

    return FixtureRepo(
        repo_path=repo_path,
        initial_sha=initial_sha,
        initial_tag="v1.0.0",
        releases=[r1_gt, r2_gt],
    )
