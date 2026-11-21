"""Operator-configured, read-only checks for artifacts and application outcomes."""
from contextlib import closing
import io
import json
from pathlib import Path
import sqlite3
import time
import urllib.request

from ..verification import FileVerifier, MAX_TEXT_BYTES, _json_pointer
from ..adapters.browser import _origin
from ..policy import fingerprint


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("Verifier redirects are disabled")


class ConfiguredVerifier(FileVerifier):
    verification_level = "custom_verifier"
    verification_source = "configured_checks"

    def __init__(self, rules, root, *, allowed_http_origins=()):
        rules = json.loads(json.dumps(rules, allow_nan=False))
        super().__init__(rules, Path(root))
        self.allowed_origins = {_origin(url) for url in allowed_http_origins}
        self.identity = fingerprint([self.identity, sorted(self.allowed_origins)])
        self.backend = None
        for rule in rules.values():
            if not isinstance(rule, dict) or rule.get("kind", "file") not in {
                    "file", "dom", "http_json", "sqlite", "app_state", "screenshot_region"}:
                raise ValueError("Unknown configured verifier kind")
            if rule.get("kind", "file") != "file" and rule.get("fresh"):
                raise ValueError("Freshness baselines apply only to file rules")

    def bind_backend(self, backend):
        self.backend = backend

    def _page(self):
        page = getattr(self.backend, "page", None)
        if page is None:
            raise ValueError("This check requires an active browser backend")
        return page

    def _http_document(self, rule):
        url = rule["url"]
        if _origin(url) not in self.allowed_origins:
            raise ValueError("HTTP verifier origin is not allowlisted")
        opener = urllib.request.build_opener(NoRedirect())
        request = urllib.request.Request(url, method="GET", headers={"Accept": "application/json"})
        with opener.open(request, timeout=5) as response:
            if response.status != rule.get("status", 200):
                raise ValueError("Unexpected HTTP status")
            payload = response.read(MAX_TEXT_BYTES + 1)
        if len(payload) > MAX_TEXT_BYTES:
            raise ValueError("HTTP verifier response is too large")
        return json.loads(payload)

    def _sqlite_rows(self, rule):
        path = self._path(rule)
        if not path.is_file(): raise ValueError("Missing database")
        query = rule["query"]
        if not isinstance(query, str) or not query.lstrip().upper().startswith("SELECT "):
            raise ValueError("Only SELECT verification queries are supported")
        deadline = time.monotonic() + 2
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)) as database:
            database.set_authorizer(lambda action, *args: sqlite3.SQLITE_OK
                                    if action in {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ}
                                    else sqlite3.SQLITE_DENY)
            database.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            rows = database.execute(query, rule.get("parameters", [])).fetchmany(1001)
        if len(rows) > 1000: raise ValueError("Database verifier result exceeds row limit")
        return [list(row) for row in rows]

    def _screenshot(self, rule):
        try:
            from PIL import Image, ImageChops
        except ImportError as exc:
            raise ValueError("Install praxis-agent-runtime[visual] for screenshot checks") from exc
        box = rule["region"]
        if (not isinstance(box, list) or len(box) != 4 or any(type(x) is not int or x < 0 for x in box)
                or box[2] <= box[0] or box[3] <= box[1]):
            raise ValueError("Region must be [left, top, right, bottom]")
        expected_path = self._path({"path": rule["baseline"]})
        payload = self._page().screenshot(type="png")
        if len(payload) > 20_000_000: raise ValueError("Screenshot exceeds limit")
        with Image.open(io.BytesIO(payload)) as current, Image.open(expected_path) as expected:
            if current.width * current.height > 20_000_000 or expected.width * expected.height > 20_000_000:
                raise ValueError("Image dimensions exceed limit")
            if any(box[2] > img.width or box[3] > img.height for img in (current, expected)):
                raise ValueError("Region extends outside screenshot or baseline")
            difference = ImageChops.difference(current.convert("RGB").crop(box), expected.convert("RGB").crop(box))
            pixels = difference.tobytes()
            changed = sum(any(pixels[i:i+3]) for i in range(0, len(pixels), 3))
            fraction = changed / ((box[2]-box[0]) * (box[3]-box[1]))
        tolerance = rule.get("max_changed_fraction", 0)
        if type(tolerance) not in (int, float) or not 0 <= tolerance <= 1:
            raise ValueError("Invalid screenshot tolerance")
        return fraction <= tolerance, {"changed_fraction": fraction}

    def _check_rule(self, rule, baseline, checkpoint_id):
        kind = rule.get("kind", "file")
        if kind == "file": return super()._check_rule(rule, baseline, checkpoint_id)
        try:
            evidence = {"kind": kind}
            if kind == "dom":
                locator = self._page().get_by_role(rule["role"], name=rule["name"], exact=True)
                passed = locator.count() == 1 and locator.is_visible()
                if passed and "text" in rule: passed = locator.inner_text(timeout=2000) == rule["text"]
                if passed and "value" in rule: passed = locator.input_value(timeout=2000) == rule["value"]
            elif kind in {"http_json", "app_state"}:
                document = self._http_document(rule) if kind == "http_json" else self.backend.verification_state()
                fields = rule["json_fields"]
                if not isinstance(fields, dict) or not fields: raise ValueError("Expected JSON fields required")
                passed = all(json.dumps(_json_pointer(document, key), sort_keys=True, allow_nan=False)
                             == json.dumps(value, sort_keys=True, allow_nan=False) for key, value in fields.items())
                evidence["document_sha256"] = fingerprint(document)
            elif kind == "sqlite":
                rows = self._sqlite_rows(rule)
                passed = rows == rule["rows"]
                evidence["row_count"] = len(rows)
                evidence["result_sha256"] = fingerprint(rows)
            elif kind == "screenshot_region":
                passed, detail = self._screenshot(rule)
                evidence.update(detail)
            else:
                return "unknown_verifier", evidence
            return ("passed" if passed else "check_mismatch"), evidence
        except Exception as exc:
            return "check_error", {"kind": kind, "error_type": type(exc).__name__}


def load_verifier(config):
    config = Path(config).resolve(strict=True)
    data = json.loads(config.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("criteria"), dict) or not data["criteria"]:
        raise ValueError("Verifier JSON must contain nonempty criteria")
    if any(isinstance(rule, dict) and rule.get("kind", "file") != "file" for rule in data["criteria"].values()):
        return ConfiguredVerifier(data["criteria"], config.parent / data.get("root", "."),
                                  allowed_http_origins=data.get("allowed_http_origins", []))
    return FileVerifier.from_json(config)
