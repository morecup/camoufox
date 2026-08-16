from __future__ import annotations

import argparse
import asyncio
import json
import sys
import urllib.parse
import urllib.request
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


REPO_ROOT = Path(__file__).resolve().parent.parent
LAB_ROOT = Path(__file__).resolve().parent
PYTHONLIB_ROOT = REPO_ROOT / "pythonlib"
if str(PYTHONLIB_ROOT) not in sys.path:
    sys.path.insert(0, str(PYTHONLIB_ROOT))
if str(LAB_ROOT) not in sys.path:
    sys.path.insert(0, str(LAB_ROOT))

from server import start_lab_server  # noqa: E402


INTERESTING_URL_MARKERS = (
    "127.0.0.1",
    "localhost",
    "challenges.cloudflare.com",
    "turnstile",
    "2captcha.com",
    "nopecha.com",
    "cf-chl",
)

REAL_OBSERVER_INIT_SCRIPT = r"""
(() => {
  const state = {
    startedAt: Date.now(),
    events: [],
    messages: [],
    iframes: [],
  };

  function limit(list, maxSize) {
    if (list.length > maxSize) {
      list.splice(0, list.length - maxSize);
    }
  }

  function summarize(value, depth = 0) {
    if (depth >= 2) {
      return typeof value;
    }
    if (value === null || value === undefined) {
      return value;
    }
    if (typeof value === "string") {
      return value.length > 220 ? `${value.slice(0, 220)}...` : value;
    }
    if (typeof value === "number" || typeof value === "boolean") {
      return value;
    }
    if (Array.isArray(value)) {
      return value.slice(0, 10).map((item) => summarize(item, depth + 1));
    }
    if (typeof value === "object") {
      const output = {};
      for (const key of Object.keys(value).slice(0, 12)) {
        output[key] = summarize(value[key], depth + 1);
      }
      return output;
    }
    return String(value);
  }

  function log(type, detail) {
    state.events.push({
      ts: new Date().toISOString(),
      elapsedMs: Date.now() - state.startedAt,
      type,
      detail: summarize(detail),
    });
    limit(state.events, 160);
  }

  function snapshotIframes() {
    return Array.from(document.querySelectorAll("iframe")).map((frame, index) => {
      let sameOriginAccessible = false;
      try {
        sameOriginAccessible = Boolean(frame.contentWindow && frame.contentWindow.location && frame.contentWindow.location.href);
      } catch (_error) {
        sameOriginAccessible = false;
      }
      return {
        index,
        id: frame.id || null,
        name: frame.name || null,
        title: frame.title || null,
        src: frame.getAttribute("src") || frame.src || null,
        sandbox: frame.getAttribute("sandbox") || null,
        width: frame.clientWidth,
        height: frame.clientHeight,
        sameOriginAccessible,
      };
    });
  }

  function getFingerprint() {
    const canvas = document.createElement("canvas");
    const gl = canvas.getContext("webgl") || canvas.getContext("experimental-webgl");
    const webgl = { vendor: null, renderer: null };
    if (gl) {
      const ext = gl.getExtension("WEBGL_debug_renderer_info");
      if (ext) {
        webgl.vendor = gl.getParameter(ext.UNMASKED_VENDOR_WEBGL);
        webgl.renderer = gl.getParameter(ext.UNMASKED_RENDERER_WEBGL);
      }
    }

    return {
      userAgent: navigator.userAgent,
      platform: navigator.platform,
      oscpu: navigator.oscpu || null,
      hardwareConcurrency: navigator.hardwareConcurrency || null,
      webdriver: navigator.webdriver,
      timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
      screen: {
        width: screen.width,
        height: screen.height,
        availWidth: screen.availWidth,
        availHeight: screen.availHeight,
      },
      viewport: {
        width: window.innerWidth,
        height: window.innerHeight,
        devicePixelRatio: window.devicePixelRatio,
      },
      webgl,
    };
  }

  window.__turnstileObserver = {
    snapshot(reason) {
      state.iframes = snapshotIframes();
      return {
        reason,
        sentAt: new Date().toISOString(),
        location: location.href,
        title: document.title,
        readyState: document.readyState,
        visibilityState: document.visibilityState,
        turnstilePresent: Boolean(window.turnstile),
        iframeCount: state.iframes.length,
        iframes: state.iframes,
        messages: state.messages.slice(-24),
        events: state.events.slice(-48),
        fingerprint: getFingerprint(),
      };
    },
  };

  window.addEventListener("message", (event) => {
    state.messages.push({
      ts: new Date().toISOString(),
      origin: event.origin,
      data: summarize(event.data),
    });
    limit(state.messages, 120);
  });

  window.addEventListener("error", (event) => {
    log("window.error", {
      message: event.message,
      filename: event.filename,
      lineno: event.lineno,
      colno: event.colno,
    });
  });

  window.addEventListener("unhandledrejection", (event) => {
    log("window.unhandledrejection", summarize(event.reason));
  });

  document.addEventListener("visibilitychange", () => {
    log("visibilitychange", { visibilityState: document.visibilityState });
  });

  const observer = new MutationObserver(() => {
    state.iframes = snapshotIframes();
    log("iframe.update", { count: state.iframes.length, urls: state.iframes.map((frame) => frame.src).filter(Boolean).slice(0, 6) });
  });

  if (document.documentElement) {
    observer.observe(document.documentElement, {
      childList: true,
      subtree: true,
      attributes: true,
      attributeFilter: ["src", "title", "sandbox"],
    });
  }

  const waitTurnstile = setInterval(() => {
    if (window.turnstile) {
      log("turnstile.present", { keys: Object.keys(window.turnstile || {}) });
      clearInterval(waitTurnstile);
    }
  }, 500);

  log("observer.ready", { href: location.href });
})();
"""


