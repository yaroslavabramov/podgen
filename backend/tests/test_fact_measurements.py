from collections import Counter
from types import SimpleNamespace

import pytest

from podgen.runtime.evaluation.fact_source_experiment.measurements import Measurements


def test_resumed_phase_counts_both_intervals_and_keeps_export_separate(database):
    objects = SimpleNamespace(counts=Counter())
    metrics = Measurements(database, objects)
    with database.engine.connect() as conn:
        with pytest.raises(InterruptedError), metrics.phase("reading"):
            conn.exec_driver_sql("SELECT 1")
            objects.counts.update(read_bytes=17)
            raise InterruptedError
        with metrics.phase("reading"):
            conn.exec_driver_sql("SELECT 2")
            objects.counts.update(read_bytes=23, read_calls=1)
        with metrics.phase("export"):
            conn.exec_driver_sql("SELECT 3")

    reading = metrics.snapshot()["phases"]["reading"]
    assert reading["intervals"] == reading["sql_calls"] == reading["sql_select"] == 2
    assert reading["objects"] == {"read_bytes": 40, "read_calls": 1}
    assert metrics.phases["export"]["sql_calls"] == metrics.phases["export"]["intervals"] == 1
    assert metrics.phases["export"]["objects"] == {}
