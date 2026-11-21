"""Nova stdio MCP transport."""
from __future__ import annotations
import json, queue, subprocess, threading, re, time
from typing import Any
class StdioMCP:
    def __init__(self, command):
        if not isinstance(command, list) or not command or any(not isinstance(value, str) or not value for value in command):
            raise ValueError("Explicit MCP command argv required")
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.responses: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self.next_id = 0
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()
        try:
            self._initialize()
        except Exception:
            self.close()
            raise

    def _initialize(self):
        initialization = self._rpc("initialize", {"protocolVersion": "2025-03-26",
                                 "capabilities": {}, "clientInfo": {"name": "praxis-agent-runtime", "version": "0.1"}})
        server = initialization.get("serverInfo") if isinstance(initialization, dict) else None
        version = server.get("version") if isinstance(server, dict) else None
        self.backend_version = version[:128] if isinstance(version, str) else None
        self._notify("notifications/initialized", {})
        self.tools = []
        cursor = None
        seen = set()
        while True:
            page = self._rpc("tools/list", {"cursor": cursor} if cursor else {})
            self.tools.extend(page.get("tools", []))
            cursor = page.get("nextCursor")
            if not cursor:
                break
            if cursor in seen or len(seen) >= 100:
                raise ValueError("Invalid MCP tool pagination")
            seen.add(cursor)
        self.names = {tool["name"] for tool in self.tools}

    def _read(self):
        assert self.process.stdout is not None
        try:
            for line in self.process.stdout:
                try:
                    self.responses.put(json.loads(line))
                except (ValueError, UnicodeDecodeError):
                    continue
        except (OSError, ValueError):
            pass
        finally:
            self.responses.put(None)  # Wake a pending RPC immediately on EOF.

    def _notify(self, method, params):
        assert self.process.stdin is not None
        self.process.stdin.write((json.dumps({"jsonrpc": "2.0", "method": method,
                                              "params": params}) + "\n").encode())
        self.process.stdin.flush()

    def _rpc(self, method, params):
        self.next_id += 1
        ident = self.next_id
        assert self.process.stdin is not None
        self.process.stdin.write((json.dumps({"jsonrpc": "2.0", "id": ident,
                                              "method": method, "params": params}) + "\n").encode())
        self.process.stdin.flush()
        deadline = time.monotonic() + 30
        while True:
            if self.process.poll() is not None and self.responses.empty():
                raise RuntimeError("MCP process exited before responding")
            try:
                response = self.responses.get(timeout=max(0.01, deadline - time.monotonic()))
            except queue.Empty as exc:
                raise TimeoutError(f"MCP timeout: {method}") from exc
            if response is None:
                raise RuntimeError("MCP stream closed before responding")
            if time.monotonic() > deadline:
                raise TimeoutError(f"MCP timeout: {method}")
            if not isinstance(response, dict) or response.get("id") != ident:
                continue
            if "error" in response:
                raise RuntimeError("MCP server returned an error")
            result = response.get("result")
            if not isinstance(result, dict):
                raise ValueError("Invalid MCP result object")
            return result

    def call(self, name, arguments):
        if name not in self.names:
            raise ValueError(f"Unknown MCP tool: {name}")
        return self._rpc("tools/call", {"name": name, "arguments": arguments})

    def observe(self):
        # Automatic observation is restricted to advertised, explicitly read-only
        # tools with no mandatory arguments. Other Nova versions retain manual discovery.
        candidates = [tool for tool in self.tools
                      if tool.get("annotations", {}).get("readOnlyHint") is True
                      and not tool.get("inputSchema", {}).get("required")
                      and re.search(r"observe|screenshot|snapshot|accessibility|window.*list", tool["name"], re.I)]
        if not candidates:
            return {"summary": "Tools available; discover a read-only observation action first", "tools": self.tools}
        tool = candidates[0]
        result = self.call(tool["name"], {})
        if result.get("isError"):
            raise RuntimeError("Nova observation failed")
        content = result.get("content", [])
        text = "\n".join(item.get("text", "") for item in content if item.get("type") == "text")[:5000]
        from ..policy import fingerprint
        return {"summary": "Nova desktop observed", "ui_tree": text, "content": content,
                "action_scope": fingerprint({"backend": self.backend_version, "view": content}),
                "tools": self.tools}

    def act(self, action):
        result = self.call(action["name"], action.get("arguments", {}))
        return {"ok": not result.get("isError", False),
                "summary": "MCP call completed" if not result.get("isError") else "MCP tool error",
                "content": result.get("content", [])}

    def close(self):
        if getattr(self, "_closed", False):
            return
        self._closed = True
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        for stream in (self.process.stdin, self.process.stdout):
            if stream is not None:
                stream.close()
        if self._reader is not threading.current_thread():
            self._reader.join(timeout=1)


class NovaMCP(StdioMCP):
    def __init__(self, executable: str):
        super().__init__([executable])