def _reconfigure_stdio() -> None:
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _slug(value: str) -> str:
    cleaned = []
    for char in value:
        if char.isalnum() or char in ("-", "_", "."):
            cleaned.append(char)
        else:
            cleaned.append("_")
    return "".join(cleaned).strip("._") or "item"


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _fetch_json(url: str) -> Dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": "turnstile-lab/1.0"})
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def _extract_binary_specs(values: Optional[List[str]]) -> List[tuple[str, Path]]:
    if values:
        specs: List[tuple[str, Path]] = []
        for item in values:
            if "=" in item:
                label, raw_path = item.split("=", 1)
            else:
                raw_path = item
                label = Path(raw_path).stem
            specs.append((_slug(label), Path(raw_path).expanduser().resolve()))
        return specs

    candidates: List[tuple[str, Path]] = []
    stable = REPO_ROOT / "camoufox-builds" / "146.0.1-beta.25-win" / "camoufox.exe"
    if stable.exists():
        candidates.append(("stable", stable.resolve()))

    backups = sorted((REPO_ROOT / "camoufox-builds").glob("146.0.1-beta.25-win.bak.*"))
    if backups:
        backup_binary = backups[-1] / "camoufox.exe"
        if backup_binary.exists():
            candidates.append(("backup", backup_binary.resolve()))

    return candidates


@dataclass
class BrowserCase:
    binary_label: str
    binary_path: Path
    launcher: str
    kind: str
    disable_coop: bool = False

    @property
    def case_label(self) -> str:
        return f"{self.binary_label}-{self.launcher}"


@dataclass
class EventCapture:
    console: List[Dict[str, Any]] = field(default_factory=list)
    page_errors: List[str] = field(default_factory=list)
    request_failures: List[Dict[str, Any]] = field(default_factory=list)
    responses: List[Dict[str, Any]] = field(default_factory=list)
    frame_navs: List[Dict[str, Any]] = field(default_factory=list)
    click_attempts: List[Dict[str, Any]] = field(default_factory=list)

    def _trim(self) -> None:
        for attr, limit in (("console", 120), ("page_errors", 40), ("request_failures", 80), ("responses", 160), ("frame_navs", 120), ("click_attempts", 60)):
            values = getattr(self, attr)
            if len(values) > limit:
                setattr(self, attr, values[-limit:])


def _launcher_presets(names: Iterable[str]) -> List[tuple[str, str, bool]]:
    presets = {
        "raw": ("raw", False),
        "raw-disable-coop": ("raw", True),
        "camoufox": ("camoufox", False),
        "camoufox-disable-coop": ("camoufox", True),
    }

    output: List[tuple[str, str, bool]] = []
    for name in names:
        if name not in presets:
            raise ValueError(f"Unsupported launcher: {name}")
        kind, disable_coop = presets[name]
        output.append((name, kind, disable_coop))
    return output


def _is_interesting_url(url: str) -> bool:
    return any(marker in url for marker in INTERESTING_URL_MARKERS)


def _summarize_console_arg(value: Any) -> Any:
    if isinstance(value, str) and len(value) > 240:
        return value[:240] + "..."
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)


