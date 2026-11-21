"""Observational hint exposure tracking; correlation is not causal credit."""
from pathlib import Path
from .policy import fingerprint
from .state import load


def source_identity(store):
    path = getattr(store, "path", None)
    return fingerprint(str(Path(path).resolve()) if path is not None else type(store).__name__)


def descriptors(kind, entries, environment, store):
    return [{"kind": kind, "id": str(entry["id"])[:128], "environment": environment[:200],
             "source_store": source_identity(store)} for entry in entries]


def hint_outcomes(directory, *, limit=1000):
    directory = Path(directory)
    if not directory.is_dir() or type(limit) is not int or not 1 <= limit <= 10000:
        raise ValueError("Checkpoint directory and limit 1..10000 required")
    groups, skipped, seen = {}, [], set()
    paths = sorted(directory.glob("*.json"))
    for path in paths[:limit]:
        try: state = load(path)
        except (OSError, ValueError, TypeError):
            skipped.append(path.name)
            continue
        for hint in state.get("hint_exposures", []):
            if not isinstance(hint, dict) or hint.get("kind") not in {"experience", "strategy"}: continue
            key = fingerprint([hint.get(k) for k in ("kind", "id", "environment", "source_store")])
            if (key, state["id"]) in seen: continue
            seen.add((key, state["id"]))
            group = groups.setdefault(key, {k: hint.get(k) for k in ("kind", "id", "environment", "source_store")})
            group.setdefault("runs", [])
            verified = (state["status"] == "completed" and state.get("verification_level") in
                        {"independent_artifact", "custom_verifier"}
                        and (state.get("artifact_verification") or {}).get("passed") is True)
            group["runs"].append({"checkpoint_id": state["id"], "status": state["status"],
                                  "recorded_verified": verified,
                                  "model_calls": state.get("model_usage", {}).get("calls", 0)})
    for group in groups.values():
        runs = group["runs"]
        group["distinct_runs"] = len(runs)
        group["verified_completions"] = sum(row["recorded_verified"] for row in runs)
        group["blocked_or_failed"] = sum(row["status"] in {"blocked", "failed"} for row in runs)
        group["inconclusive_runs"] = len(runs)-group["verified_completions"]-group["blocked_or_failed"]
        group["recorded_verified_completion_rate"] = group["verified_completions"]/len(runs)
    return {"schema_version": 1, "kind": "hint-outcome-cooccurrence", "causal_attribution": False,
            "groups": [groups[key] for key in sorted(groups)], "skipped_files": skipped,
            "truncated_input": len(paths) > limit,
            "note": "Exposure means supplied as model context, not followed or causally effective. Use paired benchmarks for efficacy."}
