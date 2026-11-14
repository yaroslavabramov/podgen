# Document publication

Acquisition owns durable request, received-original and extracted-output receipts.
A receipt commits with its verified blob binding. Storage and extraction run outside
SQL transactions; each publication checks fresh membership and byte withdrawal.
The final document publishes one metadata-complete SourceRevision, its extraction
configuration, SourceRepresentation and document outcome atomically. Failed
extraction publishes the received source without a representation.

The common artifact publisher accepts a bounded graph of named artifact/runtime
nodes and prepared bytes. Singleton creation and package import use this same
implementation. It reads all operation identities before substituting local refs,
then validates fingerprints and the union of external inputs. New nodes resolve
from the complete in-memory graph. Tables, links and events use multirow inserts;
returned rows form envelopes without post-insert reads. Only SQL is retried.

Acquisition journal version 2 has a closed reference policy. Configuration and
control provenance are not generic recursive read grants. Request policies retain
privacy propagation to received bytes; extraction retains its original-byte
privacy dependency. Owned bytes always retain fresh SHA classification/revocation
checks. Output refs retain ordinary artifact authorization. Unknown reference
paths fail validation; callers cannot supply permission flags. Legacy records keep
their original projection and exact saved identities/results. Recovery decodes
legacy checkpoints into the same publisher rather than rewriting prior artifacts.

A lost commit acknowledgement is resolved by immutable operation/receipt identity.
An existing storage binding under a different key must be verified outside SQL
before it can enter a receipt. Serialization retries reuse prepared bytes and do
not repeat fetch/extraction. Completed outcomes are never inferred from memory.


PublicationScope owns only the exact root/savepoint identity and active read
views. Replacing the root transaction fails, including immediately before the
archive callback returns. Savepoint changes clear authorization facts in place;
read fast paths synchronize scope before using a positive result. SQL mutations
invalidate every active purpose view. Immutable invocation metadata is evicted
when its reference topology changes. No transaction-local descriptor/body/access
cache survives between publication nodes. Multirow statements and identity reads
are chunked at 64 within the same atomic transaction; this does not impose a new
64-node limit on existing packages.

AcquisitionStore.start owns one initialization transaction: exact request/result
and recovery-state lookup, optional request registration, and the authorized
configuration read. Only immutable values leave it. The selected configuration
remains bound to the request; authorization decisions do not survive network or
storage waits. Subsequent blob receipts recheck effective rights before commit.