async def _attach_capture(context: Any, page: Any, capture: EventCapture) -> None:
    async def on_console(message: Any) -> None:
        args_summary: List[Any] = []
        for arg in message.args[:6]:
            try:
                args_summary.append(_summarize_console_arg(await arg.json_value()))
            except Exception:
                args_summary.append(await arg.evaluate("v => String(v)"))
        capture.console.append(
            {
                "ts": _iso_now(),
                "type": message.type,
                "text": message.text,
                "location": message.location,
                "args": args_summary,
            }
        )
        capture._trim()

    def on_page_error(error: Exception) -> None:
        capture.page_errors.append(f"{type(error).__name__}: {error}")
        capture._trim()

    def on_request_failed(request: Any) -> None:
        url = request.url
        if not _is_interesting_url(url):
            return
        failure = request.failure
        capture.request_failures.append(
            {
                "ts": _iso_now(),
                "url": url,
                "method": request.method,
                "resourceType": request.resource_type,
                "failure": failure,
            }
        )
        capture._trim()

    def on_response(response: Any) -> None:
        url = response.url
        if not _is_interesting_url(url):
            return
        capture.responses.append(
            {
                "ts": _iso_now(),
                "url": url,
                "status": response.status,
                "ok": response.ok,
                "fromServiceWorker": getattr(response, "from_service_worker", False),
            }
        )
        capture._trim()

    def on_frame_navigated(frame: Any) -> None:
        url = frame.url or ""
        if not _is_interesting_url(url):
            return
        capture.frame_navs.append(
            {
                "ts": _iso_now(),
                "url": url,
                "name": frame.name,
                "parentUrl": frame.parent_frame.url if frame.parent_frame else None,
            }
        )
        capture._trim()

    page.on("console", lambda message: asyncio.create_task(on_console(message)))
    page.on("pageerror", on_page_error)
    page.on("framenavigated", on_frame_navigated)
    context.on("requestfailed", on_request_failed)
    context.on("response", on_response)


async def _safe_evaluate(page: Any, expression: str, arg: Any = None) -> Any:
    try:
        if arg is None:
            return await page.evaluate(expression)
        return await page.evaluate(expression, arg)
    except Exception as exc:
        return {"__evaluateError__": str(exc)}


async def _snapshot_local_page(page: Any, reason: str) -> Any:
    return await _safe_evaluate(
        page,
        "reason => (window.__tsLab && typeof window.__tsLab.snapshot === 'function' ? window.__tsLab.snapshot(reason) : null)",
        reason,
    )


async def _flush_local_page(page: Any, reason: str) -> Any:
    return await _safe_evaluate(
        page,
        "async reason => (window.__tsLab && typeof window.__tsLab.flush === 'function' ? await window.__tsLab.flush(reason) : null)",
        reason,
    )


async def _snapshot_real_page(page: Any, reason: str) -> Any:
    return await _safe_evaluate(
        page,
        "reason => (window.__turnstileObserver && typeof window.__turnstileObserver.snapshot === 'function' ? window.__turnstileObserver.snapshot(reason) : null)",
        reason,
    )


async def _attempt_turnstile_click(page: Any, capture: EventCapture, timeout_ms: int = 15000) -> Dict[str, Any]:
    selectors = (
        "label.ctp-checkbox-label",
        "input[type='checkbox']",
        "[role='checkbox']",
        ".ctp-checkbox-label",
    )
    deadline = asyncio.get_running_loop().time() + timeout_ms / 1000
    last_error = None

    while asyncio.get_running_loop().time() < deadline:
        frames = [frame for frame in page.frames if "challenges.cloudflare.com" in (frame.url or "")]
        for frame in frames:
            for selector in selectors:
                try:
                    locator = frame.locator(selector).first
                    if await locator.count() == 0:
                        continue
                    if not await locator.is_visible(timeout=600):
                        continue
                    await locator.click(timeout=2000)
                    result = {"clicked": True, "frameUrl": frame.url, "selector": selector, "ts": _iso_now()}
                    capture.click_attempts.append(result)
                    capture._trim()
                    return result
                except Exception as exc:
                    last_error = str(exc)
                    capture.click_attempts.append(
                        {
                            "clicked": False,
                            "frameUrl": frame.url,
                            "selector": selector,
                            "error": last_error,
                            "ts": _iso_now(),
                        }
                    )
                    capture._trim()
        await asyncio.sleep(0.5)

    return {"clicked": False, "error": last_error or "No clickable Turnstile control found"}


