"""Composable, operator-configured file, HTTP, command and stdio MCP tools."""
from contextlib import contextmanager, ExitStack
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import urllib.request
from urllib.parse import urlencode, urlsplit, urlunsplit, parse_qsl

from .browser import _origin
from ..policy import fingerprint
from ..storage import atomic_json_write
from ..tools import ToolRegistry, ToolSpec, RegisteredToolsBackend
from ..verifiers.configured import NoRedirect

MAX_BYTES = 1_000_000


def config_data(path):
    path = Path(path).resolve(strict=True)
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or set(data)-{"root", "files", "http", "commands", "mcp"}:
        raise ValueError("Unknown tool configuration fields")
    root = (path.parent / data.get("root", ".")).resolve(strict=True)
    if not root.is_dir(): raise ValueError("Tool root must be a directory")
    return data, root


def config_identity(path):
    data, root = config_data(path)
    return fingerprint([str(Path(path).resolve()), str(root), data])


def _inside(root, name):
    relative = Path(name)
    if relative.anchor or ".." in relative.parts or not name:
        raise ValueError("Tool path must remain within configured root")
    path = (root / relative).resolve()
    if path == root or not path.is_relative_to(root): raise ValueError("Tool path escapes configured root")
    return path


def _text_result(text, summary):
    return {"ok": True, "summary": summary, "content": [{"type": "text", "text": text[:MAX_BYTES]}]}


def _file_tools(registry, root, config):
    if not isinstance(config, dict) or set(config)-{"read", "write_json"}: raise ValueError("Invalid file tool settings")
    def read(args):
        path = _inside(root, args["path"])
        with path.open("rb") as stream: payload = stream.read(MAX_BYTES+1)
        if len(payload) > MAX_BYTES: raise ValueError("File exceeds tool read limit")
        return _text_result(payload.decode("utf-8"), "File read completed")
    def write(args):
        path = _inside(root, args["path"])
        if len(json.dumps(args["data"], ensure_ascii=False).encode()) > MAX_BYTES: raise ValueError("JSON exceeds write limit")
        atomic_json_write(path, args["data"], create_only=True)
        return {"ok": True, "summary": "New JSON artifact created"}
    if config.get("read") is True:
        def preflight_read(args):
            path = _inside(root, args["path"])
            if not path.is_file() or path.stat().st_size > MAX_BYTES:
                raise ValueError("Expected an existing bounded text file")
        registry.register(ToolSpec("file_read_text", read, {"path": str}, impact="low", idempotent=True,
                                   description="Read bounded UTF-8 text inside the configured root.",
                                   preflight=preflight_read))
    if config.get("write_json") is True:
        def preflight(args):
            path = _inside(root, args["path"])
            if path.exists() or path.is_symlink(): raise ValueError("Output already exists")
            if len(json.dumps(args["data"], ensure_ascii=False, allow_nan=False).encode()) > MAX_BYTES:
                raise ValueError("JSON exceeds write limit")
        registry.register(ToolSpec("file_write_json", write, {"path": str, "data": dict},
                                   description="Create a new JSON artifact without replacing files.", preflight=preflight))


def _http_tools(registry, config):
    if not isinstance(config, dict) or set(config)-{"allowed_origins", "tools"}: raise ValueError("Invalid HTTP settings")
    origins = {_origin(url) for url in config.get("allowed_origins", [])}
    for item in config.get("tools", []):
        if not isinstance(item, dict) or set(item)-{"name", "method", "url", "inputSchema", "headers_env", "description"}:
            raise ValueError("Invalid HTTP tool fields")
        method = item.get("method", "GET")
        if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"} or _origin(item["url"]) not in origins:
            raise ValueError("HTTP operation must use an allowlisted origin and supported method")
        def build_request(args, item=item, method=method):
            headers = {"Accept": "application/json"}
            for name, variable in item.get("headers_env", {}).items():
                if not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]*", name) or not isinstance(variable, str) or not variable.isidentifier():
                    raise ValueError("Invalid HTTP credential header mapping")
                value = os.environ.get(variable)
                if value is None: raise ValueError("Missing HTTP credential environment variable")
                headers[name] = value
            url, payload = item["url"], None
            if method == "GET":
                if any(type(value) not in (str, int, float, bool) for value in args.values()):
                    raise ValueError("GET arguments must be scalar query values")
                parts = urlsplit(url)
                url = urlunsplit((parts.scheme, parts.netloc, parts.path,
                                 urlencode(parse_qsl(parts.query)+list(args.items())), ""))
            else:
                payload = json.dumps(args, allow_nan=False).encode()
                if len(payload) > MAX_BYTES: raise ValueError("HTTP payload exceeds limit")
                headers["Content-Type"] = "application/json"
            req = urllib.request.Request(url, data=payload, headers=headers, method=method)
            return req
        def request(args, build=build_request):
            req = build(args)
            with urllib.request.build_opener(NoRedirect()).open(req, timeout=15) as response:
                body = response.read(MAX_BYTES+1)
            if len(body) > MAX_BYTES: raise ValueError("HTTP response exceeds limit")
            return _text_result(body.decode("utf-8"), "HTTP operation completed")
        registry.register(ToolSpec(item["name"], request, {}, impact="low" if method == "GET" else "high",
            idempotent=method == "GET", input_schema=item.get("inputSchema", {"type": "object", "properties": {}, "additionalProperties": False}),
            description=item.get("description", "Configured HTTP operation."), preflight=build_request))


