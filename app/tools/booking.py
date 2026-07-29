"""
Booking Pipeline — entry point del sistema conversazionale.

Implementa il ciclo completo in 5 passi:
  1. Recupera sessione e tenant dal DB
  2. Chiama il Parser Engine (1 chiamata LLM → Parsing Result)
  3. Chiama il Conversation Manager (logica deterministica)
  4. Genera la risposta testuale (1 chiamata LLM → testo finale)
  5. Invia il messaggio via WhatsApp
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session as DbSession

from app.ai.engine import call_openai
from app.ai.parser_engine import ParserEngine
from app.ai.prompts import build_tenant_context, RESPONSE_GENERATOR_PROMPT
from app.core.conversation_manager import ConversationManager
from app.db.models import Tenant, UserSession
from app.whatsapp.sender import send_whatsapp_message


# Singleton
_parser_engine = ParserEngine(call_openai)
_conversation_manager = ConversationManager()


# ---------------------------------------------------------------------------
# Message templates
# ---------------------------------------------------------------------------

MESSAGE_TEMPLATES = {
    "ask_full_name":
        "Chiedi gentilmente NOME e COGNOME della persona a cui intitolare l'appuntamento (nome e cognome completi).",

    "ask_phone_confirmation":
        "Informa l'utente che stai usando il numero da cui scrive per le comunicazioni dell'appuntamento (mostra il numero dal contesto) "
        "e chiedi se va bene o se desidera fornirne un altro.",

    "ask_slot_selection":
        "L'utente ha selezionato uno slot oppure gli sono stati mostrati degli slot. "
        "Se l'utente ha appena scelto una data/ora, chiedi in modo DIRETTISSIMO e conciso se intende confermare l'appuntamento "
        "(es. 'Confermi la prenotazione per Giovedì 13 Agosto alle ore 15:00?'). "
        "NON ripetere 'attualmente ho disponibilità' e NON usare frasi ridondanti o preamboli.",

    "slots_available":
        "Se e' presente un appuntamento esistente nel contesto, comunicalo all'utente. "
        "Elenca le prossime disponibilita' trovate numerando le opzioni (1, 2, 3...) e chiedi quale preferisce.",

    "no_slots_found":
        "Informa l'utente che non ci sono disponibilita' nel periodo richiesto. "
        "Chiedi se vuole cercare in un altro giorno.",

    "booking_confirmed":
        "Conferma la prenotazione con i dettagli forniti nel contesto. "
        "Sii entusiasta ma professionale.",

    "reschedule_confirmed":
        "Conferma che l'appuntamento e' stato spostato con successo. "
        "Indica la nuova data e ora dal contesto. Ricorda che il vecchio appuntamento e' stato cancellato.",

    "cancellation_confirmed":
        "Conferma che l'appuntamento e' stato cancellato. "
        "Chiedi se l'utente desidera fissarne uno nuovo.",

    "cancellation_aborted":
        "Informa l'utente che la cancellazione e' stata annullata. "
        "L'appuntamento rimane confermato.",

    "no_appointment_to_reschedule":
        "Informa l'utente che non risulta nessun appuntamento futuro attivo da spostare. "
        "Chiedi se vuole prenotarne uno nuovo.",

    "no_appointment_to_cancel":
        "Informa l'utente che non risulta nessun appuntamento attivo da cancellare.",

    "no_appointment_found":
        "Informa l'utente che non risulta nessun appuntamento futuro nel sistema.",

    "appointment_found":
        "Comunica all'utente i dettagli del suo prossimo appuntamento dal contesto.",

    "show_appointment":
        "Comunica all me l'utente i dettagli del suo prossimo appuntamento dal contesto.",

    "slot_rejected_ask_again":
        "L'utente ha rifiutato lo slot proposto. Mostra nuovamente le opzioni disponibili "
        "e chiedi quale preferisce.",

    "smalltalk_response":
        "Rispondi in modo cordiale al messaggio conversazionale. "
        "Offri assistenza per prenotazioni o informazioni sullo studio.",

    "information_response":
        "Rispondi in modo chiaro alla richiesta di informazioni usando i dati dello studio nel contesto.",

    "calendar_not_configured":
        "Informa l'utente che il servizio di prenotazione online non e' ancora attivo "
        "e invita a contattare lo studio direttamente.",

    "calendar_temporarily_unavailable":
        "Informa l'utente che il sistema e' temporaneamente non disponibile. Riprovare tra poco.",

    "generic_error":
        "Informa l'utente che si e' verificato un problema tecnico. Invita a riprovare.",

    "fallback":
        "Non hai capito bene la richiesta. Chiedi all'utente di riformularla in modo piu' chiaro.",
}


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

def process_incoming_message(
    sender_phone: str,
    contact_name: str,
    message_body: str,
    tenant: Tenant,
    db: DbSession,
) -> str:
    """
    Entry point chiamato da webhook.py (con oggetto tenant gia' caricato).
    """
    return _execute_pipeline(
        phone_number=sender_phone,
        message_text=message_body,
        customer_name=contact_name,
        tenant=tenant,
        db=db,
    )


def handle_whatsapp_message(
    phone_number: str,
    message_text: str,
    tenant_id: int,
    db: DbSession,
    customer_name: str = "",
) -> str:
    """
    Entry point alternativo (con tenant_id).
    """
    tenant = db.query(Tenant).filter(Tenant.id == tenant_id).first()
    if not tenant or not tenant.is_active:
        return ""

    return _execute_pipeline(
        phone_number=phone_number,
        message_text=message_text,
        customer_name=customer_name,
        tenant=tenant,
        db=db,
    )


# ---------------------------------------------------------------------------
# Internal pipeline
# ---------------------------------------------------------------------------

def _execute_pipeline(
    phone_number: str,
    message_text: str,
    customer_name: str,
    tenant: Tenant,
    db: DbSession,
) -> str:
    """Pipeline a 5 passi."""
    try:
        # 1. Recupera sessione
        session = _get_or_create_session(db, tenant.id, phone_number)

        # 2. Parser Engine — 1 chiamata LLM
        workflow_label = _format_workflow_label(session.workflow, session.conv_state)
        parsing_result = _parser_engine.parse(
            message=message_text,
            workflow_state=workflow_label,
            tenant=tenant,
            now=datetime.now(),
        )

        print(f"[Pipeline] phone={phone_number} "
              f"intent={parsing_result.intent.value} "
              f"workflow={session.workflow}/{session.conv_state}")

        # 3. Conversation Manager — deterministico
        operation_context = _conversation_manager.process(
            parsing_result=parsing_result,
            session=session,
            tenant=tenant,
            db=db,
            phone_number=phone_number,
            customer_name=customer_name,
        )

        # 4. Response Generator — 1 chiamata LLM
        response_text = _generate_response(
            message_key=operation_context["message_key"],
            operation_context=operation_context,
            user_message=message_text,
            tenant=tenant,
        )

        # 5. Invia messaggio WhatsApp via sender.py
        token = getattr(tenant, "whatsapp_access_token", None) or ""
        phone_id = getattr(tenant, "whatsapp_phone_number_id", None) or ""

        send_whatsapp_message(
            to=phone_number,
            text=response_text,
            token=token,
            phone_id=phone_id,
        )

        return response_text

    except Exception as e:
        print(f"[Pipeline] ERRORE CRITICO per {phone_number}: {e}")
        import traceback
        traceback.print_exc()
        return _fallback_error_response(tenant)


# ---------------------------------------------------------------------------
# Response Generator
# ---------------------------------------------------------------------------

def _generate_response(
    message_key: str,
    operation_context: dict,
    user_message: str,
    tenant: Tenant,
) -> str:
    """Genera la risposta testuale via LLM."""
    instruction = MESSAGE_TEMPLATES.get(message_key, MESSAGE_TEMPLATES["fallback"])
    tenant_context = build_tenant_context(tenant)

    conv_data = operation_context.get("conv_data", {})
    workflow = operation_context.get("workflow")
    state = operation_context.get("state")

    context_parts = [
        f"Istruzione: {instruction}",
        f"Workflow: {workflow or 'nessuno'} | Stato: {state or 'nessuno'}",
    ]

    customer = conv_data.get("customer", {})
    if customer.get("full_name"):
        context_parts.append(f"Nome della persona dell'appuntamento: {customer['full_name']}")
    if customer.get("phone"):
        context_parts.append(f"Numero di telefono di contatto attuale: {customer['phone']}")

    # Appuntamento esistente attivo (per reschedule / cancel / check)
    existing = conv_data.get("existing_appointment")
    if existing and isinstance(existing, dict) and existing.get("datetime_display"):
        context_parts.append(f"Appuntamento attuale del cliente: {existing['datetime_display']}")

    # Slot disponibili (se presenti)
    slots = conv_data.get("availability", {}).get("slots", [])
    if slots:
        slot_lines = []
        for i, s in enumerate(slots, 1):
            slot_lines.append(f"  {i}. {_format_date_italian(s.get('date', ''))} alle ore {s.get('time', '')}")
        context_parts.append("Disponibilita' trovate:\n" + "\n".join(slot_lines))

    # Nuovo appuntamento confermato/modificato
    appt = conv_data.get("appointment", {})
    if appt.get("date"):
        context_parts.append(
            f"Nuovo appuntamento: {_format_date_italian(appt['date'])} alle ore {appt.get('time', '')}"
        )

    full_context = "\n".join(context_parts)

    prompt = (
        RESPONSE_GENERATOR_PROMPT.format(
            tenant_context=tenant_context,
            operation_context=full_context,
        )
        + f"\n\nMessaggio utente: {user_message}"
    )

    return call_openai(prompt).strip()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_or_create_session(db: DbSession, tenant_id: int, phone_number: str) -> UserSession:
    """Recupera la sessione esistente o ne crea una nuova."""
    session = (
        db.query(UserSession)
        .filter(
            UserSession.tenant_id == tenant_id,
            UserSession.customer_phone == phone_number,
        )
        .first()
    )
    if not session:
        session = UserSession(
            tenant_id=tenant_id,
            customer_phone=phone_number,
            workflow=None,
            conv_state=None,
            conv_data=None,
            state="idle",
        )
        db.add(session)
        db.commit()
        db.refresh(session)
    return session


def _format_workflow_label(workflow: Optional[str], state: Optional[str]) -> str:
    if workflow and state:
        return f"{workflow}/{state}"
    if workflow:
        return workflow
    return "nessuno"


def _format_date_italian(date_str: str) -> str:
    if not date_str:
        return date_str
    try:
        months = {
            1: "gennaio", 2: "febbraio", 3: "marzo", 4: "aprile",
            5: "maggio", 6: "giugno", 7: "luglio", 8: "agosto",
            9: "settembre", 10: "ottobre", 11: "novembre", 12: "dicembre",
        }
        days = {
            0: "lunedì", 1: "martedì", 2: "mercoledì", 3: "giovedì",
            4: "venerdì", 5: "sabato", 6: "domenica",
        }
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        return f"{days[dt.weekday()]} {dt.day} {months[dt.month]}"
    except ValueError:
        return date_str


def _fallback_error_response(tenant: Optional[Tenant]) -> str:
    name = getattr(tenant, "name", "") if tenant else ""
    if name:
        return f"Mi dispiace, si e' verificato un problema tecnico. Contatta direttamente {name}."
    return "Mi dispiace, si e' verificato un problema tecnico. Riprova tra qualche minuto."