async def _wait_for_local_result(page: Any, timeout_ms: int) -> Dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + timeout_ms / 1000
    latest_snapshot = None

    while asyncio.get_running_loop().time() < deadline:
        latest_snapshot = await _snapshot_local_page(page, "probe-wait")
        if isinstance(latest_snapshot, dict):
            token = latest_snapshot.get("callbackToken") or latest_snapshot.get("hiddenInputResponse") or latest_snapshot.get("currentResponse")
            verify = latest_snapshot.get("lastVerify") or {}
            verify_body = verify.get("response") if isinstance(verify, dict) else None
            if token and not str(token).startswith("__getResponseError__"):
                return {"status": "token", "snapshot": latest_snapshot}
            if latest_snapshot.get("lastErrorCode"):
                return {"status": "error-callback", "snapshot": latest_snapshot}
            if latest_snapshot.get("scriptTimedOut"):
                return {"status": "script-timeout", "snapshot": latest_snapshot}
            if isinstance(verify_body, dict) and verify_body.get("success") is False:
                return {"status": "verify-failed", "snapshot": latest_snapshot}
        await asyncio.sleep(1)

    return {"status": "timeout", "snapshot": latest_snapshot}


async def _collect_frame_tree(page: Any) -> List[Dict[str, Any]]:
    tree = []
    for frame in page.frames:
        tree.append(
            {
                "name": frame.name,
                "url": frame.url,
                "parentUrl": frame.parent_frame.url if frame.parent_frame else None,
            }
        )
    return tree


@asynccontextmanager
async def _open_browser(case: BrowserCase, *, headless: bool):
    if case.kind == "raw":
        from playwright.async_api import async_playwright

        async with async_playwright() as pw:
            prefs = {}
            if case.disable_coop:
                prefs["browser.tabs.remote.useCrossOriginOpenerPolicy"] = False
            browser = await pw.firefox.launch(
                executable_path=str(case.binary_path),
                headless=headless,
                firefox_user_prefs=prefs,
            )
            try:
                yield browser
            finally:
                await browser.close()
        return

    if case.kind == "camoufox":
        from camoufox import AsyncCamoufox

        async with AsyncCamoufox(
            executable_path=str(case.binary_path),
            headless=headless,
            disable_coop=case.disable_coop,
        ) as browser:
            yield browser
        return

    raise ValueError(f"Unsupported case kind: {case.kind}")


async def _new_context(browser: Any, *, init_script: Optional[str] = None) -> Any:
    context = await browser.new_context(viewport={"width": 1280, "height": 920})
    if init_script:
        await context.add_init_script(init_script)
    return context


async def run_local_probe(
    case: BrowserCase,
    *,
    mode: str,
    lab_base_url: str,
    output_dir: Path,
    headless: bool,
    timeout_ms: int,
) -> Dict[str, Any]:
    case_slug = _slug(f"{case.case_label}-{mode}")
    case_dir = output_dir / case_slug
    case_dir.mkdir(parents=True, exist_ok=True)
    capture = EventCapture()
    session_id = f"session-{case_slug}"
    case_id = f"case-{case_slug}"
    url = f"{lab_base_url}/?mode={urllib.parse.quote(mode)}&session={urllib.parse.quote(session_id)}&case={urllib.parse.quote(case_id)}"

    async with _open_browser(case, headless=headless) as browser:
        version = browser.version
        context = await _new_context(browser)
        page = await context.new_page()
        await _attach_capture(context, page, capture)

        navigation_error = None
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except Exception as exc:
            navigation_error = str(exc)

        before_path = case_dir / "before.png"
        try:
            await page.screenshot(path=str(before_path), full_page=True)
        except Exception:
            pass

        click_result = None
        wait_result = {"status": "navigation-error", "snapshot": None}
        if navigation_error is None:
            await asyncio.sleep(2)
            if mode == "dummy-interactive":
                click_result = await _attempt_turnstile_click(page, capture)
                await asyncio.sleep(2)
            wait_result = await _wait_for_local_result(page, timeout_ms)
            await _flush_local_page(page, "probe-final")

        final_snapshot = await _snapshot_local_page(page, "probe-summary") if navigation_error is None else None
        frame_tree = await _collect_frame_tree(page) if navigation_error is None else []
        after_path = case_dir / "after.png"
        try:
            await page.screenshot(path=str(after_path), full_page=True)
        except Exception:
            pass
        html_path = case_dir / "page.html"
        try:
            html_path.write_text(await page.content(), encoding="utf-8")
        except Exception:
            pass
        await context.close()

    server_results = _fetch_json(
        f"{lab_base_url}/results?case_id={urllib.parse.quote(case_id)}&session_id={urllib.parse.quote(session_id)}&limit=80"
    )
    latest_verify = server_results.get("latestVerify") or {}
    verify_payload = latest_verify.get("payload", {}).get("verify") if isinstance(latest_verify, dict) else None
    verify_body = verify_payload.get("response") if isinstance(verify_payload, dict) else None

    summary = {
        "kind": "local",
        "binaryLabel": case.binary_label,
        "binaryPath": str(case.binary_path),
        "launcher": case.launcher,
        "disableCoop": case.disable_coop,
        "browserVersion": version,
        "mode": mode,
        "url": url,
        "navigationError": navigation_error,
        "status": wait_result.get("status") if navigation_error is None else "navigation-error",
        "clickResult": click_result,
        "latestSnapshot": final_snapshot,
        "waitSnapshot": wait_result.get("snapshot"),
        "serverResults": server_results,
        "verifyPayload": verify_payload,
        "verifySuccess": verify_body.get("success") if isinstance(verify_body, dict) else None,
        "frameTree": frame_tree,
        "capture": {
            "console": capture.console,
            "pageErrors": capture.page_errors,
            "requestFailures": capture.request_failures,
            "responses": capture.responses,
            "frameNavs": capture.frame_navs,
            "clickAttempts": capture.click_attempts,
        },
        "artifacts": {
            "caseDir": str(case_dir),
            "beforeScreenshot": str(before_path),
            "afterScreenshot": str(after_path),
            "pageHtml": str(html_path),
        },
    }
    _write_json(case_dir / "summary.json", summary)
    return summary