def _command_tools(registry, root, commands):
    if not isinstance(commands, list): raise ValueError("Commands must be a list")
    for item in commands:
        if not isinstance(item, dict) or set(item)-{"name", "argv", "timeout_seconds", "description"}:
            raise ValueError("Invalid command tool fields")
        argv = item["argv"]
        if not isinstance(argv, list) or not argv or any(not isinstance(value, str) or "\x00" in value for value in argv):
            raise ValueError("Fixed command argv required")
        executable = shutil.which(argv[0])
        if not executable: raise ValueError("Configured command executable is unavailable")
        argv = [str(Path(executable).resolve()), *argv[1:]]
        timeout = item.get("timeout_seconds", 30)
        if type(timeout) not in (int, float) or not 0 < timeout <= 300: raise ValueError("Command timeout must be 0..300 seconds")
        def execute(args, argv=argv, timeout=timeout):
            with tempfile.TemporaryFile() as output:
                result = subprocess.run(argv, cwd=root, stdin=subprocess.DEVNULL, stdout=output,
                                        stderr=subprocess.STDOUT, timeout=timeout, shell=False)
                output.seek(0)
                data = output.read(MAX_BYTES+1)
            text = data[:MAX_BYTES].decode("utf-8", errors="replace")
            return {"ok": result.returncode == 0, "summary": f"Command exited {result.returncode}",
                    "content": [{"type": "text", "text": text}], "output_truncated": len(data) > MAX_BYTES}
        registry.register(ToolSpec(item["name"], execute, {}, timeout_seconds=timeout,
                                   description=item.get("description", "Run the operator's fixed argv command.")))


@contextmanager
def open_tool_backend(path, *, expected_identity=None):
    data, root = config_data(path)
    identity = fingerprint([str(Path(path).resolve()), str(root), data])
    if expected_identity is not None and identity != expected_identity:
        raise ValueError("Tool configuration changed before backend startup")
    registry = ToolRegistry()
    versions = {}
    with ExitStack() as stack:
        if "files" in data: _file_tools(registry, root, data["files"])
        if "http" in data: _http_tools(registry, data["http"])
        _command_tools(registry, root, data.get("commands", []))
        for item in data.get("mcp", []):
            if not isinstance(item, dict) or set(item)-{"name", "command", "tools", "readonly_tools"}:
                raise ValueError("Invalid MCP tool settings")
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", item["name"]): raise ValueError("Invalid MCP namespace")
            allowed, readonly = item.get("tools", []), item.get("readonly_tools", [])
            if (not isinstance(allowed, list) or not allowed or not isinstance(readonly, list)
                    or not set(readonly).issubset(allowed)):
                raise ValueError("Explicit MCP tool allowlist required")
            from .nova import StdioMCP
            client = StdioMCP(item["command"])
            stack.callback(client.close)
            versions[item["name"]] = client.backend_version
            advertised = {tool["name"]: tool for tool in client.tools}
            if not set(allowed).issubset(advertised): raise ValueError("Allowlisted MCP tools were not advertised")
            for name in allowed:
                def call(args, name=name, client=client, readonly=readonly):
                    result = client.call(name, args)
                    if result.get("isError") and name not in readonly:
                        raise RuntimeError("MCP side-effect outcome requires reconciliation")
                    return {"ok": not result.get("isError", False), "summary": "MCP operation returned",
                            "content": result.get("content", [])}
                registry.register(ToolSpec("mcp."+item["name"]+"."+name, call, {},
                    impact="low" if name in readonly else "high", input_schema=advertised[name].get("inputSchema", {}),
                    description=str(advertised[name].get("description", "Configured MCP operation"))[:500]))
        if not registry.describe(): raise ValueError("Tool configuration exposes no tools")
        scope = fingerprint([identity, registry.describe(), versions])
        yield RegisteredToolsBackend(registry, lambda: {"summary": "Configured tool workspace ready",
                                                       "action_scope": scope})
