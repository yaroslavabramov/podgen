"""Operational research navigation, explicit checkpoints and durable material handoffs."""

import json
from uuid import uuid5

from podgen.domain.contracts.common import BlobRef

PAGE = 9600
CATALOG_PAGE = 6


def text_page(text, start, end):
    stop = min(len(text), start + PAGE) if end is None else end
    if not 0 <= start < stop <= len(text) or stop - start > PAGE:
        return dict(error="invalid_history_range", length=len(text))
    return dict(
        text=text[start:stop],
        start=start,
        end=stop,
        length=len(text),
        next_start=stop if stop < len(text) else None,
    )


class ResearchTools:
    def __init__(self, journal, root, corpus, history, guard):
        self.journal, self.root, self.corpus = journal, root, corpus
        self.history, self.guard = history, guard

    @property
    def last_search_diagnostics(self):
        return self.corpus.last_search_diagnostics

    def execute(self, name, args, *, read_id, **options):
        index = int(read_id[1:]) - 1
        if name in {"request_source", "request_research"}:
            resolver = self.corpus.material_resolver
            if resolver is None:
                return dict(error="source_acquisition_not_configured")
            resolution = resolver.resolve(
                name,
                args,
                operation_id=uuid5(self.root["invocation_id"], f"material-request:{index}"),
                run_id=self.root["run_id"],
                guard=self.guard,
            )
            self.restore(resolver.restore(run_id=self.root["run_id"], guard=self.guard))
            self.corpus.refresh()
            return resolution.result
        if name == "select_research_result":
            if self.root["stage"] not in {"plan", "plan-revise", "plan-review", "plan-review-revised"}:
                return dict(error="research_selection_requires_planner")
            if args["result_id"] not in self.corpus.supplemental_reports:
                return dict(error="unknown_research_result")
            return dict(
                selected=True, result_id=args["result_id"], delivery="full_report_if_this_plan_is_accepted"
            )
        if name in {"list_sources", "list_research_results", "list_research_sources"}:
            from .research_catalog import citation_page, report_page, source_page

            return {
                "list_sources": source_page,
                "list_research_results": report_page,
                "list_research_sources": citation_page,
            }[name](self.corpus, args)
        if name == "save_working_state":
            if (
                not args["state"].strip()
                or not args["resume_state"].strip()
                or set(args["receipt_ids"]) - self.history.receipts
            ):
                return dict(error="invalid_working_state_or_receipt")
            return dict(checkpoint=f"W{index + 1}", durable=True)
        if name == "list_work_history":
            offset = args["offset"]
            rows = []
            for kind, index in self.history.entries[offset : offset + CATALOG_PAGE]:
                entry = dict(kind=kind, index=index)
                if kind == "tool_result":
                    data = self.history.for_call("source_tool_result", index)[0]["data"]
                    entry["tool"] = data["name"]
                    passage = data["result"].get("passage") or data["result"].get("material_passage")
                    if passage:
                        entry.update(
                            {
                                key: passage[key]
                                for key in ("read_id", "source_id", "material_id", "start", "end")
                                if key in passage
                            }
                        )
                rows.append(entry)
            following = offset + len(rows)
            return dict(
                entries=rows,
                total_entries=len(self.history.entries),
                next_offset=following if following < len(self.history.entries) else None,
            )
        if name == "read_work_history":
            if args["kind"] == "tool_result":
                saved = self.history.for_call("source_tool_result", args["index"])
                if not saved:
                    return dict(error="unknown_work_history_entry")
                text = json.dumps(saved[0]["data"], ensure_ascii=False, separators=(",", ":"))
            elif args["kind"] == "conversation":
                refs = self.history.references["source_context_segment"]
                if args["index"] >= len(refs):
                    return dict(error="unknown_work_history_entry")
                ref = refs[args["index"]]
                blob = BlobRef.model_validate(self.journal.read(ref)["data"]["conversation_blob"])
                text = self.journal.read_blob(ref, blob).decode()
            else:
                return dict(error="unknown_work_history_kind")
            return dict(kind=args["kind"], index=args["index"], **text_page(text, args["start"], args["end"]))
        if name == "read_research_result":
            source = self.corpus.supplemental_reports.get(args["result_id"])
            if source is None:
                return dict(error="unknown_research_result")
            page = text_page(source.text, args["start"], args["end"])
            if "error" in page:
                return page
            return dict(
                material_passage=dict(
                    read_id="R" + read_id[1:],
                    material_id=args["result_id"],
                    title=source.title,
                    text=page["text"],
                    start=page["start"],
                    end=page["end"],
                ),
                next_start=page["next_start"],
                length=page["length"],
                kind="research_synthesis_not_source_evidence",
                source_mapping=dict(
                    tool="list_research_sources", result_id=args["result_id"], offset=0, version=None
                ),
            )
        return self.corpus.execute(name, args, read_id=read_id, **options)

    def restore(self, admissions):
        self.corpus._materials.runtime_admissions[:] = admissions
        for admission in admissions:
            self.corpus.admit(admission)