async def run_real_probe(
    case: BrowserCase,
    *,
    target_url: str,
    output_dir: Path,
    headless: bool,
    timeout_ms: int,
) -> Dict[str, Any]:
    case_slug = _slug(f"{case.case_label}-real-{target_url}")
    case_dir = output_dir / case_slug
    case_dir.mkdir(parents=True, exist_ok=True)
    capture = EventCapture()

    async with _open_browser(case, headless=headless) as browser:
        version = browser.version
        context = await _new_context(browser, init_script=REAL_OBSERVER_INIT_SCRIPT)
        page = await context.new_page()
        await _attach_capture(context, page, capture)

        navigation_error = None
        try:
            await page.goto(target_url, wait_until="domcontentloaded", timeout=45000)
        except Exception as exc:
            navigation_error = str(exc)

        before_path = case_dir / "before.png"
        try:
            await page.screenshot(path=str(before_path), full_page=True)
        except Exception:
            pass

        click_result = None
        if navigation_error is None:
            await asyncio.sleep(6)
            click_result = await _attempt_turnstile_click(page, capture, timeout_ms=12000)
            await asyncio.sleep(timeout_ms / 1000)

        final_snapshot = await _snapshot_real_page(page, "real-summary") if navigation_error is None else None
        frame_tree = await _collect_frame_tree(page) if navigation_error is None else []
        after_path = case_dir / "after.png"
        try:
            await page.screenshot(path=str(after_path), full_page=True)
        except Exception:
            pass
        html_path = case_dir / "page.html"
        try:
            html_path.write_text(await page.content(), encoding="utf-8")
        except Exception:
            pass
        await context.close()

    summary = {
        "kind": "real",
        "binaryLabel": case.binary_label,
        "binaryPath": str(case.binary_path),
        "launcher": case.launcher,
        "disableCoop": case.disable_coop,
        "browserVersion": version,
        "url": target_url,
        "navigationError": navigation_error,
        "clickResult": click_result,
        "snapshot": final_snapshot,
        "frameTree": frame_tree,
        "capture": {
            "console": capture.console,
            "pageErrors": capture.page_errors,
            "requestFailures": capture.request_failures,
            "responses": capture.responses,
            "frameNavs": capture.frame_navs,
            "clickAttempts": capture.click_attempts,
        },
        "artifacts": {
            "caseDir": str(case_dir),
            "beforeScreenshot": str(before_path),
            "afterScreenshot": str(after_path),
            "pageHtml": str(html_path),
        },
    }
    _write_json(case_dir / "summary.json", summary)
    return summary


