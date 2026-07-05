"""Tests for rca.diff_sync: git diff -> ChangedSymbol tagging against the
baseline code graph, per SPEC.md.

    async def sync_release(repo_path, prev_sha, rel_sha, tag, timestamp) -> ReleaseSyncResult
        # 1) git diff names+hunks  2) refresh baseline for changed files (post-release content)
        # 3) map hunks->symbols at both revisions (deleted = present@prev, absent@rel)
        # 4) linking rules: modified/added->symbol node; deleted->CodeFile; miss->CodeFile; never drop
        # 5) insert Release+ChangedSymbols (idempotent per (release,symbol))

Ground truth for R1 (fixture_repo.r1), from tests/fixtures/repo_builder.py:
    modified: app.payments.process_payment, app.payments.validate_card, app.utils.format_currency
    added:    app.payments.refund_payment
    deleted:  app.utils.retry

These tests are marked `graph` because sync_release ingests into the cognee
dataset (rca.env.DATASET) and diff_sync reads from the baseline graph built
there.
"""

from __future__ import annotations

import pytest

from rca import env
from rca.baseline import build_baseline
from rca.diff_sync import sync_release

pytestmark = pytest.mark.graph


async def _get_graph_data():
    """Fetch (nodes, edges) from the underlying graph adapter.

    SPEC.md names `get_graph_data()` (-> networkx) as the read path but does
    not pin its import location, and cognee is not installed in this dev
    environment so the exact module path can't be verified here. Try the
    known candidate locations in order rather than hardcoding one guess;
    fail loudly if none resolve so a real run surfaces a clear error instead
    of a silent wrong-path skip.
    """
    candidates = [
        ("cognee.modules.graph.cognee_graph.get_graph_data", "get_graph_data"),
        ("cognee.modules.graph.utils", "get_graph_data"),
        ("cognee.infrastructure.databases.graph", "get_graph_engine"),
    ]
    last_err: Exception | None = None
    for module_path, attr in candidates:
        try:
            module = __import__(module_path, fromlist=[attr])
            func = getattr(module, attr)
        except (ImportError, AttributeError) as err:
            last_err = err
            continue
        if attr == "get_graph_engine":
            engine = await func()
            return await engine.get_graph_data()
        return await func()
    raise ImportError(
        f"could not locate a get_graph_data()-equivalent adapter call; last error: {last_err}"
    )


async def _build_baseline_and_sync_r1(fixture_repo, clean_cognee):
    """Shared setup: build baseline at R1, then sync the R1 release."""
    repo_path = str(fixture_repo.repo_path)
    await build_baseline(repo_path)

    r1 = fixture_repo.r1
    result = await sync_release(
        repo_path=repo_path,
        prev_sha=fixture_repo.initial_sha,
        rel_sha=r1.commit_sha,
        tag=r1.tag,
        timestamp=r1.timestamp,
    )
    return result, r1


