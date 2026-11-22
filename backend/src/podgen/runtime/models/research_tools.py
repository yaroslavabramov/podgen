"""Operational research navigation, explicit checkpoints and durable material handoffs."""

from uuid import uuid5

from podgen.domain.contracts.common import BlobRef
from podgen.domain.errors import Conflict, ContractError

from . import research_history_view, search_argument_errors

PAGE = 9600
CATALOG_PAGE = 6


def text_page(text, start, end):
    length = len(text)
    stop = min(length, start + PAGE) if end is None else end
    if not 0 <= start < stop <= length or stop - start > PAGE:
        return dict(error="invalid_history_range", length=length)
    return dict(
        text=text[start:stop],
        start=start,
        end=stop,
        length=length,
        next_start=stop if stop < length else None,
    )


class ResearchTools:
    def __init__(
        self,
        journal,
        root,
        corpus,
        history,
        guard,
        *,
        history_view=None,
        anchored=False,
        search_argument_policy=None,
        fact_navigation=False,
    ):
        self.journal, self.root, self.corpus = journal, root, corpus
        self.history, self.guard = history, guard
        if history_view not in (None, research_history_view.VERSION):
            raise ContractError("unsupported_research_history_view")
        self.history_view = history_view
        self.anchored = anchored
        from podgen.research.materials.orders import policy

        self.research_request_policy = policy(journal.manifest(root["run_id"])) if anchored else None
        if search_argument_policy not in (None, search_argument_errors.VERSION):
            raise ContractError("unsupported_search_argument_policy")
        self.search_argument_policy = search_argument_policy
        self.fact_navigation = fact_navigation
        self.last_search_delivery = None

    @property
    def last_search_diagnostics(self):
        return self.corpus.last_search_diagnostics

    def model_result(self, name, result):
        if name == "request_research":
            from podgen.research.materials.report_navigation import model_result

            return model_result(result)
        if self.fact_navigation and name == "request_source":
            from .fact_navigation import source_result

            return source_result(result)
        return result

    def execute(self, name, args, *, read_id, **options):
        self.last_search_delivery = None
        if getattr(self.corpus, "fact_input_mode", None) is not None:
            from podgen.writing.research_materials.fact_source_mode import ALLOWED_TOOLS

            if name not in ALLOWED_TOOLS:
                return dict(error="unknown_source_tool")
        index = int(read_id[1:]) - 1
        if name in {"request_source", "request_research"}:
            resolver = self.corpus.material_resolver
            if resolver is None:
                return dict(error="source_acquisition_not_configured")
            if name == "request_research" and self.anchored:
                from .research_binding import bind

                try:
                    args = bind(self.journal, self.root, args, request_policy=self.research_request_policy)
                except ContractError as error:
                    return dict(error=str(error))
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
                data = saved[0]["data"]
                text = research_history_view.encoded(
                    dict(
                        research_history_view.tool_result(data),
                        result=self.model_result(data["name"], data["result"]),
                    )
                    if self.history_view
                    else data
                )
            elif args["kind"] == "conversation":
                refs = self.history.references["source_context_segment"]
                if args["index"] >= len(refs):
                    return dict(error="unknown_work_history_entry")
                ref = refs[args["index"]]
                segment = self.journal.read(ref)["data"]
                if self.history_view:
                    if segment.get("history_view") != self.history_view:
                        raise ContractError("research_history_view_missing")
                    blob = BlobRef.model_validate(segment["history_blob"])
                    text = self.journal.text_blob(ref, blob)
                else:
                    blob = BlobRef.model_validate(segment["conversation_blob"])
                    text = self.journal.read_blob(ref, blob).decode()
            else:
                return dict(error="unknown_work_history_kind")
            from .fact_navigation import history_page

            page = (history_page if self.fact_navigation else text_page)(text, args["start"], args["end"])
            return dict(kind=args["kind"], index=args["index"], **page)
        if name == "read_research_result":
            from podgen.writing.research_materials.report_reading import read

            return read(self.corpus, args, read_id, text_page)
        try:
            result = self.corpus.execute(name, args, read_id=read_id, **options)
            if name == "search_sources" and self.fact_navigation:
                from .fact_navigation import annotate

                result, self.last_search_delivery = annotate(result, self.corpus, self.history)
            return result
        except Conflict as error:
            if (
                self.search_argument_policy != search_argument_errors.VERSION
                or name != "search_sources"
                or not args.get("result_version")
                or error.code != "source_search_result_version_mismatch"
            ):
                raise
            self.guard()
            return search_argument_errors.result(args)

    def restore(self, admissions):
        self.corpus._materials.runtime_admissions[:] = admissions
        prepare = getattr(self.guard, "prepare_inputs", None)
        if prepare is not None:
            prepare()
        else:
            self.journal.execution.prepare(
                self.root["run_id"], evaluation=[admission.reference for admission in admissions]
            )
        for admission in admissions:
            bind_text = getattr(admission.text, "bind_execution", None)
            if bind_text is not None:
                bind_text(self.journal.execution, self.root["run_id"])
            self.corpus.admit(admission)
