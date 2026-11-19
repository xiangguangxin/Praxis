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


def main():
    parser = argparse.ArgumentParser(description="Praxis Autonomous Agent Runtime")
    commands = parser.add_subparsers(dest="command", required=True)

    goal = commands.add_parser("create")
    goal.add_argument("checkpoint", type=Path)
    goal.add_argument("--goal", required=True)
    goal.add_argument("--criterion", action="append", required=True)
    goal.add_argument("--max-steps", type=int, default=40)
    commands.add_parser("status").add_argument("checkpoint", type=Path)
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

    args = parser.parse_args()
    try:
        if args.command == "create":
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
        elif args.command == "compare-traces":
            result = compare_checkpoints(load(args.baseline), load(args.candidate))
        elif args.command == "reconcile":
            result = reconcile(args.checkpoint, outcome=args.outcome, note=args.note)
        elif args.command == "verify":
            result = FileVerifier.from_json(args.verifier).evaluate(load(args.checkpoint))
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


def _execute(args):
    from .experience import ExperienceStore
    from .planner import ModelPlanner
    from .policy import ActionPolicy

    connection = resolve_model_connection(args)
    if args.approval_id and not args.approve_pending:
        raise ValueError("--approval-id requires --approve-pending")
    if min(args.max_model_calls, args.max_model_tokens, args.max_ui_text_chars, args.max_images) < 0:
        raise ValueError("Token limits must be nonnegative")
    if bool(args.experience_store) != bool(args.experience_environment):
        raise ValueError("--experience-store and --experience-environment must be used together")
    if not args.browser_url and (args.download_dir is not None or args.browser_executable):
        raise ValueError("Browser settings require --browser-url")

    download_dir = args.download_dir.resolve(strict=True) if args.download_dir else None
    planner = ModelPlanner(connection.model, connection.api_key, connection.endpoint,
                           max_tokens=args.max_model_tokens,
                           max_calls=args.max_model_calls, max_text_chars=args.max_ui_text_chars,
                           max_images=args.max_images,
                           experience_store=ExperienceStore(args.experience_store) if args.experience_store else None,
                           experience_environment=args.experience_environment)
    verifier = FileVerifier.from_json(args.verifier) if args.verifier else None
    from .policy import fingerprint
    scope = (browser_scope(args.browser_url, download_dir) if args.browser_url else
             fingerprint({"nova": str(Path(args.nova).resolve())}))
    policy = ActionPolicy.from_json(args.policy) if args.policy else ActionPolicy()
    factory = lambda: open_session(planner, nova=args.nova, browser_url=args.browser_url,
                                   download_dir=download_dir, browser_executable=args.browser_executable)
    return run_session(args.checkpoint, factory, planner, approval=args.approve_pending,
                       verifier=verifier, backend_scope=scope, trace_tools=args.trace_tools,
                       policy=policy, approval_id=args.approval_id)


if __name__ == "__main__":
    main()
