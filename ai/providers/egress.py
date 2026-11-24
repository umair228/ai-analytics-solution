"""Egress guard — keep lab data on-premise by construction, not by convention.

DIS tells regulated customers that prompts and lab records never leave their
network. That claim used to rest entirely on a single environment variable: the
Anthropic client is shipped in the image, ``LLM_PROVIDER`` defaulted to
"anthropic", and the prompts we send include sample LIMS rows (``ai.context``).
One missing ``.env`` and the promise was quietly false.

So egress is now a deliberate act. ``DSE_ALLOW_EXTERNAL_LLM`` (default False)
gates it, and this module is the single place that decides what "external"
means. Both providers consult it at construction:

  * :class:`~ai.providers.anthropic_provider.AnthropicProvider` refuses to build.
  * :class:`~ai.providers.openai_provider.OpenAIProvider` refuses an
    ``LLM_BASE_URL`` that is not private — so ``LLM_PROVIDER=local`` cannot be
    repointed at ``api.openai.com`` either. "Local" has to mean local.

The test is ``not addr.is_global`` — i.e. "cannot reach the public internet",
which is exactly what egress control means. That covers loopback, RFC1918,
link-local, IPv6 ULA, and the 100.64.0.0/10 CGNAT range Tailscale allocates from
(the production GPU box lives there), with no special-casing. A hostname is
resolved and judged on the address it lands on; if it will not resolve we fail
closed, because an endpoint we cannot vet is one we cannot vouch for.
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

from django.conf import settings

from .base import AINotConfigured


def external_llm_allowed() -> bool:
    return bool(getattr(settings, "DSE_ALLOW_EXTERNAL_LLM", False))


def is_private_address(host: str) -> bool:
    """True if ``host`` is not globally routable (or resolves only to such addresses)."""
    if not host:
        return False
    host = host.strip().strip("[]")  # bare IPv6 literals arrive bracketed

    try:
        return not ipaddress.ip_address(host).is_global
    except ValueError:
        pass  # not a literal — fall through to DNS

    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError):
        return False  # unresolvable → cannot vet → not private
    if not infos:
        return False
    # EVERY address it resolves to must be unroutable. A name that also answers
    # with a public address is not one we can vouch for.
    return all(not ipaddress.ip_address(i[4][0]).is_global for i in infos)


def endpoint_permitted(url: str) -> bool:
    """True if we may call ``url`` — i.e. it is private, or egress is allowed."""
    if external_llm_allowed():
        return True
    return is_private_address(urlparse(url or "").hostname or "")


def assert_endpoint_permitted(url: str) -> None:
    if endpoint_permitted(url):
        return
    host = urlparse(url or "").hostname or url
    raise AINotConfigured(
        f"Refusing to send prompts to '{host}': it is not an on-premise address, "
        "and DSE_ALLOW_EXTERNAL_LLM is not enabled. Prompts include sample LIMS "
        "rows, so this build will not transmit them off-network. Point "
        "LLM_BASE_URL at your on-prem model server, or set "
        "DSE_ALLOW_EXTERNAL_LLM=True to permit outbound calls."
    )


def assert_external_allowed(service: str) -> None:
    if external_llm_allowed():
        return
    raise AINotConfigured(
        f"Refusing to use {service}: DSE_ALLOW_EXTERNAL_LLM is not enabled. This "
        "build keeps lab data on-premise. Set LLM_PROVIDER=local and point "
        "LLM_BASE_URL at your on-prem model server, or set "
        f"DSE_ALLOW_EXTERNAL_LLM=True to permit outbound calls to {service}."
    )
