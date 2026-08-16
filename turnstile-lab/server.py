from __future__ import annotations

import argparse
import json
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4


TURNSTILE_SITEKEYS = {
    "dummy-success": "1x00000000000000000000AA",
    "dummy-fail": "2x00000000000000000000AB",
    "dummy-invisible-success": "1x00000000000000000000BB",
    "dummy-invisible-fail": "2x00000000000000000000BB",
    "dummy-interactive": "3x00000000000000000000FF",
}

TURNSTILE_SECRETS = {
    "always_pass": "1x0000000000000000000000000000000AA",
    "always_fail": "2x0000000000000000000000000000000AA",
    "always_duplicate": "3x0000000000000000000000000000000AA",
}

SITEVERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_component(value: Any, default: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return default

    cleaned = []
    for char in raw:
        if char.isalnum() or char in ("-", "_", "."):
            cleaned.append(char)
        else:
            cleaned.append("_")

    normalized = "".join(cleaned).strip("._")
    return normalized or default


def _mask_secret(secret: str) -> str:
    if len(secret) <= 8:
        return "*" * len(secret)
    return f"{secret[:4]}{'*' * (len(secret) - 8)}{secret[-4:]}"


def _verify_secret_for_mode(mode: str) -> str:
    if mode in ("dummy-success", "dummy-invisible-success", "dummy-interactive"):
        return TURNSTILE_SECRETS["always_pass"]
    if mode in ("dummy-fail", "dummy-invisible-fail"):
        return TURNSTILE_SECRETS["always_fail"]
    return TURNSTILE_SECRETS["always_pass"]


def _json_response(handler: BaseHTTPRequestHandler, status: int, payload: Dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def _text_response(handler: BaseHTTPRequestHandler, status: int, body: bytes, content_type: str) -> None:
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


@dataclass
class LabState:
    html_path: Path
    results_dir: Path
    lock: threading.Lock = field(default_factory=threading.Lock)
    history: List[Dict[str, Any]] = field(default_factory=list)
    latest_report_by_case: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    latest_verify_by_case: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    def record(self, kind: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        session_id = _safe_component(payload.get("sessionId"), "session")
        case_id = _safe_component(payload.get("caseId"), "case")
        received_at = _iso_now()
        record = {
            "id": uuid4().hex,
            "kind": kind,
            "receivedAt": received_at,
            "payload": payload,
        }

        record_dir = self.results_dir / session_id / case_id
        record_dir.mkdir(parents=True, exist_ok=True)
        timestamp_stub = received_at.replace(":", "").replace("-", "")
        file_path = record_dir / f"{timestamp_stub}-{kind}.json"
        file_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        record["artifactPath"] = str(file_path)

        with self.lock:
            self.history.append(record)
            if len(self.history) > 400:
                self.history = self.history[-400:]

            if kind == "report":
                self.latest_report_by_case[case_id] = record
            elif kind == "verify":
                self.latest_verify_by_case[case_id] = record

        return record

    def query(self, *, case_id: Optional[str], session_id: Optional[str], limit: int) -> Dict[str, Any]:
        with self.lock:
            entries = list(self.history)

        if session_id:
            entries = [entry for entry in entries if str(entry.get("payload", {}).get("sessionId")) == session_id]
        if case_id:
            entries = [entry for entry in entries if str(entry.get("payload", {}).get("caseId")) == case_id]
        entries = entries[-max(1, min(limit, 200)) :]

        latest_report = None
        latest_verify = None
        if case_id:
            latest_report = self.latest_report_by_case.get(_safe_component(case_id, "case"))
            latest_verify = self.latest_verify_by_case.get(_safe_component(case_id, "case"))

        return {
            "ok": True,
            "historyCount": len(entries),
            "entries": entries,
            "latestReport": latest_report,
            "latestVerify": latest_verify,
            "availableModes": {
                "sitekeys": TURNSTILE_SITEKEYS,
                "verifySecrets": {key: _mask_secret(value) for key, value in TURNSTILE_SECRETS.items()},
            },
        }


@dataclass
class RunningLabServer:
    server: ThreadingHTTPServer
    state: LabState
    thread: threading.Thread
    base_url: str

    def shutdown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def _proxy_siteverify(payload: Dict[str, Any]) -> Dict[str, Any]:
    mode = str(payload.get("mode") or "")
    token = str(payload.get("token") or payload.get("response") or "")
    if not token:
        raise ValueError("token is required")

    secret = str(payload.get("secret") or _verify_secret_for_mode(mode))
    form_payload = {
        "secret": secret,
        "response": token,
    }
    remote_ip = payload.get("remoteip")
    if remote_ip:
        form_payload["remoteip"] = str(remote_ip)

    request_body = urllib.parse.urlencode(form_payload).encode("utf-8")
    request = urllib.request.Request(
        SITEVERIFY_URL,
        data=request_body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "turnstile-lab/1.0",
        },
        method="POST",
    )

    started = time.perf_counter()
    http_status = 0
    raw_body = b""
    transport_error = None
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            http_status = getattr(response, "status", 200)
            raw_body = response.read()
    except urllib.error.HTTPError as exc:
        http_status = exc.code
        raw_body = exc.read()
        transport_error = str(exc)
    except Exception as exc:
        transport_error = str(exc)
    duration_ms = round((time.perf_counter() - started) * 1000, 2)

    parsed_body: Any
    try:
        parsed_body = json.loads(raw_body.decode("utf-8")) if raw_body else None
    except Exception:
        parsed_body = raw_body.decode("utf-8", errors="replace") if raw_body else None

    return {
        "ok": transport_error is None,
        "mode": mode,
        "httpStatus": http_status,
        "durationMs": duration_ms,
        "response": parsed_body,
        "transportError": transport_error,
        "secretMasked": _mask_secret(secret),
        "tokenLength": len(token),
    }


def start_lab_server(
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    root_dir: Optional[Path] = None,
    results_dir: Optional[Path] = None,
) -> RunningLabServer:
    root = (root_dir or Path(__file__).resolve().parent).resolve()
    html_path = root / "turnstile_test_page.html"
    if not html_path.exists():
        raise FileNotFoundError(f"Missing lab page: {html_path}")

    output_dir = (results_dir or (root / "results")).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    state = LabState(html_path=html_path, results_dir=output_dir)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _format: str, *_args: Any) -> None:
            return

        def do_GET(self) -> None:
            parsed = urllib.parse.urlsplit(self.path)
            if parsed.path in ("/", "/test", "/test/"):
                body = state.html_path.read_bytes()
                _text_response(self, HTTPStatus.OK, body, "text/html; charset=utf-8")
                return

            if parsed.path == "/healthz":
                _json_response(self, HTTPStatus.OK, {"ok": True, "now": _iso_now()})
                return

            if parsed.path == "/results":
                query = urllib.parse.parse_qs(parsed.query)
                case_id = query.get("case_id", [None])[0]
                session_id = query.get("session_id", [None])[0]
                try:
                    limit = int(query.get("limit", ["20"])[0])
                except ValueError:
                    limit = 20
                _json_response(
                    self,
                    HTTPStatus.OK,
                    state.query(case_id=case_id, session_id=session_id, limit=limit),
                )
                return

            _json_response(self, HTTPStatus.NOT_FOUND, {"ok": False, "error": f"Unknown path: {parsed.path}"})

        def do_POST(self) -> None:
            parsed = urllib.parse.urlsplit(self.path)
            content_length = int(self.headers.get("Content-Length") or "0")
            raw_body = self.rfile.read(content_length)
            try:
                payload = json.loads(raw_body.decode("utf-8")) if raw_body else {}
            except json.JSONDecodeError as exc:
                _json_response(self, HTTPStatus.BAD_REQUEST, {"ok": False, "error": f"Invalid JSON: {exc}"})
                return

            if parsed.path == "/report":
                record = state.record("report", payload)
                _json_response(
                    self,
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "recordId": record["id"],
                        "artifactPath": record["artifactPath"],
                    },
                )
                return

            if parsed.path == "/siteverify":
                try:
                    verify_result = _proxy_siteverify(payload)
                except ValueError as exc:
                    _json_response(self, HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                    return

                record_payload = {
                    **payload,
                    "verify": verify_result,
                }
                record = state.record("verify", record_payload)
                _json_response(
                    self,
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        **verify_result,
                        "recordId": record["id"],
                        "artifactPath": record["artifactPath"],
                    },
                )
                return

            _json_response(self, HTTPStatus.NOT_FOUND, {"ok": False, "error": f"Unknown path: {parsed.path}"})

    server = ThreadingHTTPServer((host, port), Handler)
    actual_host, actual_port = server.server_address
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return RunningLabServer(
        server=server,
        state=state,
        thread=thread,
        base_url=f"http://{actual_host}:{actual_port}",
    )


def _main() -> int:
    parser = argparse.ArgumentParser(description="Serve the Turnstile lab page and result endpoints.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--results-dir", type=Path, default=None)
    args = parser.parse_args()

    running = start_lab_server(host=args.host, port=args.port, results_dir=args.results_dir)
    print(f"Turnstile lab listening on {running.base_url}")
    print(f"Artifacts: {running.state.results_dir}")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        running.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
