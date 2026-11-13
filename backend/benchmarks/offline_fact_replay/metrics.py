"""Exclusive SQL attribution and inclusive call counters for the entire replay."""

import functools
import time
from collections import Counter, defaultdict
from contextlib import contextmanager

from sqlalchemy import event

from podgen.runtime.evaluation.fact_source_experiment.measurements import Measurements


class Metrics(Measurements):
    def __init__(self, database, objects):
        super().__init__(database, objects)
        self.stack, self.operations, self.templates = [], defaultdict(Counter), Counter()
        self.phase_name = "outside"
        self.turn = None
        self.turns = Counter()
        event.listen(database.engine, "before_cursor_execute", self.sql)

    def sql(self, connection, cursor, statement, parameters, context, many):
        name = "/".join(self.stack) or "other"
        self.operations[self.phase_name + ":" + name]["sql"] += 1
        self.templates[(self.phase_name, " ".join(statement.split()))] += 1
        if self.phase_name == "fact_review":
            self.turns[str(self.turn)] += 1

    @contextmanager
    def phase(self, name):
        previous = self.phase_name
        self.phase_name = name
        try:
            with super().phase(name):
                yield
        finally:
            self.phase_name = previous

    @contextmanager
    def operation(self, name):
        self.stack.append(name)
        key = self.phase_name + ":" + "/".join(self.stack)
        started = time.monotonic()
        try:
            yield
        finally:
            self.operations[key]["calls"] += 1
            self.operations[key]["seconds"] += time.monotonic() - started
            self.stack.pop()

    def wrap(self, cls, name, label=None):
        original = getattr(cls, name)

        @functools.wraps(original)
        def wrapped(instance, *args, **kwargs):
            tag = label(instance, *args, **kwargs) if label else cls.__name__ + "." + name
            with self.operation(tag):
                return original(instance, *args, **kwargs)

        setattr(cls, name, wrapped)

    def snapshot(self):
        return dict(
            super().snapshot(),
            sql_including_coordination=sum(self.templates.values()),
            turn_sql=dict(self.turns),
            operations=dict(self.operations),
            sql_templates=[
                dict(phase=phase, sql=sql, count=count)
                for (phase, sql), count in self.templates.most_common()
            ],
        )
