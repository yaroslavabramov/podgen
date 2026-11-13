"""Bounded traversal and batched writes preserve privacy, including cycles and SHA aliases."""

import random
from uuid import uuid4

from podgen.persistence.classification import classify_evaluator
from podgen.persistence.privacy import revoke_content
from podgen.persistence.privacy_graph import BATCH_SIZE, protected_hashes
from tests.privacy_graph_cases import Graph, GraphConnection, checksum, identity


def test_forward_alias_closure_preserves_sources_removed_edges_catalog_assets_and_cycles():
    primary, root, alias, derived, catalog = (identity(i) for i in range(1, 6))
    graph = Graph(
        runtime_inputs={(root, primary), (derived, alias), (alias, derived)},
        runtime_blobs={
            (primary, checksum(1)),
            (root, checksum(2)),
            (alias, checksum(2)),
            (derived, checksum(3)),
        },
        artifact_runtimes={(catalog, derived)},
        catalog_blobs={(catalog, checksum(4))},
        original={(identity(6), catalog)},
        revalidation={(identity(7), identity(6))},
        artifact_blobs={(identity(7), checksum(5))},
    )
    tx = GraphConnection(graph)
    actual = protected_hashes(tx, uuid4(), runtime_ids=[root])
    assert actual == graph.legacy_closure(runtimes=[root]) == {checksum(i) for i in range(2, 6)}
    expanded = [
        (kind, value)
        for args in tx.expansions
        for kind, key in [("a", "artifacts"), ("r", "runtimes"), ("b", "hashes")]
        for value in args[key]
    ]
    assert len(expanded) == len(set(expanded))


def test_random_graphs_match_historical_fixed_point_without_pruning_already_classified_nodes():
    randomizer = random.Random(213)
    for _ in range(100):
        nodes = [identity(i) for i in range(1, 25)]
        hashes = [checksum(i) for i in range(8)]
        graph = Graph(
            **{
                field: {
                    (randomizer.choice(nodes), randomizer.choice(hashes if "blobs" in field else nodes))
                    for _ in range(40)
                }
                for field in Graph.__dataclass_fields__
            }
        )
        roots = randomizer.sample(nodes, 2)
        assert protected_hashes(
            GraphConnection(graph), uuid4(), artifact_ids=roots[:1], runtime_ids=roots[1:]
        ) == graph.legacy_closure(roots[:1], roots[1:])


def test_large_shared_hash_component_is_reached_once_and_writes_are_batched():
    root = identity(1)
    graph = Graph(runtime_blobs={(root, checksum(1))})
    for number in range(2, 1502):
        graph.runtime_blobs.update({(identity(number), checksum(1)), (identity(number), checksum(number))})
    # Unrelated project history does not become part of the operation frontier.
    graph.runtime_blobs.add((identity(9999), checksum(9999)))
    tx = GraphConnection(graph)
    classify_evaluator(tx, uuid4(), runtime_id=root)
    assert len(tx.writes) == 3
    assert all(len(args["hashes"]) <= BATCH_SIZE for _, args in tx.writes)
    assert {sha for _, args in tx.writes for sha in args["hashes"]} == {checksum(i) for i in range(1, 1502)}
    assert all(
        sum(len(args[k]) for k in ("artifacts", "runtimes", "hashes")) <= BATCH_SIZE for args in tx.expansions
    )
    assert all(
        identity(9999) not in args["runtimes"] and checksum(9999) not in args["hashes"]
        for args in tx.expansions
    )


def test_access_revocation_uses_same_closure_and_one_batch_for_multiple_hashes():
    root = identity(1)
    tx = GraphConnection(Graph(artifact_blobs={(root, checksum(i)) for i in range(20)}))
    event, project = uuid4(), uuid4()
    revoke_content(tx, project, root, event, "source withdrawal")
    assert len(tx.writes) == 1
    sql, params = tx.writes[0]
    assert "blob_access_revocations" in sql
    assert params["id"] == event and params["p"] == project
    assert set(params["hashes"]) == {checksum(i) for i in range(20)}
