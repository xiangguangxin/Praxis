"""Explicitly curate, evaluate, promote and roll back learned strategies."""
import argparse
import json
from pathlib import Path
from .experience import ExperienceStore
from .evolution import EvolutionStore

def main():
    parser=argparse.ArgumentParser(description="Praxis evidence-gated strategy evolution")
    parser.add_argument("--store",type=Path,required=True)
    commands=parser.add_subparsers(dest="command",required=True)
    propose=commands.add_parser("propose")
    propose.add_argument("--experience-store",type=Path,required=True)
    propose.add_argument("--source-experience-id",required=True)
    propose.add_argument("--goal",required=True)
    propose.add_argument("--environment",required=True)
    propose.add_argument("--instruction",required=True)
    propose.add_argument("--rationale",required=True)
    extract=commands.add_parser("extract")
    extract.add_argument("--experience-store",type=Path,required=True)
    extract.add_argument("--experience-id",required=True)
    generate=commands.add_parser("generate-from-insights", help="Generate an inert strategy from verified insights")
    generate.add_argument("--experience-store",type=Path,required=True)
    generate.add_argument("--cluster-report",type=Path,required=True)
    generate.add_argument("--failure-report",type=Path,required=True)
    generate.add_argument("--cluster-id",required=True)
    generate.add_argument("--pattern-id",required=True)
    generate.add_argument("--source-experience-id",required=True)
    evaluate=commands.add_parser("evaluate")
    evaluate.add_argument("strategy_id")
    evaluate.add_argument("--report",type=Path,required=True)
    evaluate.add_argument("--baseline-variant",default="baseline")
    evaluate.add_argument("--candidate-variant",default="strategy_candidate")
    reassess=commands.add_parser("reassess")
    reassess.add_argument("strategy_id")
    reassess.add_argument("--report",type=Path,required=True)
    reassess.add_argument("--baseline-variant",default="baseline")
    reassess.add_argument("--candidate-variant",default="strategy_candidate")
    promote=commands.add_parser("promote")
    promote.add_argument("strategy_id")
    promote.add_argument("--experience-store",type=Path,required=True,
        help="Recheck that the source verified experience remains active")
    rollback=commands.add_parser("rollback")
    rollback.add_argument("strategy_id")
    rollback.add_argument("--reason",required=True)
    listing=commands.add_parser("list")
    listing.add_argument("--status",choices=("candidate","promoted","rolled_back"))
    args=parser.parse_args()
    store=EvolutionStore(args.store,
        ExperienceStore(args.experience_store) if args.command in ("propose","promote","extract","generate-from-insights") else None)
    if args.command=="propose":
        result=store.propose(goal=args.goal,environment=args.environment,instruction=args.instruction,
            source_experience_id=args.source_experience_id,rationale=args.rationale)
    elif args.command=="extract":
        result=store.extract_candidate(args.experience_id)
    elif args.command=="generate-from-insights":
        clusters=json.loads(args.cluster_report.read_text(encoding="utf-8"))
        failures=json.loads(args.failure_report.read_text(encoding="utf-8"))
        cluster=next((item for item in clusters.get("clusters", [])
                      if item.get("cluster_id")==args.cluster_id),None)
        pattern=next((item for item in failures.get("patterns", [])
                      if item.get("pattern_id")==args.pattern_id),None)
        if cluster is None or pattern is None:
            raise ValueError("Selected cluster or failure pattern was not found in the supplied reports")
        result=store.propose_from_insights(cluster=cluster,failure_pattern=pattern,
            source_experience_id=args.source_experience_id)
    elif args.command in ("evaluate","reassess"):
        result=store.evaluate_variants(args.strategy_id,args.report,
            baseline_variant=args.baseline_variant,candidate_variant=args.candidate_variant)
    elif args.command=="promote":
        result=store.promote(args.strategy_id)
    elif args.command=="rollback":
        result=store.rollback(args.strategy_id,reason=args.reason)
    else:
        result=store.list(status=args.status)
    print(json.dumps(result,ensure_ascii=False,indent=2))

if __name__=="__main__":
    main()
