"""Custom DataPoint node types for the release/diff layer.

`Release` and `ChangedSymbol` subclass cognee's `DataPoint` so they live in the
same graph as the built-in CodeGraphEntities (CodeFile / FunctionDefinition /
...). The `ChangedSymbol.modified_node` typed reference is the MODIFIED_IN edge:
it points at the exact baseline node (FunctionDefinition / ClassDefinition, or a
CodeFile for deletions / lookup misses) the change corresponds to.

Node ids are deterministic (SPEC hard rule 2): every id we mint is
uuid5(env.NAMESPACE, f"{type}:{...}") so re-ingesting a release upserts the same
nodes instead of duplicating them.
"""

from __future__ import annotations

from typing import Literal, Union
from uuid import UUID, uuid5

from cognee.low_level import DataPoint
from cognee.shared.CodeGraphEntities import CodeFile

from rca.env import NAMESPACE
from rca.extract import ClassDefinition, FunctionDefinition

ChangeType = Literal["added", "modified", "deleted"]

# The three node kinds a ChangedSymbol can point its MODIFIED_IN edge at.
ModifiedNode = Union[FunctionDefinition, ClassDefinition, CodeFile]


def release_id(tag: str) -> UUID:
    return uuid5(NAMESPACE, f"Release:{tag}")


def changed_symbol_id(tag: str, qualified_name: str) -> UUID:
    # Idempotent per (release, symbol): re-syncing the same release upserts.
    return uuid5(NAMESPACE, f"ChangedSymbol:{tag}:{qualified_name}")


class Release(DataPoint):
    commit_sha: str
    prev_sha: str
    tag: str
    timestamp: str
    metadata: dict = {"index_fields": ["tag"]}


class ChangedSymbol(DataPoint):
    qualified_name: str
    file_path: str
    change_type: ChangeType
    diff_hunk: str
    release: Release
    # The MODIFIED_IN target: the baseline node this change lands on.
    modified_node: ModifiedNode
    metadata: dict = {"index_fields": ["diff_hunk"]}