def _build_cases(binary_specs: List[tuple[str, Path]], launchers: List[str]) -> List[BrowserCase]:
    cases: List[BrowserCase] = []
    launcher_specs = _launcher_presets(launchers)
    for label, binary_path in binary_specs:
        if not binary_path.exists():
            raise FileNotFoundError(f"Binary not found: {binary_path}")
        for launcher_name, kind, disable_coop in launcher_specs:
            cases.append(
                BrowserCase(
                    binary_label=label,
                    binary_path=binary_path,
                    launcher=launcher_name,
                    kind=kind,
                    disable_coop=disable_coop,
                )
            )
    return cases


async def _run(args: argparse.Namespace) -> int:
    binary_specs = _extract_binary_specs(args.binary)
    if not binary_specs:
        print("没有找到可用的 Camoufox 二进制，请用 --binary label=path 指定。", file=sys.stderr)
        return 1

    launchers = args.launcher or ["raw", "camoufox"]
    cases = _build_cases(binary_specs, launchers)
    modes = args.mode or ["dummy-success", "dummy-interactive"]
    real_urls = args.real_url or []

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    output_dir = (Path(args.output_dir).resolve() if args.output_dir else (REPO_ROOT / "tmp" / "turnstile-lab" / timestamp).resolve())
    output_dir.mkdir(parents=True, exist_ok=True)

    lab = start_lab_server(results_dir=output_dir / "lab-server")
    print(f"Turnstile lab server: {lab.base_url}")
    print(f"Artifacts: {output_dir}")

    results: List[Dict[str, Any]] = []
    try:
        for case in cases:
            for mode in modes:
                print(f"\n[local] {case.case_label} -> {mode}")
                result = await run_local_probe(
                    case,
                    mode=mode,
                    lab_base_url=lab.base_url,
                    output_dir=output_dir,
                    headless=not args.headed,
                    timeout_ms=args.timeout_ms,
                )
                results.append(result)
                token_present = False
                snapshot = result.get("latestSnapshot") or {}
                if isinstance(snapshot, dict):
                    token_present = bool(snapshot.get("callbackToken") or snapshot.get("hiddenInputResponse") or snapshot.get("currentResponse"))
                print(
                    "  status={status} token={token} verify={verify} click={click}".format(
                        status=result.get("status"),
                        token=token_present,
                        verify=result.get("verifySuccess"),
                        click=(result.get("clickResult") or {}).get("clicked"),
                    )
                )

            for target_url in real_urls:
                print(f"\n[real] {case.case_label} -> {target_url}")
                result = await run_real_probe(
                    case,
                    target_url=target_url,
                    output_dir=output_dir,
                    headless=not args.headed,
                    timeout_ms=args.real_wait_ms,
                )
                results.append(result)
                snapshot = result.get("snapshot") or {}
                iframe_count = snapshot.get("iframeCount") if isinstance(snapshot, dict) else None
                print(
                    "  navigationError={error} iframeCount={iframe_count} click={click}".format(
                        error=result.get("navigationError"),
                        iframe_count=iframe_count,
                        click=(result.get("clickResult") or {}).get("clicked"),
                    )
                )
    finally:
        lab.shutdown()

    aggregate = {
        "generatedAt": _iso_now(),
        "outputDir": str(output_dir),
        "cases": results,
    }
    _write_json(output_dir / "aggregate.json", aggregate)
    print(f"\nSummary written to {output_dir / 'aggregate.json'}")
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Probe Turnstile behavior across binaries and launch paths.")
    parser.add_argument(
        "--binary",
        action="append",
        help="Binary spec, either label=path or plain path. Repeatable.",
    )
    parser.add_argument(
        "--launcher",
        action="append",
        choices=["raw", "raw-disable-coop", "camoufox", "camoufox-disable-coop"],
        help="Launch path to test. Repeatable.",
    )
    parser.add_argument(
        "--mode",
        action="append",
        choices=[
            "dummy-success",
            "dummy-fail",
            "dummy-invisible-success",
            "dummy-invisible-fail",
            "dummy-interactive",
        ],
        help="Local lab mode to run. Repeatable.",
    )
    parser.add_argument("--real-url", action="append", help="Optional real page URL to observe. Repeatable.")
    parser.add_argument("--timeout-ms", type=int, default=30000, help="Local case timeout after navigation.")
    parser.add_argument("--real-wait-ms", type=int, default=12000, help="Additional wait time for real-page observation after navigation.")
    parser.add_argument("--output-dir", help="Directory for screenshots and JSON artifacts.")
    parser.add_argument("--headed", action="store_true", help="Run browsers in headed mode instead of headless.")
    return parser.parse_args()


def main() -> int:
    _reconfigure_stdio()
    args = _parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
