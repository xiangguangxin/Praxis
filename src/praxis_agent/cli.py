"""Create, execute, resume and independently verify GUI goals."""
import argparse
import json
from pathlib import Path

from .agent import run_session
from .model_connection import add_model_arguments, resolve_model_connection
from .recovery import reconcile, retry
from .session import browser_scope, open_session
from .state import create, load
from .tool_trace import compare_checkpoints
from .verification import FileVerifier
from .verifiers.configured import load_verifier
from .request_runtime import add_request_arguments, request_options


def main():
    parser = argparse.ArgumentParser(description="Praxis Autonomous Agent Runtime")
    commands = parser.add_subparsers(dest="command", required=True)

    goal = commands.add_parser("create")
    goal.add_argument("checkpoint", type=Path)
    goal.add_argument("--goal", required=True)
    goal.add_argument("--criterion", action="append", required=True)
    goal.add_argument("--max-steps", type=int, default=40)
    commands.add_parser("status").add_argument("checkpoint", type=Path)
    inspection = commands.add_parser("timeline", help="Export a read-only run timeline as HTML or JSON")
    inspection.add_argument("checkpoint", type=Path)
    inspection.add_argument("--output", type=Path, required=True)
    inspection.add_argument("--journal", type=Path)
    comparison = commands.add_parser("compare-traces", help="Find the first recorded checkpoint difference")
    comparison.add_argument("baseline", type=Path)
    comparison.add_argument("candidate", type=Path)

    verifier_command = commands.add_parser("verify", help="Recheck artifacts for a checkpoint without a model")
    verifier_command.add_argument("checkpoint", type=Path)
    verifier_command.add_argument("--verifier", type=Path, required=True)
    recovery = commands.add_parser("reconcile")
    recovery.add_argument("checkpoint", type=Path)
    recovery.add_argument("--outcome", choices=("executed", "not-executed"), required=True)
    recovery.add_argument("--note", required=True)

    again = commands.add_parser("retry", help="Reopen a known-outcome paused/blocked goal")
    again.add_argument("checkpoint", type=Path)
    again.add_argument("--note", required=True)
    again.add_argument("--max-steps", type=int)
    health = commands.add_parser("doctor", help="Check local readiness without model or desktop actions")
    health.add_argument("--browser", action="store_true")
    health.add_argument("--browser-executable")
    health.add_argument("--nova")
    health.add_argument("--check-model", action="store_true")
    add_model_arguments(health)
    scheduled = commands.add_parser("schedule", help="Queue a goal for a bounded local worker")
    scheduled.add_argument("--database", type=Path, required=True)
    scheduled.add_argument("--goal", required=True)
    scheduled.add_argument("--criterion", action="append", required=True)
    scheduled.add_argument("--run-at", type=float)
    worker = commands.add_parser("worker", help="Execute up to N due jobs, then exit")
    worker.add_argument("--database", type=Path, required=True)
    worker.add_argument("--checkpoint-dir", type=Path, required=True)
    worker.add_argument("--max-jobs", type=int, default=1)
    worker.add_argument("--lease-seconds", type=float, default=60,
                        help="Worker job lease duration; renewed while the job runs")
    worker.add_argument("--heartbeat-interval", type=float,
                        help="Heartbeat cadence (default: one third of the lease)")
    backend = worker.add_mutually_exclusive_group(required=True)
    backend.add_argument("--nova")
    backend.add_argument("--browser-url")
    worker.add_argument("--download-dir", type=Path)
    worker.add_argument("--browser-executable")
    add_model_arguments(worker)
    add_request_arguments(worker)
    worker.add_argument("--model-profiles", type=Path)
    worker.add_argument("--verifier", type=Path)
    worker.add_argument("--trace-tools", action="store_true")
    worker.add_argument("--journal", type=Path)
    worker.add_argument("--policy", type=Path)
    worker.add_argument("--max-model-calls", type=int, default=0)
    worker.add_argument("--max-model-tokens", type=int, default=0)
    worker.add_argument("--max-ui-text-chars", type=int, default=5000)
    worker.add_argument("--max-images", type=int, default=1)
    worker.add_argument("--experience-store", type=Path)
    worker.add_argument("--experience-environment", default="")
    worker.add_argument("--evolution-store", type=Path)
    worker.add_argument("--evolution-environment", default="")
    dispatch = commands.add_parser("outbox-dispatch", help="Deliver durable verified-completion events")
    dispatch.add_argument("--database", type=Path, required=True)
    dispatch.add_argument("--consumer", choices=("experience", "evolution"), required=True)
    dispatch.add_argument("--limit", type=int, default=100)
    dispatch.add_argument("--store", type=Path, required=True)
    dispatch.add_argument("--verifier", type=Path)
    dispatch.add_argument("--environment", default="")
    dispatch.add_argument("--note", default="", help="Operator-curated note required for Experience delivery")
    audit = commands.add_parser("audit-state", help="Audit scheduler/checkpoint consistency without modifying state")
    audit.add_argument("--database", type=Path, required=True)
    audit.add_argument("--checkpoint-dir", type=Path, required=True)

    example = commands.add_parser("demo", help="Run real Chromium with a deterministic fixture planner")
    example.add_argument("--output", type=Path, required=True)
    example.add_argument("--browser-executable")

    for name in ("run", "resume"):
        execution = commands.add_parser(name)
        execution.add_argument("checkpoint", type=Path)
        backend = execution.add_mutually_exclusive_group(required=True)
        backend.add_argument("--nova")
        backend.add_argument("--browser-url", help="Start URL for an isolated Chromium browser")
        execution.add_argument("--download-dir", type=Path, help="Existing directory for new browser downloads")
        execution.add_argument("--browser-executable")
        add_model_arguments(execution)
        add_request_arguments(execution)
        execution.add_argument("--approve-pending", action="store_true")
        execution.add_argument("--approval-id", help="Require this exact pending approval fingerprint")
        execution.add_argument("--policy", type=Path, help="Trusted exact-match operator action rules")
        execution.add_argument("--trace-tools", action="store_true",
                               help="Record local tool names, outcomes, and times in the checkpoint")
        execution.add_argument("--verifier", type=Path, help="JSON rules for independent artifact verification")
        execution.add_argument("--max-model-calls", type=int, default=0)
        execution.add_argument("--max-model-tokens", type=int, default=0)
        execution.add_argument("--max-ui-text-chars", type=int, default=5000)
        execution.add_argument("--max-images", type=int, default=1)
        execution.add_argument("--experience-store", type=Path)
        execution.add_argument("--experience-environment", default="")
        execution.add_argument("--evolution-store", type=Path)
        execution.add_argument("--evolution-environment", default="")
        execution.add_argument("--strategy-candidate-id", help="Preview an unpromoted strategy for paired evaluation only")
        execution.add_argument("--model-profiles", type=Path)
        execution.add_argument("--journal", type=Path)

    args = parser.parse_args()
    try:
        if args.command == "schedule":
            from .scheduler import Scheduler
            result = Scheduler(args.database).enqueue(args.goal, {"criteria": args.criterion}, run_at=args.run_at)
        elif args.command == "worker":
            result = _run_worker(args)
        elif args.command == "outbox-dispatch":
            from .scheduler import Scheduler
            Scheduler(args.database)  # Ensure the durable outbox schema exists before dispatch.
            from .outbox import OutboxDispatcher, ExperienceEventConsumer, EvolutionEventConsumer
            if args.consumer == "experience":
                if not args.verifier or not args.environment or not args.note:
                    raise ValueError("Experience delivery requires --verifier, --environment and --note")
                from .experience import ExperienceStore
                handler = ExperienceEventConsumer(ExperienceStore(args.store),
                    FileVerifier.from_json(args.verifier), environment=args.environment, note=args.note)
            else:
                from .evolution import EvolutionStore
                handler = EvolutionEventConsumer(EvolutionStore(args.store))
            result = OutboxDispatcher(args.database, consumer=args.consumer,
                                      handler=handler).dispatch(limit=args.limit)
        elif args.command == "audit-state":
            from .coordinator import ExecutionCoordinator
            from .scheduler import Scheduler
            result = ExecutionCoordinator(Scheduler(args.database), args.checkpoint_dir).audit()
        elif args.command == "create":
            result = create(args.checkpoint, args.goal, args.criterion, max_steps=args.max_steps)
        elif args.command == "doctor":
            from .doctor import doctor
            result = doctor(args)
        elif args.command == "demo":
            from .demo import demo
            result = demo(args.output, args.browser_executable)
        elif args.command == "retry":
            result = retry(args.checkpoint, note=args.note, max_steps=args.max_steps)
        elif args.command == "status":
            result = load(args.checkpoint)
        elif args.command == "timeline":
            from .timeline import write_timeline
            result = write_timeline(args.checkpoint, args.output, journal=args.journal)
        elif args.command == "compare-traces":
            result = compare_checkpoints(load(args.baseline), load(args.candidate))
        elif args.command == "reconcile":
            result = reconcile(args.checkpoint, outcome=args.outcome, note=args.note)
        elif args.command == "verify":
            result = load_verifier(args.verifier).evaluate(load(args.checkpoint))
        else:
            result = _execute(args)
    except KeyboardInterrupt:
        # The runtime has already persisted a paused/blocked state. Avoid a traceback.
        print(json.dumps({"interrupted": True, "checkpoint": str(getattr(args, "checkpoint", ""))}))
        raise SystemExit(130)
    except (OSError, ValueError, RuntimeError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.command == "doctor" and not result["healthy"]:
        raise SystemExit(1)
    if args.command in {"run", "resume", "demo"}:
        raise SystemExit({"completed": 0, "paused": 3, "blocked": 4, "failed": 5}.get(result["status"], 5))
    if args.command == "verify" and not result["passed"]:
        raise SystemExit(1)
    if args.command == "audit-state" and not result["consistent"]:
        raise SystemExit(1)


def _execute(args, execution_guard=None):
    from .experience import ExperienceStore
    from .planner import ModelPlanner
    from .policy import ActionPolicy
    from .model_router import ModelRouter

    connection = resolve_model_connection(args)
    if args.approval_id and not args.approve_pending:
        raise ValueError("--approval-id requires --approve-pending")
    if min(args.max_model_calls, args.max_model_tokens, args.max_ui_text_chars, args.max_images) < 0:
        raise ValueError("Token limits must be nonnegative")
    if bool(args.experience_store) != bool(args.experience_environment):
        raise ValueError("--experience-store and --experience-environment must be used together")
    if bool(args.evolution_store) != bool(args.evolution_environment):
        raise ValueError("--evolution-store and --evolution-environment must be used together")
    if args.strategy_candidate_id and not args.evolution_store:
        raise ValueError("--strategy-candidate-id requires --evolution-store")
    if not args.browser_url and (args.download_dir is not None or args.browser_executable):
        raise ValueError("Browser settings require --browser-url")

    download_dir = args.download_dir.resolve(strict=True) if args.download_dir else None
    router = ModelRouter.from_json(args.model_profiles) if args.model_profiles else None
    planner = ModelPlanner(connection.model, connection.api_key, connection.endpoint,
                           max_tokens=args.max_model_tokens,
                           max_calls=args.max_model_calls, max_text_chars=args.max_ui_text_chars,
                           max_images=args.max_images,
                           experience_store=ExperienceStore(args.experience_store) if args.experience_store else None,
                           experience_environment=args.experience_environment, model_router=router,
                           evolution_store=__import__("praxis_agent.evolution", fromlist=["EvolutionStore"]).EvolutionStore(args.evolution_store) if args.evolution_store else None,
                           evolution_environment=args.evolution_environment,
                           strategy_candidate_id=getattr(args, "strategy_candidate_id", None), **request_options(args))
    verifier = load_verifier(args.verifier) if args.verifier else None
    from .policy import fingerprint
    scope = (browser_scope(args.browser_url, download_dir) if args.browser_url else
             fingerprint({"nova": str(Path(args.nova).resolve())}))
    policy = ActionPolicy.from_json(args.policy) if args.policy else ActionPolicy()
    factory = lambda: open_session(planner, nova=args.nova, browser_url=args.browser_url,
                                   download_dir=download_dir, browser_executable=args.browser_executable)
    return run_session(args.checkpoint, factory, planner, approval=args.approve_pending,
                       verifier=verifier, backend_scope=scope, trace_tools=args.trace_tools,
                       policy=policy, approval_id=args.approval_id, execution_guard=execution_guard,
                       journal=getattr(args, "journal", None))


if __name__ == "__main__":
    main()

def _run_worker(args):
    from argparse import Namespace
    from .coordinator import ExecutionCoordinator
    from .scheduler import Scheduler
    from .worker import Worker
    scheduler = Scheduler(args.database)
    coordinator = ExecutionCoordinator(scheduler, args.checkpoint_dir)
    def execute(job, lease_guard=None):
        checkpoint = coordinator.open_checkpoint(job)
        values = vars(args).copy()
        values.update({"command": "run", "checkpoint": checkpoint, "approve_pending": False, "approval_id": None})
        result = _execute(Namespace(**values), execution_guard=lease_guard)
        if isinstance(result, dict) and result.get("status") == "completed":
            result = dict(result, _checkpoint_path=str(checkpoint))
        return result
    return {"jobs": Worker(scheduler, execute, lease_seconds=args.lease_seconds,
                           heartbeat_interval=args.heartbeat_interval).run(max_jobs=args.max_jobs)}
