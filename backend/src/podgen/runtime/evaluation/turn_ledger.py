"""Invocation-owned accounting of committed physical turns; no permission cache."""

from . import model_turns


class TurnLedger:
    def __init__(self, journal, root, output_limit, history):
        self.journal, self.root, self.output_limit, self.history = journal, root, output_limit, history
        self.rows = {row["turn_index"]: row for row in model_turns.rows(journal, root)}
        self.charged = {
            row["turn_index"]
            for row in model_turns.charged_rows(
                journal, root, history=history, all_rows=list(self.rows.values())
            )
        }
        self.prefix = [(0, 0)]
        self.prefix_end = 0
        self.totals = dict(input=0, output=0, known_output=0, unknown=0)
        self.needs_reconciliation = False
        for index in self.charged:
            self._add(self.rows[index], 1)

    def _add(self, row, sign, *, totals=None):
        totals = self.totals if totals is None else totals
        totals["input"] += sign * (row["input_tokens"] or 0)
        totals["output"] += sign * (
            self.output_limit if row["output_tokens"] is None else row["output_tokens"]
        )
        totals["known_output"] += sign * (row["output_tokens"] or 0)
        totals["unknown"] += sign * (row["output_tokens"] is None)

    def projected(self, row):
        """Root usage projection for the same transaction as a physical update."""
        index = row["turn_index"]
        totals = dict(self.totals)
        if index in self.charged:
            self._add(self.rows[index], -1, totals=totals)
        submitted = self.history.is_submitted(index) or row["status"] in (
            "submitting",
            "waiting",
            "response_completed",
            "outcome_unknown",
        )
        if submitted:
            # Pure conservative projection. Explicit refusals are classified only
            # after commit, when their complete durable evidence is readable.
            self._add(row, 1, totals=totals)
        return dict(totals, unknown=bool(totals["unknown"]))

    def committed(self, row):
        index = row["turn_index"]
        if index in self.charged:
            self._add(self.rows[index], -1)
        self.rows[index] = dict(row)
        self.prefix_end = min(self.prefix_end, index)
        del self.prefix[self.prefix_end + 1 :]
        if self.history.is_submitted(index):
            # Explicit non-ambiguous refusals retain the existing evidence policy.
            if row["status"] == "failed" and row["provider_response_id"] is None:
                selected = model_turns.charged_rows(
                    self.journal, self.root, history=self.history, all_rows=[row]
                )
                if not selected:
                    self.charged.discard(index)
                    self.needs_reconciliation = True
                    return
            self.charged.add(index)
            self._add(row, 1)

    def before(self, index):
        while self.prefix_end < index:
            row = self.rows.get(self.prefix_end, {})
            previous = self.prefix[-1]
            self.prefix.append(
                (
                    previous[0] + (row.get("input_tokens") or 0),
                    previous[1]
                    + (self.output_limit if row.get("output_tokens") is None else row["output_tokens"])
                    if row
                    else previous[1],
                )
            )
            self.prefix_end += 1
        return dict(input=self.prefix[index][0], output=self.prefix[index][1])

    def usage(self):
        return dict(self.totals, unknown=bool(self.totals["unknown"]))
