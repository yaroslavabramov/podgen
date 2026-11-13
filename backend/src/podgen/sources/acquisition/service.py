"""Acquire saved versions of research URLs without rerunning successful fetches."""

from podgen.sources.extraction.extract import DocumentExtractors

from .browser import BrowserSourceFetcher
from .contracts import AcquisitionRequest, AcquisitionResult
from .fallbacks import jina_request, mediawiki_url, promoted_pdf
from .http import HttpSourceFetcher
from .network import _check_ssrf
from .profile import load_profile
from .stages import acquire_document, saved_extracted_text
from .store import AcquisitionStore


async def acquire_source(repo, project_id, request, *, fetcher=None, extractors=None, browser_fetcher=None):
    request = AcquisitionRequest.model_validate(request)
    store = AcquisitionStore(repo, project_id, request.operation_id)
    with store.lock():
        profile = load_profile(repo, project_id, request.profile_ref)
        raw = request.model_dump(mode="json")
        store.put("request", raw)
        saved = store.get("result")
        if saved:
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
        fetcher, extractors = fetcher or HttpSourceFetcher(), extractors or DocumentExtractors()
        sources, diagnostics = [], []

        async def obtain(role, url, *, headers=None, transport=None):
            source = await acquire_document(
                store, request, profile, role, url, transport or fetcher, extractors, headers=headers
            )
            if source:
                sources.append(source)
            else:
                diagnostics.extend(store.get(role + ":outcome")["diagnostics"])
            return source

        await obtain("original", request.url)
        if profile.prefer_mediawiki:
            target = mediawiki_url(request.url)
            if target:
                await obtain("mediawiki", target)
        if not any(source.status == "available" for source in sources) and profile.enable_jina:
            jina = jina_request(request.url, profile)
            if jina:
                source = await obtain("jina", jina[0], headers=jina[1])
                if source and source.representation_refs:
                    target = promoted_pdf(saved_extracted_text(store, source), request.url)
                    if target:
                        await obtain("promoted_pdf", target)
            else:
                diagnostics.append("jina_credential_missing")
        if not any(source.status == "available" for source in sources) and profile.enable_browser:
            await obtain("browser", request.url, transport=browser_fetcher or BrowserSourceFetcher())
        status = (
            "available"
            if any(source.status == "available" for source in sources)
            else (
                "unconfirmed"
                if any(source.status == "unconfirmed" for source in sources)
                else "unavailable"
                if sources
                else "failed"
            )
        )
        result = AcquisitionResult(
            acquisition_record_ref=store.ref("request"),
            sources=sources,
            status=status,
            diagnostics=diagnostics,
        )
        store.put("result", result.model_dump(mode="json"))
        return result
