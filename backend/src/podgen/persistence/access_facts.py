"""One owner for reachable authorization facts in a bounded snapshot."""

from .artifact_access import ArtifactFacts
from .blob_access import BlobFacts
from .database import query
from .read_authorization import authorization_state, checked_node
from .runtime_access import RuntimeFacts


def access_facts(repo, tx):
    state = authorization_state(repo, tx)
    if (
        state is None
        or state.transaction is not tx
        or state.actor != repo.actor_id
        or state.purpose != repo.purpose
    ):
        return None
    return checked_node(repo, tx, ("access_facts",), lambda: AccessFacts(repo, tx))


class AccessFacts:
    def __init__(self, repo, tx):
        from .publication_scope import owner_for

        self.publication = owner_for(tx)
        self.repo, self.tx, self.roles = repo, tx, {}
        self.blobs = BlobFacts(repo, tx, self.role, publication=self.publication)
        self.artifacts = ArtifactFacts(tx, self.blobs, publication=self.publication)
        state = repo._read_authorization.get()
        self.runtimes = RuntimeFacts(
            tx,
            self.blobs,
            self.artifacts,
            graph=repo._runtime_graph.get(),
            cache_writes=state is not None and state.transaction is tx and state.owner is not None,
            publication=self.publication,
        )

    def reset(self, *, preserve_roles=False):
        if not preserve_roles:
            self.roles.clear()
        self.blobs.rows.clear()
        self.artifacts.reset()
        self.runtimes.reset()

    def prefetch_runtime_links(self):
        """Load artifact→runtime frontiers as a union, before scalar authorization.

        This fetches metadata only. Every owner, kind, audience, grant, withdrawal
        and active-path check still runs at the existing authorization boundary.
        New runtime frontiers can discover more artifacts; each artifact's links
        enter the queue once in this snapshot.
        """
        from collections import defaultdict

        from podgen.domain.contracts.common import RuntimeRef

        while self.artifacts.runtime_frontiers:
            groups = defaultdict(set)
            while self.artifacts.runtime_frontiers:
                owner, links = self.artifacts.runtime_frontiers.popleft()
                groups[owner].update(RuntimeRef(**link) for link in links)
            for owner, references in groups.items():
                self.runtimes.prefetch(owner, references)

    def role(self, project):
        if self.publication is not None:
            self.publication.sync()
        if project not in self.roles:
            rows = list(
                query(
                    self.tx,
                    "SELECT role FROM project_members WHERE project_id=:p AND actor_id=:a",
                    p=project,
                    a=self.repo.actor_id,
                ).mappings()
            )
            self.roles[project] = dict(rows[0]) if rows else None
        return self.roles[project]
