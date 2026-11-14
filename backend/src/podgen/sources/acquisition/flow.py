"""One transition owner for fallback scheduling and terminal publication."""

from dataclasses import dataclass

from .fallbacks import jina_request, mediawiki_url, promoted_pdf


@dataclass(frozen=True)
class FetchStep:
    role: str
    url: str
    headers: dict | None = None


class AcquisitionFlow:
    def __init__(self, request, profile, request_ref):
        self.request, self.profile, self.request_ref = request, profile, request_ref
        self.outcomes, self.texts = {}, {}
        self.mediawiki = mediawiki_url(request.url) if profile.prefer_mediawiki else None
        self.jina = jina_request(request.url, profile) if profile.enable_jina else None

    def decide(self, outcomes=None, texts=None):
        outcomes = self.outcomes if outcomes is None else outcomes
        texts = self.texts if texts is None else texts
        if "original" not in outcomes:
            return FetchStep("original", self.request.url)
        if self.mediawiki and "mediawiki" not in outcomes:
            return FetchStep("mediawiki", self.mediawiki)
        sources = [value["source"] for value in outcomes.values() if value["source"] is not None]
        prior_available = any(
            value["status"] == "available" for value in sources if value["role"] in ("original", "mediawiki")
        )
        diagnostics = [
            item for value in outcomes.values() if value["source"] is None for item in value["diagnostics"]
        ]
        if not prior_available and self.profile.enable_jina:
            if self.jina is None:
                diagnostics.append("jina_credential_missing")
            else:
                if "jina" not in outcomes:
                    return FetchStep("jina", self.jina[0], self.jina[1])
                source = outcomes["jina"]["source"]
                if source and source["representation_refs"]:
                    if "jina" not in texts:
                        raise RuntimeError("acquisition_jina_text_not_loaded")
                    target = promoted_pdf(texts["jina"], self.request.url)
                    if target and "promoted_pdf" not in outcomes:
                        return FetchStep("promoted_pdf", target)
        available = any(value["status"] == "available" for value in sources)
        if not available and self.profile.enable_browser and "browser" not in outcomes:
            return FetchStep("browser", self.request.url)
        return dict(
            contract="source-acquisition-result/1",
            acquisition_record_ref=self.request_ref.model_dump(mode="json"),
            sources=sources,
            status="available"
            if available
            else "unconfirmed"
            if any(v["status"] == "unconfirmed" for v in sources)
            else "unavailable"
            if sources
            else "failed",
            diagnostics=diagnostics,
        )

    def candidate(self, role, source, text):
        decision = self.decide(
            self.outcomes | {role: dict(source=source)},
            self.texts | ({role: text} if text is not None else {}),
        )
        return decision if isinstance(decision, dict) else None

    def accept(self, role, outcome, text=None):
        self.outcomes[role] = outcome
        if text is not None:
            self.texts[role] = text
