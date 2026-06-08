"""
Run the DIS analytics agent from the command line — no frontend needed.

    python manage.py agent_run "Which lab section has the slowest turnaround?"
    python manage.py agent_run "Flag unusual pH results and check the spec" --dataset 78
    python manage.py agent_run "..." --user alice --steps 8 --quiet

Prints the agent's evidence trace (each tool call + a short result preview) and
its final answer. Honours the active LLM provider (LLM_PROVIDER); make sure it
is configured (`python manage.py llm_check`) and, for local, that the model
supports tool calling (e.g. qwen2.5).
"""
import json

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from ai.agent import run_agent
from ai.client import active_model, is_configured
from ai.providers import AINotConfigured, get_provider
from datasets.models import Dataset


class Command(BaseCommand):
    help = "Run the agentic assistant on a question and print its trace + answer."

    def add_arguments(self, parser):
        parser.add_argument("question", help="The question to ask the agent.")
        parser.add_argument("--dataset", type=int, default=None,
                            help="Focus a dataset id (optional).")
        parser.add_argument("--user", default=None,
                            help="Username to run as (default: first admin/superuser).")
        parser.add_argument("--steps", type=int, default=None,
                            help="Override AGENT_MAX_STEPS for this run.")
        parser.add_argument("--quiet", action="store_true",
                            help="Print only the final answer (no trace).")

    def _resolve_user(self, username):
        User = get_user_model()
        if username:
            user = User.objects.filter(username=username).first()
            if not user:
                raise CommandError(f"No user '{username}'.")
            return user
        user = (User.objects.filter(is_superuser=True).first()
                or User.objects.filter(role="admin").first()
                or User.objects.first())
        if not user:
            raise CommandError("No users exist — seed the database first.")
        return user

    def handle(self, *args, **opts):
        provider = get_provider()
        self.stdout.write(f"provider = {provider.name} | model = {active_model()}")
        if not is_configured():
            raise CommandError(f"LLM not configured: {provider.not_configured_message()}")

        user = self._resolve_user(opts["user"])
        dataset = None
        if opts["dataset"] is not None:
            dataset = Dataset.objects.filter(pk=opts["dataset"]).first()
            if dataset is None:
                raise CommandError(f"No dataset with id {opts['dataset']}.")

        self.stdout.write(f"user = {user.username} | dataset = "
                          f"{dataset.name if dataset else '(none)'}")
        self.stdout.write(self.style.HTTP_INFO(f"Q: {opts['question']}\n"))

        history = [{"role": "user", "content": opts["question"]}]
        try:
            run = run_agent(user, history, dataset=dataset, max_steps=opts["steps"])
        except AINotConfigured as exc:
            raise CommandError(str(exc))

        if not opts["quiet"]:
            for entry in run.trace:
                mark = "✓" if entry["ok"] else "✗"
                args_str = json.dumps(entry["input"], default=str)
                self.stdout.write(
                    f"  [{entry['step']}] {mark} {entry['tool']}({args_str})"
                )
                preview = json.dumps(entry["result"], default=str)
                if len(preview) > 300:
                    preview = preview[:300] + "…"
                self.stdout.write(self.style.WARNING(f"        → {preview}"))
            self.stdout.write(
                f"\n(steps={run.steps}, tool_calls={run.tool_calls}, "
                f"stopped={run.stopped_reason})\n"
            )

        self.stdout.write(self.style.SUCCESS("ANSWER:"))
        self.stdout.write(run.answer)
