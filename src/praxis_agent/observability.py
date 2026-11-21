"""Allowlisted append-only operational events; excludes prompts, screenshots and secrets."""
import json, os, time, math
from collections import deque
from pathlib import Path
_ALLOWED={"run_started","task_started","task_completed","task_failed","verification","approval_required","checkpoint_resumed","run_paused","run_completed","runtime_event"}
class EventJournal:
    def __init__(self,path): self.path=Path(path)
    def emit(self,event,*,run_id,task_id=None,status=None,duration_ms=None,counters=None,sequence=None):
        if event not in _ALLOWED: raise ValueError("Unsupported event")
        if not isinstance(run_id,str) or not run_id or len(run_id)>128: raise ValueError("Invalid run_id")
        if task_id is not None and (not isinstance(task_id,str) or len(task_id)>128): raise ValueError("Invalid task_id")
        if duration_ms is not None and (type(duration_ms) not in (int,float) or not math.isfinite(duration_ms) or duration_ms<0): raise ValueError("Invalid duration")
        if sequence is not None and (type(sequence) is not int or sequence < 1): raise ValueError("Invalid sequence")
        safe={}
        for key,value in (counters or {}).items():
            if key not in {"model_calls","input_tokens","output_tokens","tool_calls","retries"} or type(value) is not int or value<0: raise ValueError("Invalid metric")
            safe[key]=value
        record={"time":time.time(),"event":event,"run_id":run_id,"task_id":task_id,"status":status,"duration_ms":duration_ms,"counters":safe,"sequence":sequence}
        self.path.parent.mkdir(parents=True,exist_ok=True)
        fd=os.open(self.path,os.O_CREAT|os.O_APPEND|os.O_WRONLY,0o600)
        try: os.write(fd,(json.dumps(record,ensure_ascii=False,separators=(",",":"))+"\n").encode())
        finally: os.close(fd)
        return record
    def read(self,*,limit=1000):
        if type(limit) is not int or not 1<=limit<=10000: raise ValueError("limit must be 1..10000")
        if not self.path.exists(): return []
        records = deque(maxlen=limit)
        with self.path.open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    item = json.loads(line)
                    if isinstance(item, dict): records.append(item)
                except json.JSONDecodeError: continue
        unique = {}
        for index, item in enumerate(records):
            key = (item.get("run_id"), item["sequence"]) if item.get("sequence") is not None else ("legacy", index)
            unique[key] = item
        return list(unique.values())
