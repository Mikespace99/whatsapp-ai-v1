"""
LLM Engine — wrapper per le chiamate al modello linguistico.

Espone:
  call_openai(prompt)             → str  (chiamata generica)
  generate_conversational_reply() → str  (per il Response Generator)
"""

import os
from datetime import datetime

from openai import OpenAI
from app.ai.prompts import RESPONSE_GENERATOR_PROMPT, build_tenant_context

client = OpenAI(
    api_key=os.environ.get("OPENAI_API_KEY", "").strip()
)


def call_openai(prompt: str) -> str:
    """Chiamata generica al modello LLM. Restituisce il testo grezzo."""
    if not client.api_key:
        raise RuntimeError("OPENAI_API_KEY mancante.")

    response = client.responses.create(
        model="gpt-4o-mini",
        input=prompt,
    )
    return response.output_text


def generate_conversational_reply(context_msg: str, user_message: str, tenant=None) -> str:
    """
    Genera una risposta conversazionale (legacy helper).
    Preferire booking._generate_response() che usa il pieno operation_context.
    """
    tenant_context = build_tenant_context(tenant)
    prompt = (
        RESPONSE_GENERATOR_PROMPT.format(
            tenant_context=tenant_context,
            operation_context=context_msg,
        )
        + f"\n\nMessaggio utente: {user_message}"
    )
    return call_openai(prompt).strip()
