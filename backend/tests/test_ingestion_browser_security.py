from types import SimpleNamespace

import pytest

from podgen.sources.acquisition.browser_guard import guarded_page_goto, public_browser_launch_args


@pytest.mark.asyncio
async def test_rendered_browser_blocks_unsafe_subrequests(monkeypatch: pytest.MonkeyPatch) -> None:
    class Route:
        aborted = False

        async def abort(self, _reason):
            self.aborted = True

        async def continue_(self):
            raise AssertionError("unsafe request must not continue")

    route = Route()

    class Page:
        url = "https://public.example/article"

        async def route(self, _pattern, handler):
            self.handler = handler

        async def goto(self, *_args, **_kwargs):
            await self.handler(route, SimpleNamespace(url="http://169.254.169.254/latest/meta-data"))
            return None

    def validate(url: str) -> None:
        if "169.254.169.254" in url:
            raise ValueError("SSRF blocked")

    monkeypatch.setattr("podgen.sources.acquisition.browser_guard.validate_public_browser_url", validate)

    with pytest.raises(ValueError, match="unsafe request"):
        await guarded_page_goto(
            Page(), "https://public.example/article", wait_until="networkidle", timeout=1000
        )

    assert route.aborted is True


@pytest.mark.asyncio
async def test_rendered_browser_ignores_public_cross_origin_assets_without_failing_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Route:
        aborted = False

        async def abort(self, _reason):
            self.aborted = True

        async def continue_(self):
            raise AssertionError("cross-origin asset must not continue")

    route = Route()

    class Page:
        url = "https://public.example/article"

        async def route(self, _pattern, handler):
            self.handler = handler

        async def goto(self, *_args, **_kwargs):
            await self.handler(route, SimpleNamespace(url="https://cdn.example/article.css"))
            return "document-response"

    monkeypatch.setattr(
        "podgen.sources.acquisition.browser_guard.validate_public_browser_url", lambda _url: None
    )

    response = await guarded_page_goto(
        Page(),
        "https://public.example/article",
        wait_until="networkidle",
        timeout=1000,
    )

    assert response == "document-response"
    assert route.aborted is True


@pytest.mark.asyncio
async def test_rendered_browser_validates_final_redirect_url(monkeypatch: pytest.MonkeyPatch) -> None:
    class Page:
        url = "https://public.example/article"

        async def route(self, _pattern, handler):
            self.handler = handler

        async def goto(self, *_args, **_kwargs):
            self.url = "http://127.0.0.1/admin"
            return None

    def validate(url: str) -> None:
        if "127.0.0.1" in url:
            raise ValueError("SSRF blocked redirect")

    monkeypatch.setattr("podgen.sources.acquisition.browser_guard.validate_public_browser_url", validate)

    with pytest.raises(ValueError, match="blocked redirect"):
        await guarded_page_goto(
            Page(), "https://public.example/article", wait_until="networkidle", timeout=1000
        )


@pytest.mark.asyncio
async def test_rendered_browser_blocks_unsafe_websocket(monkeypatch: pytest.MonkeyPatch) -> None:
    class WebSocket:
        url = "ws://127.0.0.1/admin"
        closed = False

        async def close(self, **_kwargs):
            self.closed = True

        def connect_to_server(self):  # pragma: no cover - defensive
            raise AssertionError("unsafe websocket must not connect")

    websocket = WebSocket()

    class Page:
        url = "https://public.example/article"

        async def route(self, _pattern, _handler):
            return None

        async def route_web_socket(self, _pattern, handler):
            self.websocket_handler = handler

        async def goto(self, *_args, **_kwargs):
            await self.websocket_handler(websocket)
            return None

    def validate(url: str) -> None:
        if "127.0.0.1" in url:
            raise ValueError("SSRF blocked websocket")

    monkeypatch.setattr("podgen.sources.acquisition.browser_guard.validate_public_browser_url", validate)

    with pytest.raises(ValueError, match="unsafe request"):
        await guarded_page_goto(
            Page(), "https://public.example/article", wait_until="networkidle", timeout=1000
        )

    assert websocket.closed is True


@pytest.mark.asyncio
async def test_http_transport_connects_to_validated_ip_not_rebound_hostname(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from podgen.sources.acquisition import network as web_module

    connected_hosts: list[str] = []

    class Backend:
        async def connect_tcp(self, host, _port, **_kwargs):
            connected_hosts.append(host)
            return SimpleNamespace()

        async def sleep(self, _seconds):
            return None

    monkeypatch.setattr(web_module, "_validated_public_ip_strings", lambda _host: ["93.184.216.34"])
    backend = web_module._SSRFGuardedNetworkBackend(Backend())  # type: ignore[arg-type]

    await backend.connect_tcp("rebind.example", 443)

    assert connected_hosts == ["93.184.216.34"]


def test_rendered_browser_launch_pins_dns_and_blocks_other_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "podgen.sources.acquisition.browser_guard._validated_public_ip_strings",
        lambda _host: ["93.184.216.34"],
    )

    args = public_browser_launch_args("https://public.example/article")

    assert any("MAP public.example 93.184.216.34" in arg for arg in args)
    assert any("MAP * ~NOTFOUND" in arg for arg in args)
    assert "--disable-quic" in args
    assert "--force-webrtc-ip-handling-policy=disable_non_proxied_udp" in args


@pytest.mark.asyncio
async def test_rendered_browser_disables_peer_connections_before_navigation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scripts: list[str] = []

    class Context:
        async def add_init_script(self, *, script):
            scripts.append(script)

        async def route(self, _pattern, _handler):
            return None

    class Page:
        url = "https://public.example/article"
        context = Context()

        async def goto(self, *_args, **_kwargs):
            assert scripts
            return None

    monkeypatch.setattr(
        "podgen.sources.acquisition.browser_guard.validate_public_browser_url",
        lambda _url: None,
    )

    await guarded_page_goto(
        Page(),
        "https://public.example/article",
        wait_until="networkidle",
        timeout=1000,
    )

    assert "RTCPeerConnection" in scripts[0]
    assert "RTCIceTransport" in scripts[0]
