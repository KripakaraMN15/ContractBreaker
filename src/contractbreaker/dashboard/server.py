"""Standard-library HTTP server hosting the ContractBreaker dashboard."""

from __future__ import annotations

import json
import mimetypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from ..version import __version__
from .api import ApiError, DashboardConfig, DashboardService

STATIC_DIR = Path(__file__).resolve().parent / "static"
_MAX_BODY_BYTES = 64 * 1024


def build_service(allowed_root: Path) -> DashboardService:
    """Create a dashboard service bound to one source root."""

    import os

    return DashboardService(
        DashboardConfig(
            allowed_root=Path(allowed_root).resolve(),
            llm_fallback_available=bool(os.environ.get("OPENAI_API_KEY")),
        )
    )


class DashboardHandler(BaseHTTPRequestHandler):
    """Route API calls and static assets. No shell, no environment exposure."""

    server_version = f"ContractBreaker/{__version__}"
    service: DashboardService

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        """Silence default stderr access logging."""

    # -- routing -------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/api/health":
            self._send_json(200, self.service.health())
            return
        if path == "/api/sources":
            self._guard(self.service.list_sources)
            return
        if path in ("/", "/index.html"):
            self._send_static("index.html")
            return
        if path.startswith("/static/"):
            self._send_static(path[len("/static/") :])
            return
        self._send_json(404, {"ok": False, "error": {"code": "not_found", "message": "unknown path"}})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        payload = self._read_json()
        if payload is None:
            return
        if path == "/api/plan":
            self._guard(self.service.plan, payload)
            return
        if path == "/api/execute":
            self._guard(self.service.execute, payload)
            return
        self._send_json(404, {"ok": False, "error": {"code": "not_found", "message": "unknown path"}})

    # -- helpers -------------------------------------------------------

    def _guard(self, handler, payload: dict | None = None) -> None:
        try:
            result = handler() if payload is None else handler(payload)
        except ApiError as exc:
            self._send_json(exc.status, {"ok": False, "error": exc.to_dict()})
            return
        except Exception as exc:  # pragma: no cover - defensive
            self._send_json(
                500,
                {
                    "ok": False,
                    "error": {
                        "code": "internal_error",
                        "message": f"{type(exc).__name__} while serving the request",
                    },
                },
            )
            return
        self._send_json(200, result)

    def _read_json(self) -> dict | None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._send_json(
                400,
                {
                    "ok": False,
                    "error": {"code": "invalid_body", "message": "invalid Content-Length"},
                },
            )
            return None
        if length < 0 or length > _MAX_BODY_BYTES:
            self._send_json(
                413,
                {
                    "ok": False,
                    "error": {"code": "body_too_large", "message": "request body is too large"},
                },
            )
            return None
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(
                400,
                {
                    "ok": False,
                    "error": {"code": "invalid_body", "message": "body must be JSON"},
                },
            )
            return None
        if not isinstance(data, dict):
            self._send_json(
                400,
                {
                    "ok": False,
                    "error": {"code": "invalid_body", "message": "body must be a JSON object"},
                },
            )
            return None
        return data

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _send_static(self, name: str) -> None:
        target = (STATIC_DIR / name).resolve()
        try:
            target.relative_to(STATIC_DIR)
        except ValueError:
            self._send_json(
                403,
                {
                    "ok": False,
                    "error": {"code": "forbidden", "message": "invalid asset path"},
                },
            )
            return
        if not target.is_file():
            self._send_json(
                404,
                {
                    "ok": False,
                    "error": {"code": "not_found", "message": "unknown asset"},
                },
            )
            return
        body = target.read_bytes()
        content_type, _ = mimetypes.guess_type(target.name)
        self.send_response(200)
        self.send_header(
            "Content-Type",
            content_type or "application/octet-stream",
        )
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)


def create_server(
    allowed_root: Path, *, host: str = "127.0.0.1", port: int = 8765
) -> ThreadingHTTPServer:
    """Create a ready-to-serve dashboard HTTP server."""

    service = build_service(allowed_root)
    handler = type(
        "BoundDashboardHandler", (DashboardHandler,), {"service": service}
    )
    return ThreadingHTTPServer((host, port), handler)


def serve(
    allowed_root: Path, *, host: str = "127.0.0.1", port: int = 8765
) -> None:  # pragma: no cover - manual entry point
    """Run the dashboard server until interrupted."""

    server = create_server(allowed_root, host=host, port=port)
    print(f"ContractBreaker dashboard on http://{host}:{port}")
    print(f"Allowed source root: {Path(allowed_root).resolve()}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
