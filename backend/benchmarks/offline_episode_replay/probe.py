"""Client SQL stack attribution for the full offline episode operation."""

import json
import re
import resource
import sys
import time
from collections import Counter, defaultdict
from contextlib import contextmanager
from pathlib import Path

from server_probe import ServerProbe
from sqlalchemy import event


class Probe:
    def __init__(self, database, output):
        self.server = ServerProbe(database.engine.url)
        self.output = Path(output)
        self.phase = "setup"
        self.counts = Counter()
        self.stacks = Counter()
        self.templates = Counter()
        self.tables = Counter()
        self.durations = defaultdict(float)
        self.started = time.monotonic()
        event.listen(database.engine, "before_cursor_execute", self.before)
        event.listen(database.engine, "after_cursor_execute", self.after)

    def before(self, connection, cursor, statement, parameters, context, many):
        n = len(parameters) if many else 1
        frames = []
        f = sys._getframe(1)
        while f:
            filename = f.f_code.co_filename
            if "/backend/src/podgen/" in filename:
                frames.append(
                    filename.split("/backend/src/podgen/", 1)[1]
                    + ":"
                    + f.f_code.co_name
                    + ":"
                    + str(f.f_lineno)
                )
            f = f.f_back
        template = " ".join(statement.split())
        stack = tuple(reversed(frames))
        self.counts[self.phase] += n
        self.stacks[(self.phase, stack)] += n
        self.templates[(self.phase, template)] += n
        table = re.search(r"\b(?:FROM|INTO|UPDATE)\s+([a-z_]+)", statement, re.I)
        if table:
            self.tables[(self.phase, table[1])] += n
        context._audit_started = time.monotonic()

    def after(self, connection, cursor, statement, parameters, context, many):
        self.durations[self.phase] += time.monotonic() - context._audit_started

    @contextmanager
    def scope(self, name):
        old, self.phase = self.phase, name
        server_before = self.server.snapshot()
        started = time.monotonic()
        try:
            yield
        finally:
            self.durations[name + ":wall"] += time.monotonic() - started
            self.server.finish_phase(name, server_before)
            self.phase = old
            self.save()

    def save(self):
        self.output.write_text(
            json.dumps(
                dict(
                    sql=dict(self.counts),
                    server_sql=self.server.report(),
                    seconds=dict(self.durations),
                    peak_rss_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                    stacks=[dict(phase=p, stack=list(s), count=n) for (p, s), n in self.stacks.most_common()],
                    templates=[dict(phase=p, sql=s, count=n) for (p, s), n in self.templates.most_common()],
                    tables=[dict(phase=p, table=t, count=n) for (p, t), n in self.tables.most_common()],
                ),
                ensure_ascii=False,
                indent=2,
            )
        )
