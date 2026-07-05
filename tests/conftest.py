"""Shared pytest fixtures for the RCA test suite.

Markers:
    graph - the test touches the cognee graph/dataset (Kuzu/LanceDB/SQLite
            state under rca.env.DATASET). These tests get their state reset
            via `clean_cognee` before they run. Pure extraction/parsing tests
            (tree-sitter only, no cognee ingest) should NOT use this marker
            so they can run without any cognee state or provider configured.
"""

from __future__ import annotations

import pytest

from tests.fixtures.repo_builder import FixtureRepo, build_fixture_repo


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "graph: test touches the cognee graph/dataset (uses clean_cognee)."
    )


@pytest.fixture(scope="session")
def fixture_repo(tmp_path_factory: pytest.TempPathFactory) -> FixtureRepo:
    """Build the synthetic git repo once per test session.

    Session-scoped because building it involves several real `git` subprocess
    calls (init, commits, tags) and the repo is read-only from the tests'
    perspective -- nothing in the RCA pipeline should mutate the working tree
    of the fixture repo itself (diff_sync reads history via `git diff`/`git
    show`, it doesn't checkout or write into the tree).
    """
    tmp_path = tmp_path_factory.mktemp("rca_fixture_repo")
    return build_fixture_repo(tmp_path)


@pytest.fixture
async def clean_cognee():
    """Reset cognee's graph/vector/relational state before a graph test runs.

    Import is deferred until the fixture actually runs so that pure
    extraction tests (which don't request this fixture) never require the
    `rca` package -- or cognee itself -- to be importable/configured.
    """
    from rca import env

    await env.reset()
    yield
    # No teardown reset: the next graph test resets at its own setup via this
    # same fixture, keeping failures inspectable (state survives until the
    # next test's clean start) while still guaranteeing hermeticity per test.
