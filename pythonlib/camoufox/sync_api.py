import json as _json
import urllib.request
from typing import Any, Dict, List, Optional, Union, overload
from urllib.parse import urlparse

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Playwright,
    PlaywrightContextManager,
)
from typing_extensions import Literal

from camoufox.virtdisplay import VirtualDisplay

from .exceptions import InvalidProxy
from .fingerprints import generate_context_fingerprint
from .utils import launch_options, sync_attach_vd


def _extract_launch_config(from_options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Reconstruct CAMOU_CONFIG from launch options when possible.
    """
    if not from_options:
        return {}
    env = from_options.get("env")
    if not isinstance(env, dict):
        return {}

    chunk_items = [
        (key, value)
        for key, value in env.items()
        if key.startswith("CAMOU_CONFIG_")
    ]
    chunks: List[str] = []
    for key, value in sorted(
        chunk_items,
        key=lambda item: int(item[0].rsplit("_", 1)[-1]),
    ):
        if not isinstance(value, str):
            continue
        chunks.append(value)

    if not chunks:
        return {}

    try:
        payload = _json.loads("".join(chunks))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _extract_context_defaults(
    launch_kwargs: Optional[Dict[str, Any]] = None,
    from_options: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Extract the browser-level defaults that should flow into browser.new_context/new_page.
    """
    launch_kwargs = launch_kwargs or {}
    config: Dict[str, Any] = {}

    if isinstance(launch_kwargs.get("config"), dict):
        config.update(launch_kwargs["config"])
    else:
        config.update(_extract_launch_config(from_options))

    defaults: Dict[str, Any] = {}
    os_value = launch_kwargs.get("os")
    if isinstance(os_value, str):
        defaults["os"] = os_value

    ff_version = launch_kwargs.get("ff_version")
    if ff_version is not None:
        defaults["ff_version"] = str(ff_version)

    webrtc_ipv4 = config.get("webrtc:ipv4")
    if isinstance(webrtc_ipv4, str) and webrtc_ipv4:
        defaults["webrtc_ip"] = webrtc_ipv4

    webrtc_ipv6 = config.get("webrtc:ipv6")
    if isinstance(webrtc_ipv6, str) and webrtc_ipv6:
        defaults["webrtc_ipv6"] = webrtc_ipv6

    timezone = config.get("timezone")
    if isinstance(timezone, str) and timezone:
        defaults["timezone_id"] = timezone

    proxy = launch_kwargs.get("proxy")
    if proxy is None and from_options:
        proxy = from_options.get("proxy")
    if isinstance(proxy, dict) and proxy.get("server"):
        defaults["_launch_proxy"] = proxy

    return defaults


def _split_context_kwargs(context_kwargs: Dict[str, Any]) -> tuple[Dict[str, Any], Dict[str, Any]]:
    """
    Split Camoufox-specific context args from Playwright context kwargs.
    """
    playwright_kwargs = dict(context_kwargs)
    camoufox_kwargs: Dict[str, Any] = {}
    for key in (
        "preset",
        "os",
        "ff_version",
        "webrtc_ip",
        "webrtc_ipv6",
        "proxy",
        "geolocation",
    ):
        if key in playwright_kwargs:
            camoufox_kwargs[key] = playwright_kwargs.pop(key)
    return camoufox_kwargs, playwright_kwargs


def _safe_close_context(context: BrowserContext) -> None:
    try:
        context.close()
    except Exception:
        pass


def _unwrap_browser(browser: Any) -> Browser:
    raw_browser = getattr(browser, "_camoufox_raw_browser", None)
    return raw_browser if raw_browser is not None else browser


def _create_context(
    browser: Browser,
    *,
    preset: Optional[Dict[str, Any]] = None,
    os: Optional[str] = None,
    ff_version: Optional[str] = None,
    webrtc_ip: Optional[str] = None,
    webrtc_ipv6: Optional[str] = None,
    proxy: Optional[Dict[str, str]] = None,
    geolocation: Optional[Dict[str, float]] = None,
    **context_kwargs: Any,
) -> BrowserContext:
    """
    Internal implementation for creating a fingerprinted browser context.
    """
    # Auto-derive WebRTC IP and timezone from proxy's exit IP when not explicitly provided
    if proxy and (not (webrtc_ip or webrtc_ipv6) or "timezone_id" not in context_kwargs):
        geo = _resolve_proxy_geo(proxy)
        if not (webrtc_ip or webrtc_ipv6):
            webrtc_ip = geo["ip"]
        if "timezone_id" not in context_kwargs and geo["timezone"]:
            context_kwargs["timezone_id"] = geo["timezone"]

    fp = generate_context_fingerprint(
        preset=preset,
        os=os,
        ff_version=ff_version,
        webrtc_ip=webrtc_ip,
        webrtc_ipv6=webrtc_ipv6,
    )

    # Merge generated context options with user overrides (user wins)
    opts: Dict[str, Any] = {**fp['context_options'], **context_kwargs}
    if proxy:
        opts['proxy'] = proxy
    if geolocation:
        opts['geolocation'] = geolocation
        opts.setdefault('permissions', ['geolocation'])

    context = browser.new_context(**opts)
    context.add_init_script(fp['init_script'])
    return context


def _create_context_with_defaults(
    browser: Browser,
    context_defaults: Dict[str, Any],
    **context_kwargs: Any,
) -> BrowserContext:
    """
    Create a new context while inheriting browser-level defaults from Camoufox().
    """
    camoufox_kwargs, playwright_kwargs = _split_context_kwargs(context_kwargs)

    if "os" not in camoufox_kwargs and isinstance(context_defaults.get("os"), str):
        camoufox_kwargs["os"] = context_defaults["os"]
    if "ff_version" not in camoufox_kwargs and context_defaults.get("ff_version") is not None:
        camoufox_kwargs["ff_version"] = context_defaults["ff_version"]

    explicit_proxy = camoufox_kwargs.get("proxy")
    if explicit_proxy is None:
        if (
            "webrtc_ip" not in camoufox_kwargs
            and isinstance(context_defaults.get("webrtc_ip"), str)
        ):
            camoufox_kwargs["webrtc_ip"] = context_defaults["webrtc_ip"]
        if (
            "webrtc_ipv6" not in camoufox_kwargs
            and isinstance(context_defaults.get("webrtc_ipv6"), str)
        ):
            camoufox_kwargs["webrtc_ipv6"] = context_defaults["webrtc_ipv6"]
        if (
            "timezone_id" not in playwright_kwargs
            and isinstance(context_defaults.get("timezone_id"), str)
        ):
            playwright_kwargs["timezone_id"] = context_defaults["timezone_id"]

        launch_proxy = context_defaults.get("_launch_proxy")
        if isinstance(launch_proxy, dict) and launch_proxy.get("server"):
            needs_webrtc = not (
                camoufox_kwargs.get("webrtc_ip") or camoufox_kwargs.get("webrtc_ipv6")
            )
            needs_timezone = "timezone_id" not in playwright_kwargs
            if needs_webrtc or needs_timezone:
                geo = _resolve_proxy_geo(launch_proxy)
                if needs_webrtc and geo.get("ip"):
                    camoufox_kwargs["webrtc_ip"] = geo["ip"]
                if needs_timezone and geo.get("timezone"):
                    playwright_kwargs["timezone_id"] = geo["timezone"]

    return _create_context(
        browser,
        **camoufox_kwargs,
        **playwright_kwargs,
    )


class _CamoufoxBrowserProxy:
    """
    Browser proxy that keeps browser.new_page/new_context on the fingerprint-safe path.
    """

    def __init__(self, browser: Browser, context_defaults: Optional[Dict[str, Any]] = None):
        self._camoufox_raw_browser = browser
        self._camoufox_context_defaults = context_defaults or {}

    def __getattr__(self, name: str) -> Any:
        return getattr(self._camoufox_raw_browser, name)

    def new_context(self, **context_kwargs: Any) -> BrowserContext:
        return _create_context_with_defaults(
            self._camoufox_raw_browser,
            self._camoufox_context_defaults,
            **context_kwargs,
        )

    def new_page(self, **context_kwargs: Any) -> Any:
        context = self.new_context(**context_kwargs)
        page = context.new_page()
        page.on("close", lambda *_args: _safe_close_context(context))
        return page


class Camoufox(PlaywrightContextManager):
    """
    Wrapper around playwright.sync_api.PlaywrightContextManager that automatically
    launches a browser and closes it when the context manager is exited.
    """

    def __init__(self, **launch_options):
        super().__init__()
        self.launch_options = launch_options
        self.browser: Optional[Union[Browser, BrowserContext]] = None

    def __enter__(self) -> Union[Browser, BrowserContext]:
        super().__enter__()
        try:
            self.browser = NewBrowser(self._playwright, **self.launch_options)
        except InvalidProxy as e:
            super().__exit__(InvalidProxy, e, None)
            raise
        return self.browser

    def __exit__(self, *args: Any):
        if self.browser:
            self.browser.close()
        super().__exit__(*args)


@overload
def NewBrowser(
    playwright: Playwright,
    *,
    from_options: Optional[Dict[str, Any]] = None,
    persistent_context: Literal[False] = False,
    **kwargs,
) -> Browser: ...


@overload
def NewBrowser(
    playwright: Playwright,
    *,
    from_options: Optional[Dict[str, Any]] = None,
    persistent_context: Literal[True],
    **kwargs,
) -> BrowserContext: ...


def NewBrowser(
    playwright: Playwright,
    *,
    headless: Optional[Union[bool, Literal['virtual']]] = None,
    from_options: Optional[Dict[str, Any]] = None,
    persistent_context: bool = False,
    debug: Optional[bool] = None,
    **kwargs,
) -> Union[Browser, BrowserContext]:
    """
    Launches a new browser instance for Camoufox given a set of launch options.

    Parameters:
        from_options (Dict[str, Any]):
            A set of launch options generated by `launch_options()` to use
        persistent_context (bool):
            Whether to use a persistent context.
        **kwargs:
            All other keyword arugments passed to `launch_options()`.
    """
    if headless == 'virtual':
        virtual_display = VirtualDisplay(debug=debug)
        kwargs['virtual_display'] = virtual_display.get()
        headless = False
    else:
        virtual_display = None

    if not from_options:
        from_options = launch_options(headless=headless, debug=debug, **kwargs)

    # Persistent context
    if persistent_context:
        context = playwright.firefox.launch_persistent_context(**from_options)
        return sync_attach_vd(context, virtual_display)

    # Browser
    browser = playwright.firefox.launch(**from_options)
    browser = sync_attach_vd(browser, virtual_display)
    return _CamoufoxBrowserProxy(
        browser,
        _extract_context_defaults(kwargs, from_options),
    )


def _proxy_url_with_creds(proxy: Dict[str, str]) -> str:
    """Builds a proxy URL string with embedded credentials."""
    parsed = urlparse(proxy.get("server", ""))
    user = proxy.get("username", "")
    pwd = proxy.get("password", "")
    if user and pwd:
        return f"{parsed.scheme}://{user}:{pwd}@{parsed.netloc}"
    return proxy.get("server", "")


def _resolve_proxy_geo(proxy: Dict[str, str]) -> Dict[str, Optional[str]]:
    """Queries ip-api.com through the proxy for the exit IP and timezone."""
    proxy_url = _proxy_url_with_creds(proxy)
    handler = urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
    opener = urllib.request.build_opener(handler)
    try:
        with opener.open("http://ip-api.com/json?fields=query,timezone", timeout=10) as resp:
            data = _json.loads(resp.read())
            return {"ip": data.get("query") or None, "timezone": data.get("timezone") or None}
    except Exception:
        return {"ip": None, "timezone": None}


def NewContext(
    browser: Browser,
    *,
    preset: Optional[Dict[str, Any]] = None,
    os: Optional[str] = None,
    ff_version: Optional[str] = None,
    webrtc_ip: Optional[str] = None,
    webrtc_ipv6: Optional[str] = None,
    proxy: Optional[Dict[str, str]] = None,
    geolocation: Optional[Dict[str, float]] = None,
    **context_kwargs: Any,
) -> BrowserContext:
    """
    Creates a new browser context with a unique fingerprint identity.

    Each context gets its own real fingerprint preset
    with unique seeds for audio, canvas, and font spacing noise. All values are applied
    via addInitScript so they self-destruct before page scripts can detect them.

    Parameters:
        browser: A Browser instance from NewBrowser or Camoufox.
        preset: A specific fingerprint preset dict to use. If None, generates one.
        os: Target OS for preset selection ("windows", "macos", "linux").
            Defaults to Windows when omitted.
        ff_version: Firefox version string for UA patching.
        webrtc_ip: Legacy WebRTC IP override. Supports IPv4 or IPv6.
        webrtc_ipv6: Optional explicit IPv6 override for WebRTC ICE candidates.
        proxy: Per-context proxy (Playwright format: {"server": "...", "username": "...", "password": "..."}).
        geolocation: Per-context geolocation ({"latitude": float, "longitude": float}).
        **context_kwargs: Additional Playwright new_context() options.
    """
    return _create_context(
        _unwrap_browser(browser),
        preset=preset,
        os=os,
        ff_version=ff_version,
        webrtc_ip=webrtc_ip,
        webrtc_ipv6=webrtc_ipv6,
        proxy=proxy,
        geolocation=geolocation,
        **context_kwargs,
    )
