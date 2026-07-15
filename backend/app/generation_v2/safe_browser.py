from __future__ import annotations

from typing import Any
from urllib.parse import urlparse, urlunparse

from app.ingestion.web import _check_ssrf, _validated_public_ip_strings


_LOCAL_BROWSER_SCHEMES = {"about", "blob", "data"}
_NETWORK_BROWSER_SCHEMES = {"http", "https", "ws", "wss"}
_DISABLE_DIRECT_BROWSER_TRANSPORTS_SCRIPT = """
(() => {
  const blocked = [
    "RTCPeerConnection",
    "webkitRTCPeerConnection",
    "RTCIceTransport",
    "RTCIceGatherer",
    "RTCDtlsTransport",
    "RTCSctpTransport"
  ];
  for (const name of blocked) {
    try {
      Object.defineProperty(globalThis, name, {
        value: undefined,
        writable: false,
        configurable: false
      });
    } catch (_) {}
  }
})();
"""


def validate_public_browser_url(url: str) -> None:
    parsed = urlparse(str(url or ""))
    scheme = parsed.scheme.lower()
    if scheme in _LOCAL_BROWSER_SCHEMES:
        return
    if scheme not in _NETWORK_BROWSER_SCHEMES or not parsed.hostname:
        raise ValueError(f"Browser request scheme is not allowed: {scheme or 'missing'}")
    check_url = url
    if scheme in {"ws", "wss"}:
        check_url = urlunparse(("https" if scheme == "wss" else "http", parsed.netloc, parsed.path, parsed.params, parsed.query, parsed.fragment))
    _check_ssrf(check_url)


def public_browser_launch_args(url: str) -> list[str]:
    """Pin Chromium DNS to the public address that passed validation."""
    parsed = urlparse(str(url or ""))
    hostname = parsed.hostname
    if parsed.scheme.lower() not in {"http", "https"} or not hostname:
        raise ValueError("Rendered browser requires a public HTTP(S) URL")
    address = _validated_public_ip_strings(hostname)[0]
    mapped_address = f"[{address}]" if ":" in address else address
    return [
        "--disable-features=AsyncDns",
        "--disable-quic",
        "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
        f"--host-resolver-rules=MAP {hostname} {mapped_address}, MAP * ~NOTFOUND",
    ]


async def guarded_page_goto(page: Any, url: str, *, wait_until: str, timeout: int) -> Any:
    """Navigate while rejecting private/reserved redirects and subrequests."""
    validate_public_browser_url(url)
    violations: list[str] = []
    allowed_hostname = (urlparse(url).hostname or "").casefold()

    def request_is_same_origin(request_url: str) -> bool:
        validate_public_browser_url(request_url)
        parsed = urlparse(request_url)
        return parsed.scheme.lower() not in _NETWORK_BROWSER_SCHEMES or (
            parsed.hostname or ""
        ).casefold() == allowed_hostname

    async def guard_request(route: Any, request: Any) -> None:
        try:
            same_origin = request_is_same_origin(str(request.url))
        except ValueError as exc:
            violations.append(str(exc))
            await route.abort("blockedbyclient")
            return
        # Public cross-origin assets are not an SSRF violation, but Chromium is
        # intentionally pinned to the validated document host. Drop those
        # requests without turning an otherwise readable page into a failed
        # source merely because it references a CDN, analytics, or an image.
        if not same_origin:
            await route.abort("blockedbyclient")
            return
        await route.continue_()

    router = getattr(page, "context", None) or page
    if hasattr(router, "add_init_script"):
        await router.add_init_script(script=_DISABLE_DIRECT_BROWSER_TRANSPORTS_SCRIPT)
    await router.route("**/*", guard_request)
    if hasattr(router, "route_web_socket"):
        async def guard_websocket(websocket: Any) -> None:
            try:
                same_origin = request_is_same_origin(str(websocket.url))
            except ValueError as exc:
                violations.append(str(exc))
                await websocket.close(code=1008, reason="Blocked unsafe destination")
                return
            if not same_origin:
                await websocket.close(code=1008, reason="Blocked cross-origin destination")
                return
            websocket.connect_to_server()

        await router.route_web_socket("**/*", guard_websocket)
    try:
        response = await page.goto(url, wait_until=wait_until, timeout=timeout)
    except Exception:
        if violations:
            raise ValueError(f"Rendered snapshot blocked an unsafe request: {violations[0]}") from None
        raise
    if violations:
        raise ValueError(f"Rendered snapshot blocked an unsafe request: {violations[0]}")
    final_url = str(page.url)
    if not request_is_same_origin(final_url):
        raise ValueError(
            f"Rendered browser cross-origin redirect is blocked: "
            f"{urlparse(final_url).hostname or 'missing'}"
        )
    return response


__all__ = ["guarded_page_goto", "public_browser_launch_args", "validate_public_browser_url"]
