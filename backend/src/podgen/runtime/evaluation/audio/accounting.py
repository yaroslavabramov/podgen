"""Accounting and immutable cell descriptors owned by one locked audio operation."""

from decimal import Decimal

from .contracts import VoiceCandidate
from .costs import charge, estimate
from .descriptors import SampleResult
from .history_snapshot import load


def cell_charge(rows):
    count, cost = 0, Decimal(0)
    for row in rows:
        if row["intent"] is None:
            continue
        count += 1
        if row.get("audio"):
            audio = row["audio"]
            cost += charge(
                estimate(
                    VoiceCandidate.model_validate(row["prepared"]["profile"]),
                    audio["usage"],
                    audio["usage_basis"],
                )
            )
        else:
            cost += Decimal(row["prepared"]["reservation_usd"])
    return count, cost


class OperationLedger:
    def __init__(self, journal, manifest):
        self.journal, self.manifest = journal, manifest
        self.rows = load(journal, manifest["cells"])
        self.charges = {
            identity: {row["key"]: cell_charge([row]) for row in rows} for identity, rows in self.rows.items()
        }
        self.cells = {cell_id: cell_charge(rows) for cell_id, rows in self.rows.items()}
        self.attempts = sum(row[0] for row in self.cells.values())
        self.committed = sum((row[1] for row in self.cells.values()), Decimal(0))
        self.dirty = set()

    def invalidate(self, cell):
        self.dirty.add(cell["cell_id"])

    def is_dirty(self, cell):
        return cell["cell_id"] in self.dirty

    def update(self, cell):
        identity = cell["cell_id"]
        if not self.rows[identity]:
            return
        row = self.rows[identity][-1]
        before = self.charges[identity].get(row["key"], (0, Decimal(0)))
        after = cell_charge([row])
        delta_count, delta_cost = after[0] - before[0], after[1] - before[1]
        self.attempts += delta_count
        self.committed += delta_cost
        self.cells[identity] = (self.cells[identity][0] + delta_count, self.cells[identity][1] + delta_cost)
        self.charges[identity][row["key"]] = after

    def refresh(self, cell):
        self.rows.update(load(self.journal, [cell]))
        identity = cell["cell_id"]
        before, after = self.cells[identity], cell_charge(self.rows[identity])
        self.attempts += after[0] - before[0]
        self.committed += after[1] - before[1]
        self.cells[identity] = after
        self.charges[identity] = {row["key"]: cell_charge([row]) for row in self.rows[identity]}
        self.dirty.discard(cell["cell_id"])

    def totals(self, journal, manifest):
        if journal is not self.journal or manifest is not self.manifest:
            raise ValueError("audio_ledger_operation_mismatch")
        for cell in manifest["cells"]:
            if self.is_dirty(cell):
                self.refresh(cell)
        return self.attempts, self.committed

    def result(self, cell, status):
        from .attempts import baseline

        self.totals(self.journal, self.manifest)
        selected = baseline(self.rows[cell["cell_id"]])
        reference = (
            self.journal.ref(selected["processing_key"] + ":result").model_dump(mode="json")
            if selected
            else None
        )
        count, cost = self.cells[cell["cell_id"]]
        return SampleResult(status, cell["cell_id"], reference, selected, count, cost)


def ledger(journal, manifest):
    return OperationLedger(journal, manifest).totals(journal, manifest)
