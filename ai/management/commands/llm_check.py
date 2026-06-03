"""
Smoke-test the active LLM provider (Claude or a local OpenAI-compatible server).

    python manage.py llm_check
    python manage.py llm_check --prompt "Summarise what you are in one line."

Prints which provider/model is active and a sample reply, so you can verify an
air-gapped local endpoint (Ollama/vLLM) is reachable before flipping traffic to
it. Honours LLM_PROVIDER / LLM_BASE_URL / LLM_MODEL from settings/.env.
"""
from django.conf import settings
from django.core.management.base import BaseCommand

from ai.client import active_model, is_configured, run_chat
from ai.providers import get_provider


class Command(BaseCommand):
    help = "Ping the active LLM provider and print a sample reply."

    def add_arguments(self, parser):
        parser.add_argument(
            "--prompt",
            default="Reply with the single word: OK",
            help="Prompt to send to the model.",
        )

    def handle(self, *args, **opts):
        provider = get_provider()
        self.stdout.write(f"LLM_PROVIDER = {getattr(settings, 'LLM_PROVIDER', 'anthropic')}")
        self.stdout.write(f"provider     = {provider.name}")
        self.stdout.write(f"model        = {active_model()}")
        if provider.name == "local":
            self.stdout.write(f"base_url     = {getattr(settings, 'LLM_BASE_URL', '')}")

        if not is_configured():
            self.stderr.write(self.style.ERROR(
                f"NOT configured: {provider.not_configured_message()}"
            ))
            return

        self.stdout.write("Sending test prompt…")
        try:
            reply = run_chat([{"role": "user", "content": opts["prompt"]}])
        except Exception as exc:  # noqa: BLE001
            self.stderr.write(self.style.ERROR(f"LLM call failed: {exc}"))
            return
        self.stdout.write(self.style.SUCCESS(f"reply: {reply}"))
