"""Tests for rca.query: stack trace -> ranked blast-radius Finding list.

Per SPEC.md:
    @dataclass class Finding: qualified_name; file_path; release_tag; change_type; hops; score; diff_hunk
    async def analyze(stack_trace: str | None, description: str | None = None,
                      k_hops: int = 2, release_window: list[str] | None = None) -> list[Finding]
        # seed: parse frames -> direct graph lookup
        # fallback (no trace): vector search over embedded fields
        # traverse depends_on + containment k hops; filter MODIFIED_IN window
        # rank: 0-hop >> (hops asc, recency desc, deleted/modified > added)

Uses INCIDENT_DIRECT_HIT (0-hop: trace names process_payment, changed in R1),
INCIDENT_IMPORT_HOP (1-hop: trace names process_payment, true cause is
format_currency reached via app.utils import), and INCIDENT_JS_UNRELATED
(negative control: formatPrice/submitOrder never change in any release).

These are graph tests: analyze() reads the ingested baseline + ChangedSymbol
graph, so the dataset must be built (baseline + both releases synced) before
each assertion.
"""

from __future__ import annotations

import pytest

from rca.baseline import build_baseline
from rca.diff_sync import sync_release
from rca.query import analyze

from tests.fixtures.incidents import (
    INCIDENT_DIRECT_HIT,
    INCIDENT_IMPORT_HOP,
    INCIDENT_JS_UNRELATED,
)

pytestmark = pytest.mark.graph


async def _seed_full_history(fixture_repo):
    """Build baseline at HEAD and sync both R1 and R2 releases."""
    repo_path = str(fixture_repo.repo_path)
    await build_baseline(repo_path)

    r1 = fixture_repo.r1
    await sync_release(
        repo_path=repo_path,
        prev_sha=fixture_repo.initial_sha,
        rel_sha=r1.commit_sha,
        tag=r1.tag,
        timestamp=r1.timestamp,
    )

    r2 = fixture_repo.r2
    await sync_release(
        repo_path=repo_path,
        prev_sha=r1.commit_sha,
        rel_sha=r2.commit_sha,
        tag=r2.tag,
        timestamp=r2.timestamp,
    )


class TestAnalyzeDirectHit:
    async def test_direct_hit_ranks_first_at_zero_hops(self, fixture_repo, clean_cognee):
        await _seed_full_history(fixture_repo)

        findings = await analyze(stack_trace=INCIDENT_DIRECT_HIT.stack_trace)

        assert len(findings) > 0
        top = findings[0]
        assert top.qualified_name == INCIDENT_DIRECT_HIT.root_cause_qualified_name
        assert top.file_path == INCIDENT_DIRECT_HIT.root_cause_file_path
        assert top.release_tag == INCIDENT_DIRECT_HIT.root_cause_release_tag
        assert top.hops == 0
        assert top.change_type == "modified"


class TestAnalyzeImportHop:
    async def test_import_hop_root_cause_found_at_hop_one(self, fixture_repo, clean_cognee):
        await _seed_full_history(fixture_repo)

        findings = await analyze(stack_trace=INCIDENT_IMPORT_HOP.stack_trace, k_hops=2)

        matches = [f for f in findings if f.qualified_name == INCIDENT_IMPORT_HOP.root_cause_qualified_name]
        assert matches, "true root cause (format_currency) must appear in findings"
        match = matches[0]
        assert match.hops == 1
        assert match.file_path == INCIDENT_IMPORT_HOP.root_cause_file_path
        assert match.release_tag == INCIDENT_IMPORT_HOP.root_cause_release_tag

    async def test_import_hop_ranks_below_any_zero_hop_finding(self, fixture_repo, clean_cognee):
        await _seed_full_history(fixture_repo)

        findings = await analyze(stack_trace=INCIDENT_IMPORT_HOP.stack_trace, k_hops=2)
        zero_hop = [f for f in findings if f.hops == 0]
        one_hop_root_cause = next(
            f for f in findings if f.qualified_name == INCIDENT_IMPORT_HOP.root_cause_qualified_name
        )

        if zero_hop:
            # 0-hop dominates per SPEC ranking rule.
            idx_zero = findings.index(zero_hop[0])
            idx_one = findings.index(one_hop_root_cause)
            assert idx_zero < idx_one


class TestAnalyzeUnrelatedRanksLower:
    async def test_unrelated_js_symbol_not_falsely_elevated(self, fixture_repo, clean_cognee):
        await _seed_full_history(fixture_repo)

        findings = await analyze(stack_trace=INCIDENT_JS_UNRELATED.stack_trace)

        # formatPrice/submitOrder never appear in any release's ChangedSymbols,
        # so they must not surface as a high-confidence (0-hop, in-window) finding.
        offending = [
            f
            for f in findings
            if f.qualified_name in {"web.helpers.formatPrice", "web.client.submitOrder"}
        ]
        assert offending == [], "unrelated, never-changed JS symbols must not be reported as findings"


