"""Acquire saved versions of research URLs without rerunning successful fetches."""

from podgen.sources.extraction.extract import DocumentExtractors

from .browser import BrowserSourceFetcher
from .contracts import AcquisitionRequest, AcquisitionResult
from .flow import AcquisitionFlow, FetchStep
from .http import HttpSourceFetcher
from .network import _check_ssrf
from .stages import acquire_document, saved_extracted_text
from .store import AcquisitionStore


async def acquire_source(repo, project_id, request, *, fetcher=None, extractors=None, browser_fetcher=None):
    request = AcquisitionRequest.model_validate(request)
    store = AcquisitionStore(repo, project_id, request.operation_id)
    with store.lock():
        profile, saved = store.start(request)
        if saved is not None:
            return AcquisitionResult.model_validate(saved)
        try:
            _check_ssrf(request.url)
        except ValueError as error:
            result = AcquisitionResult(
                acquisition_record_ref=store.ref("request"),
                sources=[],
                status="failed",
                diagnostics=[type(error).__name__ + ":" + str(error)],
            )
            store.put("result", result.model_dump(mode="json"))
            return result
        flow = AcquisitionFlow(request, profile, store.ref("request"))
        store.completion = flow.candidate
        fetcher, extractors = fetcher or HttpSourceFetcher(), extractors or DocumentExtractors()
        while isinstance(step := flow.decide(), FetchStep):
            transport = (browser_fetcher or BrowserSourceFetcher()) if step.role == "browser" else fetcher
            source = await acquire_document(
                store, request, profile, step.role, step.url, transport, extractors, headers=step.headers
            )
            outcome = store.outcomes[step.role]
            text = store.texts.get(step.role)
            if step.role == "jina" and source and source.representation_refs and text is None:
                text = saved_extracted_text(store, source)
            flow.accept(step.role, outcome, text)
        result = AcquisitionResult.model_validate(flow.decide())
        if store.completed is None:
            store.put("result", result.model_dump(mode="json"))
        elif store.completed != result.model_dump(mode="json"):
            raise RuntimeError("acquisition_terminal_result_mismatch")
        return result
