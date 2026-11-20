"""Deterministic, provenance-preserving failure mining and experience clustering.

No model-generated root-cause claims or external embedding service is used here.
Patterns summarize observed runtime signals; clusters summarize lexical similarity.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import time
from collections import Counter
from pathlib import Path

from .failure_analysis import analyze_failure
from .experience import _terms
from .state import load

MAX_CHECKPOINT_BYTES = 2 * 1024 * 1024
MAX_CHECKPOINTS = 10000
MAX_EXPERIENCES = 5000
_FAILURE_CATEGORIES = {
    "uncertain_action", "policy_denied", "verification_failed",
    "transient_backend_failure", "action_failed", "budget_exhausted",
}
_SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def _bounded_text(value, limit=160):
    return value[:limit] if isinstance(value, str) else ""


def _valid_analysis(value):
    if not isinstance(value, dict) or value.get("category") not in _FAILURE_CATEGORIES:
        return False
    return (value.get("severity") in _SEVERITY_ORDER
            and type(value.get("retryable")) is bool
            and type(value.get("requires_reconciliation")) is bool
            and isinstance(value.get("signals"), list))


def _incident(state):
    analysis = analyze_failure(state)
    if analysis is None:
        stored = state.get("failure_analysis")
        analysis = stored if _valid_analysis(stored) else None
    if not _valid_analysis(analysis):
        return None
    error = state.get("last_error") if isinstance(state.get("last_error"), dict) else {}
    result = state.get("last_result") if isinstance(state.get("last_result"), dict) else {}
    code = error.get("code") or result.get("code")
    phase = error.get("phase") or result.get("phase")
    failed_checks = sorted(
        _bounded_text(signal.removeprefix("failed_check:"), 140)
        for signal in analysis["signals"] if isinstance(signal, str)
        and signal.startswith("failed_check:")
    )
    return {
        "checkpoint_id": state["id"],
        "category": analysis["category"],
        "severity": analysis["severity"],
        "retryable": analysis["retryable"],
        "requires_reconciliation": analysis["requires_reconciliation"],
        "error_code": _bounded_text(code, 100),
        "phase": _bounded_text(phase, 60),
        "failed_checks": failed_checks[:10],
        "recommended_recovery": _bounded_text(analysis.get("recommended_recovery"), 240),
        "observed_at": state.get("updated_at") or state.get("created_at"),
    }


def _pattern_key(incident):
    signature = {
        "category": incident["category"],
        "error_code": incident["error_code"],
        "phase": incident["phase"],
        "failed_checks": incident["failed_checks"],
        "requires_reconciliation": incident["requires_reconciliation"],
    }
    canonical = json.dumps(signature, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def mine_failure_patterns(checkpoint_dir: Path, *, recursive: bool = False,
                          min_occurrences: int = 1, limit: int = 1000) -> dict:
    """Mine one current, trusted failure snapshot per checkpoint.

    Historical free-form event strings are deliberately not interpreted as root causes.
    """
    root = Path(checkpoint_dir)
    if not root.is_dir() or root.is_symlink():
        raise ValueError("checkpoint_dir must be an existing non-symlink directory")
    if type(min_occurrences) is not int or not 1 <= min_occurrences <= MAX_CHECKPOINTS:
        raise ValueError("min_occurrences must be between 1 and 10000")
    if type(limit) is not int or not 1 <= limit <= 5000:
        raise ValueError("limit must be between 1 and 5000")
    iterator = root.rglob("*.json") if recursive else root.glob("*.json")
    paths = sorted((path for path in iterator if not path.is_symlink()), key=lambda p: p.as_posix())
    if len(paths) > MAX_CHECKPOINTS:
        raise ValueError(f"checkpoint scan exceeds {MAX_CHECKPOINTS} files")
    incidents, skipped = [], 0
    for path in paths:
        try:
            if path.stat().st_size > MAX_CHECKPOINT_BYTES:
                skipped += 1
                continue
            state = load(path)
            incident = _incident(state)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            skipped += 1
            continue
        if incident is not None:
            incidents.append(incident)

    groups = {}
    for incident in incidents:
        key = _pattern_key(incident)
        group = groups.setdefault(key, {
            "pattern_id": key, "category": incident["category"],
            "severity": incident["severity"], "retryable": incident["retryable"],
            "requires_reconciliation": incident["requires_reconciliation"],
            "error_code": incident["error_code"], "phase": incident["phase"],
            "failed_checks": incident["failed_checks"],
            "recommended_recovery": incident["recommended_recovery"],
            "checkpoint_ids": [], "first_observed_at": None, "last_observed_at": None,
        })
        if incident["checkpoint_id"] not in group["checkpoint_ids"]:
            group["checkpoint_ids"].append(incident["checkpoint_id"])
        observed = incident["observed_at"]
        if type(observed) in (int, float) and math.isfinite(observed):
            group["first_observed_at"] = observed if group["first_observed_at"] is None else min(group["first_observed_at"], observed)
            group["last_observed_at"] = observed if group["last_observed_at"] is None else max(group["last_observed_at"], observed)

    patterns = []
    for group in groups.values():
        count = len(group["checkpoint_ids"])
        if count < min_occurrences:
            continue
        group["checkpoint_ids"].sort()
        group["occurrences"] = count
        group["recurrence"] = "repeated" if count >= 5 else "recurring" if count >= 2 else "single_observation"
        group["evidence_strength"] = "observed_signature_frequency_not_root_cause_confidence"
        patterns.append(group)
    patterns.sort(key=lambda p: (_SEVERITY_ORDER[p["severity"]], -p["occurrences"], p["pattern_id"]))
    return {
        "schema_version": 1, "analysis": "deterministic_trusted_runtime_signals",
        "generated_at": time.time(), "checkpoint_files": len(paths),
        "analyzed_failures": len(incidents), "skipped_checkpoints": skipped,
        "min_occurrences": min_occurrences, "patterns": patterns[:limit],
    }


def _experience_features(item):
    fields = (
        ("goal", 3.0), ("criteria", 2.0), ("key_steps", 2.0),
        ("preconditions", 1.0), ("failure_modes", 1.0),
    )
    weights = {}
    for field, weight in fields:
        value = item.get(field, "")
        if isinstance(value, str):
            values = [value]
        elif isinstance(value, list):
            values = [part for part in value if isinstance(part, str)]
        else:
            values = []
        for part in values:
            for term in _terms(part):
                weights[term] = max(weights.get(term, 0.0), weight)
    return weights


def _weighted_jaccard(left, right):
    union = left.keys() | right.keys()
    if not union:
        return 0.0
    numerator = sum(min(left.get(term, 0.0), right.get(term, 0.0)) for term in union)
    denominator = sum(max(left.get(term, 0.0), right.get(term, 0.0)) for term in union)
    return numerator / denominator if denominator else 0.0


def cluster_experiences(experiences, *, environment: str | None = None,
                        min_similarity: float = 0.35, include_singletons: bool = False,
                        limit: int = 500) -> dict:
    """Greedy deterministic complete-link lexical clustering of verified active items."""
    if isinstance(min_similarity, bool) or not isinstance(min_similarity, (int, float)) or not math.isfinite(min_similarity) or not 0.1 <= min_similarity <= 1.0:
        raise ValueError("min_similarity must be between 0.1 and 1.0")
    if type(include_singletons) is not bool or type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("Invalid clustering options")
    if not isinstance(experiences, list) or len(experiences) > MAX_EXPERIENCES:
        raise ValueError(f"experiences must be a list of at most {MAX_EXPERIENCES} items")
    eligible = []
    seen = set()
    for item in experiences:
        if not isinstance(item, dict):
            continue
        identity = item.get("id")
        env = item.get("environment")
        if (not isinstance(identity, str) or not identity or identity in seen
                or item.get("source") != "independently-verified"
                or item.get("status", "active") != "active"
                or not isinstance(env, str) or not env.strip()
                or (environment is not None and env != environment)):
            continue
        features = _experience_features(item)
        if not features:
            continue
        seen.add(identity)
        eligible.append({"id": identity, "environment": env,
                         "goal": _bounded_text(item.get("goal"), 1000),
                         "features": features})
    eligible.sort(key=lambda x: (x["environment"], x["id"]))
    clusters = []
    for item in eligible:
        candidates = []
        for index, cluster in enumerate(clusters):
            if cluster[0]["environment"] != item["environment"]:
                continue
            similarities = [_weighted_jaccard(item["features"], member["features"])
                            for member in cluster]
            score = min(similarities)
            if score >= min_similarity:
                candidates.append((score, index))
        if candidates:
            _, selected = max(candidates, key=lambda pair: (pair[0], -pair[1]))
            clusters[selected].append(item)
        else:
            clusters.append([item])

    output = []
    for members in clusters:
        if len(members) == 1 and not include_singletons:
            continue
        ids = sorted(member["id"] for member in members)
        env = members[0]["environment"]
        similarities = [
            _weighted_jaccard(members[i]["features"], members[j]["features"])
            for i in range(len(members)) for j in range(i + 1, len(members))
        ]
        common = set.intersection(*(set(member["features"]) for member in members))
        common_terms = sorted(common, key=lambda term: (-sum(member["features"][term] for member in members), term))[:12]
        digest = hashlib.sha256((env + "\0" + "\0".join(ids)).encode("utf-8")).hexdigest()[:16]
        output.append({
            "cluster_id": digest, "environment": env, "size": len(ids),
            "member_experience_ids": ids,
            "representative_goals": sorted({member["goal"] for member in members if member["goal"]})[:3],
            "common_terms": common_terms,
            "minimum_pair_similarity": round(min(similarities), 4) if similarities else 1.0,
            "average_pair_similarity": round(sum(similarities) / len(similarities), 4) if similarities else 1.0,
            "method": "weighted_lexical_jaccard_complete_link",
            "embedding_model": None,
        })
    output.sort(key=lambda c: (-c["size"], c["environment"], c["cluster_id"]))
    return {
        "schema_version": 1, "analysis": "deterministic_lexical_clustering",
        "generated_at": time.time(), "eligible_experiences": len(eligible),
        "cluster_count": len(output), "min_similarity": float(min_similarity),
        "environment_filter": environment, "clusters": output[:limit],
        "unclustered_count": max(0, len(eligible) - sum(c["size"] for c in output[:limit])),
    }
