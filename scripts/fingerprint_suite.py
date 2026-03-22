#!/usr/bin/env python3
"""
Run a repeatable fingerprint test suite against:

- BrowserLeaks
- CreepJS
- bot.sannysoft.com

The script parses DOM/text/table structure only. It does not rely on screenshots
or image OCR, which makes it more suitable for regression checks.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import re
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
PYTHONLIB_DIR = REPO_ROOT / "pythonlib"

if PYTHONLIB_DIR.exists():
    sys.path.insert(0, str(PYTHONLIB_DIR))

from camoufox import DefaultAddons  # type: ignore  # noqa: E402
from camoufox.sync_api import Camoufox  # type: ignore  # noqa: E402

ACCEPT_ENCODING = "identity"
DEFAULT_SETTLE_MS = 12_000
DEFAULT_TIMEOUT_MS = 90_000
DEFAULT_RETRIES = 1

BROWSERLEAKS_URLS = {
    "javascript": "https://browserleaks.com/javascript",
    "client_hints": "https://browserleaks.com/client-hints",
    "canvas": "https://browserleaks.com/canvas",
    "webgl": "https://browserleaks.com/webgl",
    "fonts": "https://browserleaks.com/fonts",
}

CREEPJS_URL = "https://abrahamjuliot.github.io/creepjs/"
SANNYSOFT_URL = "https://bot.sannysoft.com/"


def normalize_text(value: Optional[str]) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def normalize_ip(value: Optional[str]) -> Optional[str]:
    candidate = normalize_text(value)
    if not candidate:
        return None
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return None


def is_public_ip(value: Optional[str]) -> bool:
    candidate = normalize_ip(value)
    if not candidate:
        return False
    return ipaddress.ip_address(candidate).is_global


def resolve_network_profile(timeout: float = 8.0) -> Dict[str, Optional[str]]:
    url = "http://ip-api.com/json?fields=query,timezone"
    profile: Dict[str, Optional[str]] = {
        "ip": None,
        "timezone": None,
        "source": url,
        "error": None,
    }
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
        profile["ip"] = normalize_ip(payload.get("query"))
        profile["timezone"] = normalize_text(payload.get("timezone")) or None
    except Exception as exc:
        profile["error"] = repr(exc)
    return profile


def build_launch_network_overrides(
    network_profile: Dict[str, Optional[str]],
) -> Dict[str, Any]:
    config: Dict[str, Any] = {}
    firefox_user_prefs: Dict[str, Any] = {}
    timezone = normalize_text(network_profile.get("timezone")) or None
    if timezone:
        config["timezone"] = timezone
    ip_value = normalize_ip(network_profile.get("ip"))
    if not ip_value:
        return {
            "config": config,
            "firefox_user_prefs": firefox_user_prefs,
        }

    parsed_ip = ipaddress.ip_address(ip_value)
    if parsed_ip.version == 4:
        config["webrtc:ipv4"] = ip_value
        # Prefer IPv4-only ICE candidates when we know the public IPv4.
        firefox_user_prefs["network.dns.disableIPv6"] = True
    else:
        config["webrtc:ipv6"] = ip_value

    return {
        "config": config,
        "firefox_user_prefs": firefox_user_prefs,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run BrowserLeaks + CreepJS + Sannysoft fingerprint checks and "
            "save a machine-readable JSON report."
        )
    )
    parser.add_argument(
        "--output",
        help=(
            "Path to the output JSON file. Defaults to "
            "tmp/fingerprint_report_<timestamp>.json"
        ),
    )
    parser.add_argument(
        "--executable-path",
        help=(
            "Camoufox executable path. If omitted, the script will first try to "
            "auto-detect a locally built binary under the repo, then fall back "
            "to the package default."
        ),
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help=(
            "Run in headless mode. For Linux/XRDP/Xvfb scenarios, you can also "
            "keep headed mode and wrap the script with xvfb-run."
        ),
    )
    parser.add_argument(
        "--timeout-ms",
        type=int,
        default=DEFAULT_TIMEOUT_MS,
        help=f"Per-navigation timeout in ms. Default: {DEFAULT_TIMEOUT_MS}.",
    )
    parser.add_argument(
        "--settle-ms",
        type=int,
        default=DEFAULT_SETTLE_MS,
        help=(
            f"Extra wait after DOMContentLoaded in ms. Default: {DEFAULT_SETTLE_MS}."
        ),
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=DEFAULT_RETRIES,
        help=(
            "Retry count for flaky external checks. "
            f"Default: {DEFAULT_RETRIES}."
        ),
    )
    parser.add_argument(
        "--include-default-addons",
        action="store_true",
        help=(
            "Do not exclude default addons. By default, the script excludes all "
            "default addons to avoid source-tree runs failing when addon folders "
            "have not been extracted into manifest.json form."
        ),
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print the JSON report to stdout after finishing.",
    )
    return parser.parse_args()


def default_output_path() -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return REPO_ROOT / "tmp" / f"fingerprint_report_{timestamp}.json"


def resolve_output_path(output: Optional[str]) -> Path:
    path = Path(output) if output else default_output_path()
    if not path.is_absolute():
        path = REPO_ROOT / path
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def auto_detect_executable() -> Optional[Path]:
    patterns = [
        "camoufox-*/obj-*/dist/bin/camoufox",
        "camoufox-*/obj-*/dist/bin/camoufox.exe",
        "camoufox-*/obj-*/dist/bin/firefox",
        "camoufox-*/obj-*/dist/bin/firefox.exe",
    ]
    candidates: List[Path] = []
    for pattern in patterns:
        candidates.extend(REPO_ROOT.glob(pattern))
    candidates = [candidate for candidate in candidates if candidate.is_file()]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def navigate(page: Any, url: str, timeout_ms: int, settle_ms: int) -> None:
    page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    try:
        page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 15_000))
    except Exception:
        pass
    page.wait_for_timeout(settle_ms)


def extract_body_text(page: Any) -> str:
    return normalize_text(page.evaluate("() => document.body ? document.body.innerText : ''"))


def extract_sectioned_rows(page: Any) -> List[Dict[str, Any]]:
    return page.evaluate(
        r"""
        () => {
          const out = [];
          let section = '';
          for (const row of document.querySelectorAll('tr')) {
            const cells = Array.from(row.querySelectorAll('th,td'))
              .map(cell => (cell.innerText || '').replace(/\s+/g, ' ').trim())
              .filter(Boolean);
            if (!cells.length) {
              continue;
            }
            if (cells.length === 1) {
              section = cells[0];
              continue;
            }
            out.push({ section, cells });
          }
          return out;
        }
        """
    )


def build_section_map(rows: List[Dict[str, Any]]) -> Dict[str, Dict[str, str]]:
    section_map: Dict[str, Dict[str, str]] = {}
    for row in rows:
        section = row.get("section") or ""
        cells = row.get("cells") or []
        if len(cells) < 2:
            continue
        section_map.setdefault(section, {})
        section_map[section][cells[0]] = " | ".join(cells[1:])
    return section_map


def extract_tables(page: Any) -> List[Dict[str, Any]]:
    return page.evaluate(
        r"""
        () => Array.from(document.querySelectorAll('table')).map((table, index) => ({
          index,
          rows: Array.from(table.querySelectorAll('tr')).map(row =>
            Array.from(row.querySelectorAll('th,td'))
              .map(cell => (cell.innerText || '').replace(/\s+/g, ' ').trim())
              .filter(Boolean)
          ).filter(row => row.length)
        }))
        """
    )


def pick(section_map: Dict[str, Dict[str, str]], section: str, key: str) -> Optional[str]:
    return section_map.get(section, {}).get(key)


def first_match(text: str, pattern: str) -> Optional[str]:
    match = re.search(pattern, text, re.I)
    return match.group(1).strip() if match else None


def collect_browserleaks_javascript(page: Any, args: argparse.Namespace) -> Dict[str, Any]:
    navigate(page, BROWSERLEAKS_URLS["javascript"], args.timeout_ms, args.settle_ms)
    section_map = build_section_map(extract_sectioned_rows(page))
    return {
        "title": page.title(),
        "url": BROWSERLEAKS_URLS["javascript"],
        "audio_source_note": (
            "BrowserLeaks 当前没有稳定的独立 audio 页面，脚本从 "
            "/javascript 页内的 Web Audio API 区块提取音频相关结果。"
        ),
        "userAgent": pick(section_map, "Navigator Object", "userAgent"),
        "platform": pick(section_map, "Navigator Object", "platform"),
        "oscpu": pick(section_map, "Navigator Object", "oscpu"),
        "hardwareConcurrency": pick(section_map, "Navigator Object", "hardwareConcurrency"),
        "language": pick(section_map, "Navigator Object", "language"),
        "languages": pick(section_map, "Navigator Object", "languages"),
        "webdriver": pick(section_map, "Navigator Object", "webdriver"),
        "pdfViewerEnabled": pick(section_map, "Navigator Object", "pdfViewerEnabled"),
        "timezone": pick(section_map, "Internationalization API", "timeZone"),
        "screenResolution": pick(section_map, "Screen Object", "Screen Resolution"),
        "clientHintsApiStatus": pick(
            section_map, "navigator.userAgentData (Client Hints)", "API Status"
        ),
        "webAudioApiStatus": pick(section_map, "Web Audio API", "API Status"),
        "webAudioState": pick(section_map, "Web Audio API", "State"),
        "webAudioSampleRate": pick(section_map, "Web Audio API", "Sample Rate"),
        "speechVoices": pick(section_map, "SpeechSynthesis", "Speech Voices"),
    }


def collect_browserleaks_client_hints(
    page: Any, args: argparse.Namespace
) -> Dict[str, Any]:
    navigate(page, BROWSERLEAKS_URLS["client_hints"], args.timeout_ms, args.settle_ms)
    section_map = build_section_map(extract_sectioned_rows(page))
    return {
        "title": page.title(),
        "url": BROWSERLEAKS_URLS["client_hints"],
        "httpUserAgent": pick(section_map, "Your Web Browser", "HTTP User-Agent"),
        "jsApiStatus": pick(section_map, "Client Hints JavaScript API", "API Status"),
        "brands": pick(section_map, "Client Hints JavaScript API", "brands"),
        "mobile": pick(section_map, "Client Hints JavaScript API", "mobile"),
        "platform": pick(section_map, "Client Hints JavaScript API", "platform"),
        "sec-ch-ua": pick(section_map, "Client Hints HTTP Headers", "sec-ch-ua"),
        "sec-ch-ua-mobile": pick(
            section_map, "Client Hints HTTP Headers", "sec-ch-ua-mobile"
        ),
        "sec-ch-ua-platform": pick(
            section_map, "Client Hints HTTP Headers", "sec-ch-ua-platform"
        ),
    }


def collect_browserleaks_canvas(page: Any, args: argparse.Namespace) -> Dict[str, Any]:
    navigate(page, BROWSERLEAKS_URLS["canvas"], args.timeout_ms, args.settle_ms)
    section_map = build_section_map(extract_sectioned_rows(page))
    return {
        "title": page.title(),
        "url": BROWSERLEAKS_URLS["canvas"],
        "signature": pick(section_map, "Canvas Fingerprint", "Signature"),
        "uniqueness": pick(section_map, "Canvas Fingerprint", "Uniqueness"),
        "fileSize": pick(section_map, "Image File Details", "File Size"),
        "numberOfColors": pick(section_map, "Image File Details", "Number of Colors"),
    }


def collect_browserleaks_webgl(page: Any, args: argparse.Namespace) -> Dict[str, Any]:
    navigate(page, BROWSERLEAKS_URLS["webgl"], args.timeout_ms, args.settle_ms)
    section_map = build_section_map(extract_sectioned_rows(page))
    return {
        "title": page.title(),
        "url": BROWSERLEAKS_URLS["webgl"],
        "supportsWebGL": pick(
            section_map, "WebGL Support Detection", "This browser supports WebGL"
        ),
        "supportsWebGL2": pick(
            section_map, "WebGL Support Detection", "This browser supports WebGL 2"
        ),
        "renderer": pick(section_map, "WebGL Context Info", "Renderer"),
        "unmaskedVendor": pick(section_map, "Debug Renderer Info", "Unmasked Vendor"),
        "unmaskedRenderer": pick(
            section_map, "Debug Renderer Info", "Unmasked Renderer"
        ),
    }


def collect_browserleaks_fonts(page: Any, args: argparse.Namespace) -> Dict[str, Any]:
    navigate(page, BROWSERLEAKS_URLS["fonts"], args.timeout_ms, args.settle_ms)
    section_map = build_section_map(extract_sectioned_rows(page))
    return {
        "title": page.title(),
        "url": BROWSERLEAKS_URLS["fonts"],
        "fontMetricsFingerprint": pick(section_map, "Font Metrics", "Fingerprint"),
        "fontMetricsReport": pick(section_map, "Font Metrics", "Report"),
        "unicodeGlyphsFingerprint": pick(section_map, "Unicode Glyphs", "Fingerprint"),
    }


def collect_creepjs(page: Any, args: argparse.Namespace) -> Dict[str, Any]:
    navigate(page, CREEPJS_URL, args.timeout_ms, max(args.settle_ms, 16_000))
    body_text = extract_body_text(page)
    headless_block = normalize_text(
        page.evaluate(
            "() => (document.querySelector('#headless-resistance-detection-results') || {}).innerText || ''"
        )
    )
    return {
        "title": page.title(),
        "url": CREEPJS_URL,
        "fpId": first_match(body_text, r"FP ID:\s*([a-f0-9]{32,64})"),
        "fuzzy": first_match(body_text, r"Fuzzy:\s*([a-f0-9]{32,64})"),
        "elapsed": first_match(
            body_text, r"Fuzzy:\s*[a-f0-9]{32,64}\s*([0-9.]+\s*ms)"
        ),
        "chromium": first_match(headless_block, r"chromium:\s*(true|false)"),
        "likeHeadless": first_match(
            headless_block, r"like headless:\s*[a-f0-9]+\s*([0-9]+%)"
        ),
        "headless": first_match(headless_block, r"headless:\s*[a-f0-9]+\s*([0-9]+%)"),
        "stealth": first_match(headless_block, r"stealth:\s*[a-f0-9]+\s*([0-9]+%)"),
        "platformHints": first_match(
            headless_block, r"platform hints:\s*(.*?)\s+[0-9.]+ms\s+Resistance"
        ),
        "privacy": first_match(headless_block, r"privacy:\s*([^\s]+)"),
        "security": first_match(headless_block, r"security:\s*([^\s]+)"),
        "mode": first_match(headless_block, r"mode:\s*([^\s]+)"),
        "extension": first_match(headless_block, r"extension:\s*([^\s]+)"),
        "workerConfidence": first_match(body_text, r"confidence:\s*(high|medium|low)"),
        "workerUserAgent": first_match(
            body_text, r"userAgent:\s*(Mozilla/5\.0.*?Firefox/\d+\.\d+)"
        ),
        "webrtcCandidateIp": first_match(
            body_text,
            r"candidate:\d+\s+\d+\s+UDP\s+\d+\s+([0-9a-fA-F:\.]+)\s+\d+\s+typ",
        ),
        "headlessResistanceBlock": headless_block,
    }


def rows_to_map(rows: List[List[str]]) -> Dict[str, str]:
    mapped: Dict[str, str] = {}
    for row in rows:
        if len(row) >= 2 and row[0] != "Test Name":
            mapped[row[0]] = " | ".join(row[1:])
    return mapped


def collect_sannysoft(page: Any, args: argparse.Namespace) -> Dict[str, Any]:
    navigate(page, SANNYSOFT_URL, args.timeout_ms, max(args.settle_ms, 16_000))
    tables = extract_tables(page)
    intoli_rows = tables[0]["rows"] if len(tables) > 0 else []
    scanner_rows = tables[1]["rows"] if len(tables) > 1 else []
    fp_collect_rows = tables[2]["rows"] if len(tables) > 2 else []

    intoli = rows_to_map(intoli_rows)
    scanner = [
        {"name": row[0], "result": " | ".join(row[1:])}
        for row in scanner_rows
        if len(row) >= 2 and row[0] != "Test Name"
    ]
    fp_collect = rows_to_map(fp_collect_rows)

    return {
        "title": page.title(),
        "url": SANNYSOFT_URL,
        "userAgentOld": intoli.get("User Agent (Old)"),
        "webdriverNew": intoli.get("WebDriver (New)"),
        "webdriverAdvanced": intoli.get("WebDriver Advanced"),
        "chromeNew": intoli.get("Chrome (New)"),
        "permissionsNew": intoli.get("Permissions (New)"),
        "pluginsLengthOld": intoli.get("Plugins Length (Old)"),
        "pluginsTypeOld": intoli.get("Plugins is of type PluginArray"),
        "languagesOld": intoli.get("Languages (Old)"),
        "webglVendor": intoli.get("WebGL Vendor"),
        "webglRenderer": intoli.get("WebGL Renderer"),
        "brokenImageDimensions": intoli.get("Broken Image Dimensions"),
        "flaggedMain": [
            {"name": key, "result": value}
            for key, value in intoli.items()
            if any(flag in value.lower() for flag in ("failed", "missing", "prompt"))
        ],
        "scannerNonOk": [
            row for row in scanner if not row["result"].lower().startswith("ok")
        ],
        "scannerSample": scanner[:10],
        "fpCollectUserAgent": fp_collect.get("userAgent"),
        "fpCollectWebdriver": fp_collect.get("webdriver"),
    }


def build_summary(
    checks: Dict[str, Any], meta: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    positives: List[str] = []
    warnings: List[str] = []
    high_risk_findings: List[str] = []
    network_profile = (
        meta.get("networkProfile", {}) if isinstance(meta, dict) else {}
    )
    expected_ip = normalize_ip(
        network_profile.get("ip") if isinstance(network_profile, dict) else None
    )
    expected_timezone = normalize_text(
        network_profile.get("timezone") if isinstance(network_profile, dict) else None
    )

    js_result = checks.get("browserleaks.javascript", {})
    if js_result.get("status") == "ok":
        data = js_result["data"]
        if data.get("webdriver") == "false":
            positives.append("BrowserLeaks JavaScript: webdriver=false")
        if data.get("clientHintsApiStatus"):
            positives.append(
                f"BrowserLeaks JavaScript: Client Hints 状态为 {data['clientHintsApiStatus']}"
            )
        if expected_timezone and data.get("timezone") == expected_timezone:
            positives.append(
                f"BrowserLeaks JavaScript: timezone 与网络画像一致 ({expected_timezone})"
            )
        elif data.get("timezone") == "UTC":
            warnings.append(
                "BrowserLeaks JavaScript: timezone=UTC；若与出口 IP 地域不一致，可能增加风险。"
            )

    creep_result = checks.get("creepjs", {})
    if creep_result.get("status") == "ok":
        data = creep_result["data"]
        if data.get("headless") == "0%" and data.get("likeHeadless") == "0%":
            positives.append("CreepJS: headless/like headless 均为 0%")
        candidate_ip = normalize_ip(data.get("webrtcCandidateIp"))
        if candidate_ip and expected_ip and candidate_ip == expected_ip:
            positives.append(
                f"CreepJS: WebRTC candidate 与出口 IP 一致 ({candidate_ip})"
            )
        elif candidate_ip and is_public_ip(candidate_ip):
            warnings.append(
                f"CreepJS: 检测到公网 WebRTC candidate -> {candidate_ip}"
            )
        elif candidate_ip:
            high_risk_findings.append(
                f"CreepJS: 检测到 WebRTC candidate IP 泄露 -> {candidate_ip}"
            )
        elif expected_ip:
            positives.append("CreepJS: 未发现额外 WebRTC candidate")

    sanny_result = checks.get("sannysoft", {})
    if sanny_result.get("status") == "ok":
        data = sanny_result["data"]
        if data.get("webdriverAdvanced") == "passed":
            positives.append("Sannysoft: WebDriver Advanced=passed")
        for flagged in data.get("flaggedMain", []):
            if flagged["name"] == "Chrome (New)" and "missing (failed)" in flagged["result"]:
                warnings.append(
                    "Sannysoft: Chrome (New)=missing (failed)；对 Firefox 系通常是可解释现象。"
                )
            elif flagged["name"] == "Permissions (New)" and "prompt" in flagged["result"]:
                warnings.append("Sannysoft: Permissions (New)=prompt")
            elif flagged["name"] == "WebDriver (New)" and "missing (passed)" in flagged["result"]:
                positives.append("Sannysoft: WebDriver (New)=missing (passed)")
            else:
                warnings.append(
                    f"Sannysoft: {flagged['name']} -> {flagged['result']}"
                )

    return {
        "highRiskFindings": high_risk_findings,
        "warnings": warnings,
        "positives": positives,
    }


def run_check(
    name: str,
    page: Any,
    collector: Callable[[Any, argparse.Namespace], Dict[str, Any]],
    args: argparse.Namespace,
) -> Dict[str, Any]:
    attempts = max(args.retries, 0) + 1
    last_exc: Optional[Exception] = None
    total_started = time.time()

    for attempt in range(1, attempts + 1):
        prefix = f"[RUN ] {name}"
        if attempts > 1:
            prefix += f" (attempt {attempt}/{attempts})"
        print(prefix, flush=True)
        started = time.time()
        try:
            data = collector(page, args)
            duration_ms = int((time.time() - started) * 1000)
            total_duration_ms = int((time.time() - total_started) * 1000)
            print(f"[ OK ] {name} ({duration_ms} ms)", flush=True)
            return {
                "status": "ok",
                "durationMs": total_duration_ms,
                "attempts": attempt,
                "data": data,
            }
        except Exception as exc:
            last_exc = exc
            duration_ms = int((time.time() - started) * 1000)
            print(f"[ERR ] {name} ({duration_ms} ms): {exc}", flush=True)
            if attempt < attempts:
                print(f"[RETRY] {name} -> retrying after transient failure", flush=True)
                page.wait_for_timeout(1_500)

    total_duration_ms = int((time.time() - total_started) * 1000)
    return {
        "status": "error",
        "durationMs": total_duration_ms,
        "attempts": attempts,
        "error": repr(last_exc),
    }


def main() -> int:
    args = parse_args()
    output_path = resolve_output_path(args.output)
    network_profile = resolve_network_profile()

    executable_path = (
        Path(args.executable_path)
        if args.executable_path
        else auto_detect_executable()
    )

    exclude_addons = None
    if not args.include_default_addons:
        exclude_addons = list(DefaultAddons)

    launch_kwargs: Dict[str, Any] = {
        "headless": args.headless,
        "exclude_addons": exclude_addons,
    }
    if executable_path:
        launch_kwargs["executable_path"] = str(executable_path)
    network_overrides = build_launch_network_overrides(network_profile)
    if network_overrides["config"]:
        launch_kwargs["config"] = network_overrides["config"]
    if network_overrides["firefox_user_prefs"]:
        launch_kwargs["firefox_user_prefs"] = network_overrides["firefox_user_prefs"]
    if network_overrides["config"]:
        launch_kwargs["i_know_what_im_doing"] = True

    report: Dict[str, Any] = {
        "meta": {
            "generatedAt": datetime.now().astimezone().isoformat(),
            "repoRoot": str(REPO_ROOT),
            "outputPath": str(output_path),
            "headless": args.headless,
            "timeoutMs": args.timeout_ms,
            "settleMs": args.settle_ms,
            "executablePath": str(executable_path) if executable_path else None,
            "excludeDefaultAddons": not args.include_default_addons,
            "networkProfile": network_profile,
        },
        "checks": {},
        "summary": {},
    }

    with Camoufox(**launch_kwargs) as browser:
        page = browser.new_page(
            extra_http_headers={"accept-encoding": ACCEPT_ENCODING}
        )
        report["checks"]["browserleaks.javascript"] = run_check(
            "browserleaks.javascript", page, collect_browserleaks_javascript, args
        )
        report["checks"]["browserleaks.client_hints"] = run_check(
            "browserleaks.client_hints", page, collect_browserleaks_client_hints, args
        )
        report["checks"]["browserleaks.canvas"] = run_check(
            "browserleaks.canvas", page, collect_browserleaks_canvas, args
        )
        report["checks"]["browserleaks.webgl"] = run_check(
            "browserleaks.webgl", page, collect_browserleaks_webgl, args
        )
        report["checks"]["browserleaks.fonts"] = run_check(
            "browserleaks.fonts", page, collect_browserleaks_fonts, args
        )
        report["checks"]["creepjs"] = run_check(
            "creepjs", page, collect_creepjs, args
        )
        report["checks"]["sannysoft"] = run_check(
            "sannysoft", page, collect_sannysoft, args
        )

    report["summary"] = build_summary(report["checks"], report["meta"])

    output_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"[SAVE] {output_path}", flush=True)
    if args.pretty:
        print(json.dumps(report, ensure_ascii=False, indent=2))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
