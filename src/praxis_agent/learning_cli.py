"""Offline learning analytics for Praxis experience and checkpoint data."""
import argparse
import json
from pathlib import Path

from .experience import ExperienceStore
from .learning import cluster_experiences, mine_failure_patterns
from .storage import atomic_json_write


def main():
    parser = argparse.ArgumentParser(description="Praxis failure mining and experience clustering")
    commands = parser.add_subparsers(dest="command", required=True)

    failures = commands.add_parser("mine-failures", help="Aggregate trusted failure signatures from checkpoints")
    failures.add_argument("--checkpoints", type=Path, required=True)
    failures.add_argument("--recursive", action="store_true")
    failures.add_argument("--min-occurrences", type=int, default=1)
    failures.add_argument("--limit", type=int, default=1000)
    failures.add_argument("--output", type=Path)

    clusters = commands.add_parser("cluster-experiences", help="Cluster active independently verified experiences")
    clusters.add_argument("--store", type=Path, required=True)
    clusters.add_argument("--environment")
    clusters.add_argument("--min-similarity", type=float, default=0.35)
    clusters.add_argument("--include-singletons", action="store_true")
    clusters.add_argument("--limit", type=int, default=500)
    clusters.add_argument("--output", type=Path)

    args = parser.parse_args()
    if args.command == "mine-failures":
        result = mine_failure_patterns(args.checkpoints, recursive=args.recursive,
                                       min_occurrences=args.min_occurrences, limit=args.limit)
    else:
        store = ExperienceStore(args.store)
        result = cluster_experiences(store._read()["experiences"], environment=args.environment,
                                     min_similarity=args.min_similarity,
                                     include_singletons=args.include_singletons, limit=args.limit)
    if args.output:
        atomic_json_write(args.output, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
