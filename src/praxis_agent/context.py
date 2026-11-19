"""Bounded context packing while preserving goals and acceptance criteria."""
import json
class ContextBudgetError(ValueError): pass
def pack_context(state, observation, *, max_chars=12000, recent_events=8):
    if type(max_chars) is not int or max_chars < 500: raise ContextBudgetError("max_chars must be >=500")
    if type(recent_events) is not int or not 1 <= recent_events <= 50: raise ContextBudgetError("recent_events must be 1..50")
    core={"goal":str(state.get("goal",""))[:2000],"criteria":list(state.get("criteria",[]))[:20],"status":state.get("status"),"steps":state.get("steps")}
    obs={}
    for key in ("summary","window","active_window","ui_tree","elements","tools"):
        if key in observation:
            value=observation[key]
            obs[key]=value if key=="tools" and isinstance(value,list) else str(value)[:2500]
    payload={"task":core,"observation":obs,"recent_events":state.get("history",[])[-recent_events:]}
    if len(json.dumps(payload,ensure_ascii=False)) <= max_chars: return payload
    payload["observation"]={k:v for k,v in obs.items() if k not in {"ui_tree","elements"}}
    if len(json.dumps(payload,ensure_ascii=False)) <= max_chars: return payload
    payload["recent_events"]=[]
    if len(json.dumps(payload,ensure_ascii=False)) <= max_chars: return payload
    raise ContextBudgetError("Goal and acceptance criteria exceed context budget")
