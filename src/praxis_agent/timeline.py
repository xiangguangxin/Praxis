"""Read-only, payload-free checkpoint and journal inspection."""
from datetime import datetime, timezone
from html import escape
import json
from pathlib import Path
from .diagnostics import safe_summary
from .observability import EventJournal
from .state import load


def timeline(checkpoint, *, journal=None):
    state = load(Path(checkpoint))
    events = {}
    journal_path = journal or state.get("event_journal")
    if journal_path:
        for item in EventJournal(journal_path).read(limit=10000):
            if item.get("run_id") == state["id"] and item.get("sequence") is not None:
                events[item["sequence"]] = {"time": item["time"], "event": item.get("status", item["event"]),
                                             "sequence": item["sequence"], "detail": ""}
    for index, item in enumerate(state["history"]):
        events[item.get("sequence", index)] = {key: item.get(key) for key in ("time", "event", "sequence", "detail")}
    safe_events = []
    for _, item in sorted(events.items()):
        event = safe_summary(item["event"], 80)
        category = ("actions" if event == "action" else "planning" if "plan" in event or "model" in event else
                    "checks" if "verif" in event or "acceptance" in event or event == "completed" else
                    "recovery" if any(word in event for word in ("reconcil", "retry", "paus", "resum", "error")) else "observation")
        safe_events.append({"time": item["time"], "sequence": item.get("sequence"), "event": event,
                            "category": category, "detail": safe_summary(item.get("detail", ""), 200)})
    trace = state.get("tool_trace", {})
    task_data = state.get("task_runtime", {})
    sequences = [e["sequence"] for e in safe_events if type(e.get("sequence")) is int]
    partial = bool(sequences and (sequences[0] != 1 or len(set(sequences)) != sequences[-1]))
    return {"schema_version": 1, "read_only": True, "run_id": state["id"], "status": state["status"],
            "stop_reason": safe_summary(state.get("stop_reason", "")), "steps": state["steps"],
            "verification_level": state.get("verification_level"), "elapsed_run_ms": state.get("elapsed_run_ms", 0),
            "model_usage": state.get("model_usage", {}), "events": safe_events,
            "partial_history": partial or bool(state.get("journal_error")),
            "journal_error": state.get("journal_error"),
            "tasks": {safe_summary(key, 80): value for key, value in task_data.get("graph", {}).get("status", {}).items()},
            "models": state.get("model_requests", []),
            "tools": [{"tool": safe_summary(item.get("tool", ""), 128), "outcome": item.get("outcome"),
                       "duration_ms": item.get("duration_ms")} for item in trace.get("events", [])],
            "trace_dropped": trace.get("dropped", 0)}


def write_timeline(checkpoint, output, *, journal=None):
    report = timeline(checkpoint, journal=journal)
    output = Path(output)
    if output.suffix == ".json":
        with output.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(report, ensure_ascii=False, indent=2))
    elif output.suffix == ".html":
        rows = []
        for event in report["events"]:
            stamp = datetime.fromtimestamp(event["time"], timezone.utc).strftime("%H:%M:%S.%f")[:-3]
            rows.append('<tr data-category="'+event["category"]+'"><td>'+stamp+'</td><td>'+escape(event["event"])+
                        '</td><td>'+escape(event["detail"])+'</td></tr>')
        panels = "".join('<section><h2>'+escape(name)+'</h2><pre>'+escape(json.dumps(report[key], ensure_ascii=False, indent=2))+"</pre></section>"
                         for name, key in [("Model usage", "model_usage"), ("Task states", "tasks"),
                                           ("Model requests", "models"), ("Tool outcomes", "tools")])
        html = '''<!doctype html><html lang="en"><meta charset="utf-8"><title>Praxis run timeline</title>
<style>body{font:15px system-ui;background:#f5f6fa;color:#17243b;margin:32px auto;max-width:1200px}header,section{background:white;border:1px solid #dce2ec;border-radius:12px;padding:24px;margin:16px 0}h1{margin:0}small{color:#52627b}input,select{padding:10px;font:inherit;margin:12px 12px 12px 0}table{border-collapse:collapse;width:100%;text-align:left}td,th{padding:12px;border-bottom:1px solid #edf0f5}pre{white-space:pre-wrap;overflow-wrap:anywhere}.panels{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:16px}</style>
<header><h1>Praxis run timeline</h1><p>''' + escape(report["run_id"]) + " · " + escape(report["status"]) + " · " + escape(report["verification_level"] or "unverified") + '''</p><small>Read-only inspection. No tools or model calls are replayed.</small></header>
<section><h2>Events</h2><input id="search" placeholder="Filter events" aria-label="Filter events"><select id="category" aria-label="Event category"><option value="all">All events</option><option>actions</option><option>planning</option><option>checks</option><option>recovery</option><option>observation</option></select>'''
        if report["partial_history"] or report["trace_dropped"]:
            html += '<p>Partial retained history; earlier events or tool trace entries are unavailable.</p>'
        html += '<table><thead><tr><th>Time (UTC)</th><th>Event</th><th>Detail</th></tr></thead><tbody>'+"".join(rows)+'</tbody></table></section><div class="panels">'+panels+'''</div>
<script>function filter(){const q=document.querySelector('#search').value.toLowerCase(),c=document.querySelector('#category').value;document.querySelectorAll('tbody tr').forEach(r=>r.hidden=!(r.textContent.toLowerCase().includes(q)&&(c==='all'||r.dataset.category===c)));}document.querySelector('#search').oninput=filter;document.querySelector('#category').onchange=filter;</script></html>'''
        with output.open("x", encoding="utf-8") as stream:
            stream.write(html)
    else:
        raise ValueError("Timeline output must end with .json or .html")
    return {"output": str(output), "events": len(report["events"]), "read_only": True,
            "partial_history": report["partial_history"]}