class TestAnalyzeReleaseWindow:
    async def test_release_window_excludes_older_release(self, fixture_repo, clean_cognee):
        await _seed_full_history(fixture_repo)

        r2 = fixture_repo.r2
        # Restrict the window to only R2 -- R1's process_payment change must
        # be excluded even though the trace names it.
        findings = await analyze(
            stack_trace=INCIDENT_DIRECT_HIT.stack_trace,
            release_window=[r2.tag],
        )

        r1_named = [f for f in findings if f.qualified_name == INCIDENT_DIRECT_HIT.root_cause_qualified_name]
        assert r1_named == [], "R1 change must be excluded when release_window is restricted to R2 only"

    async def test_release_window_including_r1_surfaces_direct_hit(self, fixture_repo, clean_cognee):
        await _seed_full_history(fixture_repo)

        r1 = fixture_repo.r1
        findings = await analyze(
            stack_trace=INCIDENT_DIRECT_HIT.stack_trace,
            release_window=[r1.tag],
        )

        assert any(
            f.qualified_name == INCIDENT_DIRECT_HIT.root_cause_qualified_name
            and f.release_tag == r1.tag
            for f in findings
        )


class TestExceptionRelevance:
    """The raised exception is a causal signal: a changed symbol whose name/diff
    matches the exception text is boosted, so it can overtake unrelated changes
    that are equally close in the graph (even from a newer release)."""

    async def test_import_hop_cause_beats_unrelated_newer_change(self, fixture_repo, clean_cognee):
        await _seed_full_history(fixture_repo)

        # INC-1002 raises `ValueError: Unknown format code 'f' ...`; the keyword
        # "format" matches app.utils.format_currency (the true, 1-hop cause) but
        # NOT app.payments.charge_card (an unrelated change from newer R2 that is
        # also 1 hop away). Relevance must rank the true cause above it.
        findings = await analyze(stack_trace=INCIDENT_IMPORT_HOP.stack_trace, k_hops=2)

        true_cause = next(
            f for f in findings if f.qualified_name == INCIDENT_IMPORT_HOP.root_cause_qualified_name
        )
        # it is the best-ranked finding among everything one hop out or further
        one_plus_hop = [f for f in findings if f.hops >= 1]
        assert one_plus_hop, "expected at least one non-seed finding"
        assert one_plus_hop[0].qualified_name == true_cause.qualified_name, (
            "the exception-relevant true cause must be the top-ranked 1-hop suspect, "
            "ahead of unrelated newer-release changes at the same hop distance"
        )

    async def test_direct_hit_still_ranks_first(self, fixture_repo, clean_cognee):
        # The boost must not break the genuine direct-hit case: INC-1001's
        # exception ("invalid amount") matches process_payment's own diff, so it
        # stays #1 rather than being displaced.
        await _seed_full_history(fixture_repo)
        findings = await analyze(stack_trace=INCIDENT_DIRECT_HIT.stack_trace)
        assert findings[0].qualified_name == INCIDENT_DIRECT_HIT.root_cause_qualified_name


class TestPerSymbolDiffHunk:
    """diff_hunk must be sliced to the changed symbol, not the whole-file diff:
    two symbols changed in the same file+release must carry DIFFERENT hunks, and
    each hunk must contain that symbol's own change."""

    async def test_same_file_symbols_have_distinct_hunks(self, fixture_repo, clean_cognee):
        await _seed_full_history(fixture_repo)
        findings = await analyze(stack_trace=INCIDENT_DIRECT_HIT.stack_trace, k_hops=2)

        by_name = {f.qualified_name: f for f in findings}
        pp = by_name.get("app.payments.process_payment")
        vc = by_name.get("app.payments.validate_card")
        assert pp and vc, "both payments.py symbols should be present"

        # Same file + release, but the hunks must not be identical whole-file blobs.
        assert pp.diff_hunk and vc.diff_hunk
        assert pp.diff_hunk != vc.diff_hunk, "per-symbol slicing must differ, not repeat the file diff"
        # Each hunk contains its own symbol's signature line.
        assert "def process_payment" in pp.diff_hunk
        assert "def validate_card" in vc.diff_hunk
        # And process_payment's hunk carries the actual R1 change it's blamed for.
        assert "invalid amount" in pp.diff_hunk


class TestAnalyzeNoTraceFallback:
    async def test_no_trace_uses_description_vector_fallback(self, fixture_repo, clean_cognee):
        """With no stack_trace, analyze() must fall back to vector search over
        embedded fields using `description`, and must not raise merely because
        no trace was given. If no embedding provider is configured, it should
        degrade gracefully (empty/low-confidence results) rather than erroring
        with a network/API-key failure -- this test asserts it returns a list
        (possibly empty), never raises for the no-provider case."""
        await _seed_full_history(fixture_repo)

        findings = await analyze(
            stack_trace=None,
            description="thousands separator formatting throws on receipt display amount",
        )

        assert isinstance(findings, list)
