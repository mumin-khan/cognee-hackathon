"""Replay gate test (SPEC.md "Verification" step 4 / plan Step 3):

Take 2 sequential synthetic releases (R1, R2). Replay an incident that
occurred after R1 shipped but before R2 shipped, as if querying live at that
point in history: only R1's ChangedSymbols may surface. R2's change
(app.payments.charge_card, v1.2.0) must never leak into the result, even
though it exists in the full fixture history once both releases are synced.

Uses INCIDENT_CHRONOLOGICAL (occurred_after_tag="v1.1.0",
occurred_before_tag="v1.2.0"), whose root cause is app.payments.validate_card,
modified in R1.
"""

from __future__ import annotations

import pytest

from rca.baseline import build_baseline
from rca.diff_sync import sync_release
from rca.query import analyze

from tests.fixtures.incidents import INCIDENT_CHRONOLOGICAL

pytestmark = pytest.mark.graph


class TestChronologicalReplay:
    async def test_incident_between_r1_and_r2_only_surfaces_r1(self, fixture_repo, clean_cognee):
        repo_path = str(fixture_repo.repo_path)
        r1 = fixture_repo.r1
        r2 = fixture_repo.r2

        assert INCIDENT_CHRONOLOGICAL.occurred_after_tag == r1.tag
        assert INCIDENT_CHRONOLOGICAL.occurred_before_tag == r2.tag

        # Build the baseline as of R1 (post-release content), matching what
        # the graph would have looked like at the time the incident occurred
        # -- R2 has not shipped yet, so it must not be synced at all.
        await build_baseline(repo_path)
        await sync_release(
            repo_path=repo_path,
            prev_sha=fixture_repo.initial_sha,
            rel_sha=r1.commit_sha,
            tag=r1.tag,
            timestamp=r1.timestamp,
        )

        findings = await analyze(stack_trace=INCIDENT_CHRONOLOGICAL.stack_trace)

        # The true root cause (validate_card, R1) must be found.
        root_cause_matches = [
            f for f in findings if f.qualified_name == INCIDENT_CHRONOLOGICAL.root_cause_qualified_name
        ]
        assert root_cause_matches, "R1 root cause (validate_card) must surface in chronological replay"
        assert root_cause_matches[0].release_tag == r1.tag

        # R2's change must never appear -- it postdates the incident and was
        # deliberately never synced above.
        r2_leak = [f for f in findings if f.release_tag == r2.tag]
        assert r2_leak == [], "R2 changes must never leak into a replay predating R2"

        leaked_charge_card = [f for f in findings if f.qualified_name == "app.payments.charge_card"]
        assert leaked_charge_card == [], "charge_card (R2-only change) must not appear before R2 existed"

    async def test_full_history_replay_would_otherwise_leak_r2(self, fixture_repo, clean_cognee):
        """Contrast case: if both releases ARE synced (i.e. replaying against
        today's full graph instead of the incident-era graph), R2's
        charge_card change becomes visible in an unrestricted query. This
        documents *why* chronological replay (previous test) must build the
        graph only up to the incident's era rather than querying today's
        graph and filtering after the fact -- the plan explicitly warns
        against "replaying against today's graph leaks future fixes"."""
        repo_path = str(fixture_repo.repo_path)
        r1 = fixture_repo.r1
        r2 = fixture_repo.r2

        await build_baseline(repo_path)
        await sync_release(
            repo_path=repo_path,
            prev_sha=fixture_repo.initial_sha,
            rel_sha=r1.commit_sha,
            tag=r1.tag,
            timestamp=r1.timestamp,
        )
        await sync_release(
            repo_path=repo_path,
            prev_sha=r1.commit_sha,
            rel_sha=r2.commit_sha,
            tag=r2.tag,
            timestamp=r2.timestamp,
        )

        # Querying with no release_window restriction after both releases are
        # synced is the "graph as of today" case -- charge_card (R2) is
        # reachable at all here, precisely because R2 was synced. This
        # confirms the previous test's "R2 never leaks" result came from
        # chronological staging (R2 simply never having been synced yet),
        # not from R2 data being unreachable/broken in general.
        r2_findings_direct = await analyze(stack_trace='''Traceback (most recent call last):
  File "app/payments.py", line 19, in charge_card
    return {"card": card["number"][-4:], "amount": amount + fee, "status": "charged"}
''')
        r2_matches = [f for f in r2_findings_direct if f.qualified_name == "app.payments.charge_card"]
        assert r2_matches, "charge_card (R2) must be reachable once R2 has actually been synced"
        assert r2_matches[0].release_tag == r2.tag
