"""Synthetic incidents with realistic stack traces, each mapped to a known
root-cause symbol + release from the history built by repo_builder.build_fixture_repo().

These are hand-authored to exercise query.analyze()/replay scenarios:
    - INCIDENT_DIRECT_HIT: trace frame lands exactly on a symbol changed in R1
      (0-hop case).
    - INCIDENT_IMPORT_HOP: trace frames land in app/api.py + app/payments.py,
      but the true root cause (app.utils.format_currency) is one import-hop
      away from the deepest trace frame (process_payment imports/calls it).
    - INCIDENT_JS_DIRECT_HIT: JS stack trace hitting a symbol that is NOT
      changed in any release (used as a negative/"unrelated" control, or
      paired with a modified JS symbol depending on the test).
    - INCIDENT_CHRONOLOGICAL: occurred after R1 shipped but before R2 shipped;
      used by test_replay.py to assert only R1 changes surface, never R2.

Each incident records `occurred_after_tag` / `occurred_before_tag` (or None)
so tests can build the correct release_window for chronological replay.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Incident:
    incident_id: str
    description: str
    stack_trace: str | None
    # Ground truth: the qualified symbol that is the true root cause.
    root_cause_qualified_name: str
    root_cause_file_path: str
    root_cause_release_tag: str  # which release introduced the offending change
    # Chronological bounds for replay tests: the incident is known to have
    # happened strictly after `occurred_after_tag` shipped, and (if set)
    # strictly before `occurred_before_tag` shipped.
    occurred_after_tag: str | None = None
    occurred_before_tag: str | None = None
    # Free-form notes on why this incident is shaped the way it is.
    notes: str = ""
    language: str = "python"
    tags: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Incident 1: direct hit. The stack trace's innermost frame is exactly the
# function modified in R1 (app.payments.process_payment raising ValueError
# for a non-positive amount, a behavior introduced in R1).
# ---------------------------------------------------------------------------

INCIDENT_DIRECT_HIT = Incident(
    incident_id="INC-1001",
    description=(
        "Checkout requests with a $0 promotional order started failing with "
        "'invalid amount' right after the R1 release."
    ),
    stack_trace='''Traceback (most recent call last):
  File "app/api.py", line 9, in handle_checkout
    return process_payment(order)
  File "app/payments.py", line 32, in process_payment
    raise ValueError("invalid amount")
ValueError: invalid amount
''',
    root_cause_qualified_name="app.payments.process_payment",
    root_cause_file_path="app/payments.py",
    root_cause_release_tag="v1.1.0",
    occurred_after_tag="v1.1.0",
    occurred_before_tag=None,
    notes="0-hop case: trace frame names process_payment directly, which is a symbol modified in R1.",
    language="python",
    tags=["direct_hit", "0hop"],
)


# ---------------------------------------------------------------------------
# Incident 2: import-hop. The visible trace frames are handle_checkout and
# process_payment (unchanged call sites in R1 other than process_payment
# itself), but the *actual* defect is the R1 change to format_currency
# (thousands-separator formatting throwing on a float edge case one hop away
# via the `from app.utils import format_currency` import in payments.py).
# We deliberately keep the deepest frame at process_payment's call site
# rather than inside format_currency, so the true cause is one import-hop
# from the trace, not directly named in it.
# ---------------------------------------------------------------------------

INCIDENT_IMPORT_HOP = Incident(
    incident_id="INC-1002",
    description=(
        "Receipt display amounts started rendering as 'N/A' for large orders "
        "shortly after R1; exception surfaces inside process_payment's call "
        "into the currency formatter."
    ),
    stack_trace='''Traceback (most recent call last):
  File "app/api.py", line 9, in handle_checkout
    return process_payment(order)
  File "app/payments.py", line 34, in process_payment
    receipt["display_amount"] = format_currency(order["amount"])
ValueError: Unknown format code 'f' for object of type 'str'
''',
    root_cause_qualified_name="app.utils.format_currency",
    root_cause_file_path="app/utils.py",
    root_cause_release_tag="v1.1.0",
    occurred_after_tag="v1.1.0",
    occurred_before_tag=None,
    notes=(
        "Import-hop case: deepest trace frame is process_payment (payments.py), "
        "but the true root cause is format_currency in app/utils.py, reached via "
        "the `from app.utils import format_currency` import — one hop from the "
        "trace's own frames, not named directly in the traceback."
    ),
    language="python",
    tags=["import_hop", "1hop"],
)


# ---------------------------------------------------------------------------
# Incident 3: JS trace. Root cause is submitOrder's use of formatPrice, which
# is NOT modified in any release in this fixture history -- used as an
# unrelated/negative control so ranking tests can assert it scores below
# genuinely-changed symbols. (No Python release ever touches web/*.)
# ---------------------------------------------------------------------------

INCIDENT_JS_UNRELATED = Incident(
    incident_id="INC-1003",
    description=(
        "Order submission on the web client intermittently shows a blank "
        "price; suspected client-side formatting bug unrelated to backend releases."
    ),
    stack_trace="""Error: Cannot read properties of undefined (reading 'toFixed')
    at formatPrice (web/helpers.js:4:24)
    at submitOrder (web/client.js:7:20)
""",
    root_cause_qualified_name="web.helpers.formatPrice",
    root_cause_file_path="web/helpers.js",
    root_cause_release_tag="v1.0.0",  # never actually changed post-baseline
    occurred_after_tag="v1.1.0",
    occurred_before_tag=None,
    notes=(
        "Negative control: formatPrice/submitOrder are never modified by R1 or "
        "R2, so a correct ranking should surface this incident's frontier below "
        "any incident whose frames touch truly-changed symbols (or find nothing "
        "in-window at all)."
    ),
    language="javascript",
    tags=["js", "unrelated"],
)


# ---------------------------------------------------------------------------
# Incident 4: chronological replay case. Occurred after R1 shipped but
# strictly before R2 shipped, so only R1's ChangedSymbols should be visible
# when the incident is replayed with a release_window bounded at R1 -- R2's
# charge_card change must never leak into the result even though it exists
# in the fixture's full history.
# ---------------------------------------------------------------------------

INCIDENT_CHRONOLOGICAL = Incident(
    incident_id="INC-1004",
    description=(
        "Card validation started rejecting some previously-valid short test "
        "card numbers immediately after R1 shipped, well before R2 existed."
    ),
    stack_trace='''Traceback (most recent call last):
  File "app/api.py", line 9, in handle_checkout
    return process_payment(order)
  File "app/payments.py", line 30, in process_payment
    raise ValueError("invalid card")
ValueError: invalid card
''',
    root_cause_qualified_name="app.payments.validate_card",
    root_cause_file_path="app/payments.py",
    root_cause_release_tag="v1.1.0",
    occurred_after_tag="v1.1.0",
    occurred_before_tag="v1.2.0",
    notes=(
        "Chronological replay case: must be replayed with a release_window that "
        "includes only v1.1.0 (R1). R2's charge_card change (v1.2.0) postdates "
        "this incident and must not surface even though it exists in full history."
    ),
    language="python",
    tags=["replay", "chronological"],
)


ALL_INCIDENTS = [
    INCIDENT_DIRECT_HIT,
    INCIDENT_IMPORT_HOP,
    INCIDENT_JS_UNRELATED,
    INCIDENT_CHRONOLOGICAL,
]
