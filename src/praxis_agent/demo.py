"""Reproducible real-browser demonstration with a deterministic fixture planner.

This exercises Chromium, downloads, runtime recovery primitives and file verification.
It does not measure model intelligence or general desktop autonomy.
"""
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from threading import Thread
from .adapters.browser import open_browser
from .agent import run
from .state import create
from .session import browser_scope
from .verification import FileVerifier
from .benchmark_provenance import package_version

FIXTURE = '''<!doctype html><html lang="en"><meta charset="utf-8"><title>Praxis demo</title>
<style>body{font:18px system-ui;max-width:760px;margin:80px auto;background:#f4f7fb;color:#18314f}[hidden]{display:none!important}button,a{padding:14px;margin:8px;display:inline-block}h1{color:#245ec9}</style>
<h1>Praxis · SIL coverage fixture</h1><button id="build">Build instrumented fixture</button>
<button id="test" disabled>Run SIL cases</button><p role="status" id="status">Ready</p>
<a id="report" download="coverage.json" hidden>Download MC/DC report</a>
<script>const b=document.getElementById('build'),t=document.getElementById('test'),s=document.getElementById('status'),r=document.getElementById('report');
b.onclick=()=>{b.disabled=true;t.disabled=false;s.textContent='Instrumented build ready'};
t.onclick=()=>{t.disabled=true;s.textContent='Running SIL cases';setTimeout(()=>{r.href=URL.createObjectURL(new Blob([JSON.stringify({suite:'example-sil',build:'instrumented',tests:'passed',metric:'MC/DC'})],{type:'application/json'}));r.hidden=false;s.textContent='SIL cases passed'},150)};</script></html>'''


class FixturePlanner:
    """Choose only from currently observed fixture states, not a step counter."""
    def decide(self, state, observation):
        tree = observation.get("ui_tree", "")
        if (state.get("last_result") or {}).get("summary") == "browser_download completed":
            return {"type": "complete"}
        if 'link "Download MC/DC report"' in tree:
            name, args = "browser_download", {"role": "link", "name": "Download MC/DC report", "filename": "coverage.json"}
        elif "SIL cases passed" in tree or "Running SIL cases" in tree:
            name, args = "browser_wait_for_text", {"text": "SIL cases passed"}
        elif "Instrumented build ready" in tree:
            name, args = "browser_click", {"role": "button", "name": "Run SIL cases"}
        else:
            name, args = "browser_click", {"role": "button", "name": "Build instrumented fixture"}
        return {"type": "act", "action": {"name": name, "arguments": args, "impact": "low"}}


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_): pass


def demo(output: Path, executable=None):
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    site = output / "fixture"
    site.mkdir()
    (site / "index.html").write_text(FIXTURE, encoding="utf-8")
    rules = {"Fresh MC/DC report": {"path": "coverage.json", "fresh": True, "json_fields": {
        "/suite": "example-sil", "/build": "instrumented", "/tests": "passed", "/metric": "MC/DC"}}}
    (output / "verifier.json").write_text(json.dumps({"criteria": rules}, indent=2), encoding="utf-8")
    checkpoint = output / "goal.json"
    create(checkpoint, "Build the local authorized SIL fixture, run tests and download coverage.json", list(rules), max_steps=8)
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=str(site)))
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/"
    try:
        with open_browser(url, output, executable) as browser:
            state = run(checkpoint, browser, FixturePlanner(), verifier=FileVerifier(rules, output),
                        backend_scope=browser_scope(url, output), trace_tools=True)
            browser.page.screenshot(path=str(output / "final.png"), full_page=True)
            version = browser.backend_version
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
    report = {"kind": "deterministic-browser-demo", "planner": "scripted-fixture-state-machine",
              "real_model": False, "status": state["status"], "verification_level": state["verification_level"],
              "actions": state["steps"], "checks": state.get("artifact_verification", {}),
              "browser_version": version, "playwright_version": package_version("playwright"),
              "trace": state.get("tool_trace"), "screenshot": "final.png"}
    (output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (output / "index.html").write_text('''<!doctype html><meta charset="utf-8"><title>Praxis demo result</title>
<h1>Praxis deterministic browser demonstration</h1>
<p>Scripted fixture planner · Real Chromium · Independent JSON artifact verification</p>
<p>This demonstrates integration, not real-model autonomy.</p>
<p><a href="report.json">Verification report</a> · <a href="coverage.json">Coverage artifact</a></p>
<img alt="Final fixture state" src="final.png" style="max-width:100%">''', encoding="utf-8")
    return report
