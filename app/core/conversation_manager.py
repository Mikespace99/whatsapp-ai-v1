"""
Conversation Manager — orchestratore del sistema conversazionale.

Responsabilita':
  1. Mantenere lo stato persistente della conversazione
  2. Ricevere il ParsingResult dal Parser Engine
  3. Applicare le regole tramite il Rule Engine
  4. Invocare il Business Manager per le operazioni applicative
  5. Aggiornare il Conversation State nel DB
  6. Restituire il contesto per la generazione della risposta
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session as DbSession

from app.ai.parser_engine import ParsingResult
from app.core.rule_engine import RuleEngine, Decision
from app.tools.business_manager import BusinessManager

# Mapping: intent LLM → workflow da avviare
INTENT_TO_WORKFLOW = {
    "NEW_BOOKING":          ("BOOKING",      "START_BOOKING"),
    "RESCHEDULE_BOOKING":   ("RESCHEDULE",   "START_RESCHEDULE"),
    "CANCEL_BOOKING":       ("CANCELLATION", "START_CANCELLATION"),
    "CONSULT_APPOINTMENTS": ("INFORMATION",  "START_INFORMATION"),
    "REQUEST_INFORMATION":  ("INFORMATION",  "START_INFORMATION"),
    "SMALLTALK":            (None,           None),
    "UNKNOWN":              (None,           None),
}


class ConversationManager:
    """
    Orchestratore principale del ciclo conversazionale.
    """

    def __init__(self):
        self._rule_engine = RuleEngine()
        self._business_manager = BusinessManager()

    def process(
        self,
        parsing_result: ParsingResult,
        session,
        tenant,
        db: DbSession,
        phone_number: str,
        customer_name: str = "",
    ) -> dict:
        """
        Processa un ParsingResult e aggiorna lo stato della conversazione.
        """
        conv_data = self._load_conv_data(session)
        workflow = session.workflow
        state = session.conv_state

        # Aggiorna conv_data con entita' estratte
        self._merge_entities(conv_data, parsing_result, phone_number, customer_name)

        # Determina workflow se non gia' attivo
        workflow, state = self._resolve_workflow(workflow, state, parsing_result)

        # Valuta regole
        decision = self._rule_engine.evaluate(
            workflow=workflow,
            state=state,
            conv_data=conv_data,
            parsing_result=parsing_result,
        )

        if decision is None:
            self._save_session(session, db, workflow, state, conv_data)
            return {
                "message_key": "fallback",
                "workflow": workflow,
                "state": state,
                "conv_data": conv_data,
                "business_result": None,
            }

        # Aggiorna da decisione
        if decision.next_workflow:
            workflow = decision.next_workflow
        if decision.next_state:
            state = decision.next_state

        # Esegui azioni
        operation_context = self._execute_actions(
            decision=decision,
            workflow=workflow,
            state=state,
            conv_data=conv_data,
            parsing_result=parsing_result,
            tenant=tenant,
            db=db,
            phone_number=phone_number,
            customer_name=customer_name,
        )

        # Salva lo stato finale (usando workflow/state restituiti da operation_context)
        final_workflow = operation_context.get("workflow")
        final_state = operation_context.get("state")
        self._save_session(session, db, final_workflow, final_state, conv_data)

        return operation_context

    # -----------------------------------------------------------------------
    # Execute actions
    # -----------------------------------------------------------------------

    def _execute_actions(
        self,
        decision: Decision,
        workflow: str,
        state: str,
        conv_data: dict,
        parsing_result: ParsingResult,
        tenant,
        db: DbSession,
        phone_number: str,
        customer_name: str,
    ) -> dict:
        """Esegue le azioni della decisione in sequenza."""

        message_key = "generic_response"
        business_result = None

        for action in decision.actions:
            action_type = action.get("type")

            if action_type == "CHANGE_STATE":
                state = action.get("value", state)

            elif action_type == "CHANGE_WORKFLOW":
                workflow = action.get("workflow", workflow)
                state = action.get("state", state)

            elif action_type == "RESET_WORKFLOW":
                workflow = None
                state = None
                conv_data.update({
                    "availability": {"slots": []},
                    "appointment": {},
                })

            elif action_type == "REQUEST_INFORMATION":
                message_key = action.get("message_key", "ask_information")

            elif action_type == "SEND_RESPONSE":
                message_key = action.get("message_key", "generic_response")

            elif action_type == "EXECUTE_ACTION":
                business_action = action.get("action")
                params = self._build_action_params(
                    business_action, conv_data, parsing_result
                )
                business_result = self._business_manager.execute(
                    action=business_action,
                    params=params,
                    tenant=tenant,
                    db=db,
                    phone_number=phone_number,
                    customer_name=customer_name,
                )
                message_key = self._process_business_result(
                    business_action, business_result, conv_data, state
                )

        return {
            "message_key": message_key,
            "workflow": workflow,
            "state": state,
            "conv_data": conv_data,
            "business_result": business_result,
        }

    def _build_action_params(
        self,
        action: str,
        conv_data: dict,
        pr: ParsingResult,
    ) -> dict:
        """Costruisce i parametri per il Business Manager."""

        if action == "SEARCH_AVAILABLE_SLOTS":
            params = {}
            if pr.datetime_info.resolved_date:
                params["date_str"] = pr.datetime_info.resolved_date
            if pr.preferences.time_of_day:
                params["time_of_day"] = pr.preferences.time_of_day
            return params

        if action == "CREATE_APPOINTMENT":
            slots = conv_data.get("availability", {}).get("slots", [])
            selected = self._get_selected_slot(slots, pr)
            if selected:
                return {"date_str": selected["date"], "time_str": selected["time"]}
            return {
                "date_str": pr.datetime_info.resolved_date,
                "time_str": pr.datetime_info.resolved_time,
            }

        if action == "UPDATE_APPOINTMENT":
            slots = conv_data.get("availability", {}).get("slots", [])
            selected = self._get_selected_slot(slots, pr)
            old_appt = conv_data.get("existing_appointment", {})
            params = {
                "old_event_id": old_appt.get("google_event_id") if old_appt else None,
            }
            if selected:
                params.update({"date_str": selected["date"], "time_str": selected["time"]})
            else:
                params.update({
                    "date_str": pr.datetime_info.resolved_date,
                    "time_str": pr.datetime_info.resolved_time,
                })
            return params

        if action in ("CANCEL_APPOINTMENT", "FIND_APPOINTMENT"):
            return {}

        return {}

    def _get_selected_slot(self, slots: list, pr: ParsingResult) -> Optional[dict]:
        """
        Risolve la selezione dell'utente a uno slot concreto.
        Supporta matching per:
          1. Indice (es. 1, 2, 3, "il secondo")
          2. Valore testuale
          3. Date e ora risolte dal temporal_parser (es. "venerdì alle 11" -> date+time match)
        """
        if not slots:
            return None

        # 1. Matching per indice (es. "il secondo" -> index=2 -> slots[1])
        if pr.selection.index is not None:
            idx = pr.selection.index - 1
            if 0 <= idx < len(slots):
                return slots[idx]

        # 2. Matching per valore testuale esatto in selection.value
        if pr.selection.value:
            for slot in slots:
                if slot.get("time") == pr.selection.value:
                    return slot

        # 3. Matching tramite date/time risolti dal temporal_parser
        target_date = pr.datetime_info.resolved_date
        target_time = pr.datetime_info.resolved_time

        # Match sia data che ora
        if target_date and target_time:
            for slot in slots:
                if slot.get("date") == target_date and slot.get("time") == target_time:
                    return slot

        # Match solo ora (es. "quello delle 11" -> time="11:00")
        if target_time:
            for slot in slots:
                if slot.get("time") == target_time:
                    return slot

        # Match solo data (es. "venerdì")
        if target_date:
            for slot in slots:
                if slot.get("date") == target_date:
                    return slot

        # Fallback: se c'e' un unico slot in lista
        if len(slots) == 1:
            return slots[0]

        return None

    def _process_business_result(
        self,
        action: str,
        result: dict,
        conv_data: dict,
        state: str,
    ) -> str:
        """Aggiorna conv_data con il risultato dell'azione."""
        if not result.get("success"):
            code = result.get("error", {}).get("code", "UNKNOWN")
            if code == "CALENDAR_NOT_CONNECTED":
                return "calendar_not_configured"
            if code == "CALENDAR_UNAVAILABLE":
                return "calendar_temporarily_unavailable"
            if code == "NO_APPOINTMENT":
                return "no_appointment_found"
            return "generic_error"

        data = result.get("data", {})

        if action == "SEARCH_AVAILABLE_SLOTS":
            slots = data.get("available_slots", [])
            conv_data["availability"] = {"slots": slots, "date": data.get("date")}
            if not slots:
                return "no_slots_found"
            return "slots_available"

        if action == "CREATE_APPOINTMENT":
            appt = data.get("appointment", {})
            conv_data["appointment"] = appt
            return "booking_confirmed"

        if action == "UPDATE_APPOINTMENT":
            appt = data.get("appointment", {})
            conv_data["appointment"] = appt
            return "reschedule_confirmed"

        if action == "CANCEL_APPOINTMENT":
            conv_data["appointment"] = {"status": "CANCELLED"}
            return "cancellation_confirmed"

        if action == "FIND_APPOINTMENT":
            appt = data.get("appointment")
            conv_data["existing_appointment"] = appt
            if appt:
                return "appointment_found"
            return "no_appointment_found"

        return "generic_response"

    def _resolve_workflow(
        self,
        current_workflow: Optional[str],
        current_state: Optional[str],
        pr: ParsingResult,
    ) -> tuple[Optional[str], Optional[str]]:
        """Determina il workflow da usare."""
        if not current_workflow or not current_state:
            mapping = INTENT_TO_WORKFLOW.get(pr.intent.value)
            if mapping and mapping[0]:
                return mapping
            return None, None
        return current_workflow, current_state

    def _load_conv_data(self, session) -> dict:
        """Carica il conv_data dalla sessione (JSON → dict)."""
        raw = getattr(session, "conv_data", None)
        if raw:
            try:
                data = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                data = {}
        else:
            data = {}

        data.setdefault("customer", {})
        data.setdefault("request", {})
        data.setdefault("availability", {"slots": []})
        data.setdefault("appointment", {})
        return data

    def _merge_entities(
        self,
        conv_data: dict,
        pr: ParsingResult,
        phone_number: str,
        customer_name: str,
    ) -> None:
        """Aggiunge le nuove entita' estratte al conv_data."""
        customer = conv_data.setdefault("customer", {})

        # 1. full_name: SOLO se estratto dal messaggio utente ed ha almeno 2 parole
        if pr.entities.full_name:
            words = pr.entities.full_name.strip().split()
            if len(words) >= 2:
                customer["full_name"] = pr.entities.full_name

        # 2. phone: imposta di default il numero WhatsApp da cui scrive
        if phone_number and not customer.get("phone"):
            customer["phone"] = phone_number

        # Se l'utente scrive esplicitamente un numero di telefono nel testo → usa quello e conferma
        if pr.entities.phone:
            customer["phone"] = pr.entities.phone
            customer["phone_confirmed"] = True

        # Se il cliente risponde "sì" / "va bene" / "ok" alla richiesta di conferma telefono → marca confermato
        if pr.confirmation.value == "YES" and not customer.get("phone_confirmed"):
            customer["phone_confirmed"] = True

        request = conv_data.setdefault("request", {})
        if pr.entities.service:
            request["service"] = pr.entities.service
        if pr.intent.value not in ("SMALLTALK", "UNKNOWN"):
            request["intent"] = pr.intent.value

    def _save_session(
        self,
        session,
        db: DbSession,
        workflow: Optional[str],
        state: Optional[str],
        conv_data: dict,
    ) -> None:
        """Persiste lo stato aggiornato nella UserSession."""
        session.workflow = workflow
        session.conv_state = state
        session.conv_data = json.dumps(conv_data, ensure_ascii=False)
        session.last_interaction = datetime.utcnow()
        db.commit()