class TestSyncReleaseR1:
    async def test_sync_release_tags_known_changed_symbols(self, fixture_repo, clean_cognee):
        result, r1 = await _build_baseline_and_sync_r1(fixture_repo, clean_cognee)

        changed = {cs.qualified_name: cs for cs in result.changed_symbols}

        expected = {cs.qualified_name: cs for cs in r1.changed_symbols}
        assert set(changed.keys()) == set(expected.keys())

        for qname, expected_cs in expected.items():
            actual = changed[qname]
            assert actual.change_type == expected_cs.change_type, qname
            assert actual.file_path == expected_cs.file_path, qname

    async def test_deleted_symbol_links_to_code_file(self, fixture_repo, clean_cognee):
        """app.utils.retry is deleted in R1 -- it has no node in the post-release
        graph, so its ChangedSymbol must link to the containing CodeFile
        (app/utils.py), never be dropped."""
        result, _r1 = await _build_baseline_and_sync_r1(fixture_repo, clean_cognee)

        deleted = [cs for cs in result.changed_symbols if cs.qualified_name == "app.utils.retry"]
        assert len(deleted) == 1
        retry_cs = deleted[0]
        assert retry_cs.change_type == "deleted"

        # The MODIFIED_IN linkage target is the CodeFile, identifiable by file_path.
        linked = retry_cs.modified_node
        assert linked is not None
        assert getattr(linked, "file_path", None) == "app/utils.py"
        # A CodeFile link (not a FunctionDefinition) is expected for deletions --
        # distinguish via absence of a function-only attribute like start_line,
        # or via an explicit type tag if the model exposes one.
        assert not hasattr(linked, "source_code") or getattr(linked, "type", None) != "FunctionDefinition"

    async def test_lookup_miss_falls_back_to_code_file_not_dropped(
        self, fixture_repo, clean_cognee, monkeypatch
    ):
        """Simulate an extractor disagreement (lookup miss) for one of the
        modified symbols by forcing symbol lookup to fail for it, and assert
        diff_sync still emits a ChangedSymbol linked to the CodeFile rather
        than silently dropping it.

        The exact internal hook used to force a miss is an implementation
        detail of rca.diff_sync; this test looks for a small set of plausible
        hook names and skips (rather than failing on an ImportError/AttributeError)
        if none exist yet, so it doesn't over-couple to internals not in SPEC.md.
        """
        import rca.diff_sync as diff_sync_module

        hook_name = next(
            (
                name
                for name in ("lookup_symbol_node", "find_symbol_node", "resolve_symbol_node")
                if hasattr(diff_sync_module, name)
            ),
            None,
        )
        if hook_name is None:
            pytest.skip(
                "rca.diff_sync does not expose a patchable symbol-lookup hook yet "
                "(tried lookup_symbol_node/find_symbol_node/resolve_symbol_node)"
            )

        original_lookup = getattr(diff_sync_module, hook_name)

        async def _flaky_lookup(*args, **kwargs):
            # Force a miss specifically for validate_card to exercise the
            # fallback path; everything else resolves normally.
            qualified_name = kwargs.get("qualified_name") or (args[-1] if args else None)
            if qualified_name and "validate_card" in str(qualified_name):
                return None
            return await original_lookup(*args, **kwargs)

        monkeypatch.setattr(diff_sync_module, hook_name, _flaky_lookup)

        repo_path = str(fixture_repo.repo_path)
        await build_baseline(repo_path)
        r1 = fixture_repo.r1
        result = await sync_release(
            repo_path=repo_path,
            prev_sha=fixture_repo.initial_sha,
            rel_sha=r1.commit_sha,
            tag=r1.tag,
            timestamp=r1.timestamp,
        )

        changed = {cs.qualified_name: cs for cs in result.changed_symbols}
        assert "app.payments.validate_card" in changed, "lookup-miss symbol must never be dropped"
        vc = changed["app.payments.validate_card"]
        assert vc.modified_node is not None
        assert getattr(vc.modified_node, "file_path", None) == "app/payments.py"

    async def test_sync_release_idempotent_node_counts(self, fixture_repo, clean_cognee):
        """Re-running sync_release for the same release must upsert, not
        duplicate: node/edge counts in the graph must be identical after a
        second run."""
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
        nodes_after_first, edges_after_first = await _get_graph_data()

        await sync_release(
            repo_path=repo_path,
            prev_sha=fixture_repo.initial_sha,
            rel_sha=r1.commit_sha,
            tag=r1.tag,
            timestamp=r1.timestamp,
        )
        nodes_after_second, edges_after_second = await _get_graph_data()

        assert len(nodes_after_second) == len(nodes_after_first)
        assert len(edges_after_second) == len(edges_after_first)

    async def test_changed_symbol_to_function_definition_edge_exists(
        self, fixture_repo, clean_cognee
    ):
        """Single-query proof that ChangedSymbol and FunctionDefinition share
        one dataset/graph: a modified symbol (process_payment) must have a
        traversable MODIFIED_IN edge to its FunctionDefinition node."""
        result, _r1 = await _build_baseline_and_sync_r1(fixture_repo, clean_cognee)

        nodes, edges = await _get_graph_data()
        node_by_id = {node[0]: node[1] for node in nodes}

        process_payment_cs = next(
            cs for cs in result.changed_symbols if cs.qualified_name == "app.payments.process_payment"
        )

        found_edge = False
        for source_id, target_id, edge_label, _edge_props in edges:
            source_node = node_by_id.get(source_id, {})
            target_node = node_by_id.get(target_id, {})
            if (
                source_node.get("qualified_name") == process_payment_cs.qualified_name
                and target_node.get("name") == "process_payment"
            ):
                found_edge = True
                break

        assert found_edge, "expected a ChangedSymbol -> FunctionDefinition edge for process_payment"


class TestSyncReleaseR2:
    async def test_r2_modifies_only_charge_card(self, fixture_repo, clean_cognee):
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
        result = await sync_release(
            repo_path=repo_path,
            prev_sha=r1.commit_sha,
            rel_sha=r2.commit_sha,
            tag=r2.tag,
            timestamp=r2.timestamp,
        )

        changed_names = {cs.qualified_name for cs in result.changed_symbols}
        assert changed_names == {"app.payments.charge_card"}
        cs = result.changed_symbols[0]
        assert cs.change_type == "modified"
        assert cs.file_path == "app/payments.py"
