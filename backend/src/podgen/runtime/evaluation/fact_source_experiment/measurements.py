"""Counters for this controlled reading, separate from anything delivered to the model."""

import resource
import sys
import time
from collections import Counter
from contextlib import contextmanager

from sqlalchemy import event

from podgen.persistence.objects import FileObjectStore


class MeasuredObjects(FileObjectStore):
    def __init__(self, path):
        super().__init__(path)
        self.counts = Counter()

    def read(self, project, ref):
        value = super().read(project, ref)
        self.counts.update(read_calls=1, read_bytes=len(value))
        return value

    def iter_read(self, project, ref, **options):
        self.counts["stream_calls"] += 1
        for chunk in super().iter_read(project, ref, **options):
            self.counts["read_bytes"] += len(chunk)
            yield chunk

    @contextmanager
    def stage(self, project, stream, media_type, **options):
        with super().stage(project, stream, media_type, **options) as staged:
            self.counts.update(stage_calls=1, staged_bytes=staged.byte_length)
            yield staged

    def publish(self, project, staged):
        result = super().publish(project, staged)
        self.counts.update(publish_calls=1, published_bytes=result.byte_length)
        return result


class Measurements:
    def __init__(self, database, objects):
        self.database, self.objects = database, objects
        self.phases = {}
        self.search_calls = 0
        self.search_seconds = 0

    @contextmanager
    def phase(self, name):
        counters = Counter()
        before = self.objects.counts.copy()

        def sql(_connection, _cursor, statement, _parameters, _context, _many):
            counters["sql_calls"] += 1
            counters["sql_" + statement.split()[0].lower()] += 1

        event.listen(self.database.engine, "before_cursor_execute", sql)
        started = time.monotonic()
        try:
            yield
        finally:
            elapsed = time.monotonic() - started
            event.remove(self.database.engine, "before_cursor_execute", sql)
            phase = self.phases.setdefault(name, {"objects": Counter()})
            for key, value in dict(counters, seconds=elapsed, intervals=1).items():
                phase[key] = phase.get(key, 0) + value
            phase["objects"].update(self.objects.counts - before)

    def instrument_search(self, corpus):
        original = corpus.retrieval.search

        def measured(arguments):
            started = time.monotonic()
            self.search_calls += 1
            try:
                return original(arguments)
            finally:
                self.search_seconds += time.monotonic() - started

        corpus.retrieval.search = measured

    def snapshot(self):
        return dict(
            phases=self.phases,
            search_calls=self.search_calls,
            search_seconds=self.search_seconds,
            process_peak_rss_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            * (1 if sys.platform == "darwin" else 1024),
            limitations=[
                "Object counters cover the repository store, not cached Unicode projection reads.",
                "Staging/publication are logical store operations, not physical disk writes; RSS is the process-wide high-water mark.",
                "Provider costs and token/cache usage remain in complete native trace receipts; USD is not inferred from an unpinned price.",
            ],
        )
