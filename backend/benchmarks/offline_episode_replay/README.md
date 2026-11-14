# Complete offline episode replay

Run sequentially against a pre-episode PostgreSQL dump. The runner creates an
isolated database/object directory, migrates it using `--code-root`, and invokes
the ordinary episode product. It does not connect to the live database.

Inputs are private retained artifacts, not checked into Git:

- `--state`: actor/project identities and the source object-store directory.
- `--dump`: database snapshot before the episode or its index was built.
- `--export`: the complete original production export, including native requests,
  responses, count receipts, research exchanges and final audio.
- `--fixtures`: `fetch-fixtures.json`, `audio-fixtures.json` and their `body_file`
  payloads. Fetch entries contain requested/resolved URL metadata, role, full
  extraction result and retained HTTP body. Audio entries bind the exact synthesis
  request to its complete HTTP receipt/body.
- `--code-root`: checkout being measured; baseline and candidate use the same inputs.
- `--out`: a new directory. `--pg-bin`: PostgreSQL 17 binaries with pgvector and
  pg_stat_statements installed.

Only external responses and extraction outputs are replaced. Retrieval inference,
tools, SQL, storage, decoding, master assembly and export run normally. HTTP dispatch
through both installed HTTP clients is forbidden. Local model assets must already
be available. Do not load credentials for this benchmark.

Every native generation/count request is compared before a retained answer can be
consumed. Run metadata and opaque catalog/result version identifiers are normalized;
content, instructions, tools and other request parameters must agree. Final script
identity and WAV bytes must match. A completed resume must consume no more responses.

Outputs include SQL call-site/phase metrics, object traffic, peak RSS, the equality
oracle, complete export, completed database dump and immutable object copy. The
additional `cold_outcome_read` phase is separate from complete-run totals. SQL counts
in `sql` are client executions. `server_sql` separately reports PostgreSQL top-level
and nested statements (including function/trigger SQL), rows, buffers, temporary
traffic and WAL. The observer uses a separate untracked connection; it never resets
statistics. Eviction or reset invalidates the measurement. Top-level time and buffer
metrics include nested work: do not add them to the nested totals. Phase snapshots
are inclusive and can overlap. `completed_operation` ends after the completed
resume, before the additional cold read; `overall` covers the entire measured worker.
Elapsed time excludes paid provider latency; peak RSS units in this report are macOS
bytes. A failed or interrupted run is not a qualification result. Its profile and
failure log are retained, but its temporary database is stopped and removed.
