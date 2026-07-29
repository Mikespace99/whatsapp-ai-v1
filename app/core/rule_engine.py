"""
Rule Engine — motore decisionale dichiarativo.

Riceve:
  - workflow corrente (es. "BOOKING")
  - stato corrente (es. "WAITING_SLOT_SELECTION")
  - conv_data (dict strutturato)
  - parsing_result (ParsingResult)

Restituisce:
  - Decision (dict con action, next_state, params)
"""

from __future__ import annotations

import logging
from typing import Optional
from app.ai.parser_engine import ParsingResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Decision — output del Rule Engine
# ---------------------------------------------------------------------------

class Decision:
    """Rappresenta la decisione presa dal Rule Engine."""

    def __init__(
        self,
        rule_id: str,
        actions: list[dict],
        next_state: Optional[str] = None,
        next_workflow: Optional[str] = None,
    ):
        self.rule_id = rule_id
        self.actions = actions
        self.next_state = next_state
        self.next_workflow = next_workflow

    def get_primary_action_type(self) -> str:
        """Restituisce il tipo della prima azione."""
        if self.actions:
            return self.actions[0].get("type", "")
        return ""

    def __repr__(self):
        return f"Decision(rule={self.rule_id}, actions={self.actions})"


# ---------------------------------------------------------------------------
# Condition Evaluator
# ---------------------------------------------------------------------------

class ConditionEvaluator:
    """Valuta le condizioni di una regola in modo deterministico."""

    def evaluate(
        self,
        conditions: dict,
        conv_data: dict,
        parsing_result: ParsingResult,
        workflow: Optional[str] = None,
    ) -> bool:
        """
        Restituisce True se TUTTE le condizioni sono soddisfatte (AND).
        """
        for condition_key, condition_value in conditions.items():
            if not self._check_condition(
                condition_key, condition_value, conv_data, parsing_result, workflow
            ):
                return False
        return True

    def _check_condition(
        self,
        key: str,
        value,
        conv_data: dict,
        pr: ParsingResult,
        workflow: Optional[str] = None,
    ) -> bool:
        """Valuta una singola condizione."""

        # --- Workflow condition ---
        if key == "workflow_is":
            return workflow == value

        # --- Intent conditions ---
        if key == "intent_is":
            return pr.intent.value == value

        if key == "intent_in":
            return pr.intent.value in value

        # --- Confirmation conditions ---
        if key == "confirmation_is":
            return pr.confirmation.value == value

        # --- Selection conditions ---
        if key == "has_selection":
            return pr.selection.index is not None or pr.selection.value is not None

        # --- DateTime conditions ---
        if key == "has_date":
            return bool(pr.datetime_info.resolved_date)

        if key == "has_time":
            return bool(pr.datetime_info.resolved_time)

        if key == "date_certainty_is":
            return pr.datetime_info.date_certainty == value

        # --- Customer data conditions ---
        if key == "customer_field_missing":
            customer = conv_data.get("customer", {})
            if isinstance(value, list):
                return any(not customer.get(f) for f in value)
            return not customer.get(value)

        if key == "customer_fields_complete":
            customer = conv_data.get("customer", {})
            required = value  # lista di campi obbligatori
            for f in required:
                val = customer.get(f)
                if not val:
                    return False
                if f == "full_name":
                    words = str(val).strip().split()
                    if len(words) < 2:
                        return False
            return True

        if key == "phone_confirmed":
            return bool(conv_data.get("customer", {}).get("phone_confirmed"))

        # --- Request/service conditions ---
        if key == "has_service":
            return bool(conv_data.get("request", {}).get("service"))

        # --- Availability conditions ---
        if key == "has_available_slots":
            slots = conv_data.get("availability", {}).get("slots", [])
            return len(slots) > 0

        if key == "no_available_slots":
            slots = conv_data.get("availability", {}).get("slots", [])
            return len(slots) == 0

        if key == "slot_selection_valid":
            slots = conv_data.get("availability", {}).get("slots", [])
            if not slots:
                return False

            # Match per indice (es. 1, 2, 3)
            idx = pr.selection.index
            if idx is not None and 1 <= idx <= len(slots):
                return True

            # Match per valore testuale
            if pr.selection.value:
                if any(s.get("time") == pr.selection.value for s in slots):
                    return True

            # Match per date/time risolti dal temporal_parser (es. "venerdì alle 11")
            target_date = pr.datetime_info.resolved_date
            target_time = pr.datetime_info.resolved_time

            if target_date and target_time:
                if any(s.get("date") == target_date and s.get("time") == target_time for s in slots):
                    return True

            if target_time:
                if any(s.get("time") == target_time for s in slots):
                    return True

            if target_date:
                if any(s.get("date") == target_date for s in slots):
                    return True

            return False

        # --- Appointment conditions ---
        if key == "has_active_appointment":
            appt = conv_data.get("existing_appointment")
            return bool(appt)

        if key == "no_active_appointment":
            appt = conv_data.get("existing_appointment")
            return not bool(appt)

        # --- Negazione ---
        if key == "not":
            sub_key = list(value.keys())[0]
            sub_val = list(value.values())[0]
            return not self._check_condition(sub_key, sub_val, conv_data, pr, workflow)

        # Condizione non gestita
        logger.warning(f"[ConditionEvaluator] Condizione sconosciuta o non gestita: {key}")
        return False


# ---------------------------------------------------------------------------
# Regole dichiarative — BOOKING workflow
# ---------------------------------------------------------------------------

BOOKING_RULES: list[dict] = [

    # ── START_BOOKING ─────────────────────────────────────────────────────────
    # Se il nome completo (>= 2 parole) e' gia' presente → cerca subito disponibilità!
    {
        "rule_id": "BOOKING_START_COMPLETE",
        "workflow": "BOOKING",
        "state": "START_BOOKING",
        "priority": 20,
        "conditions": {"customer_fields_complete": ["full_name"]},
        "actions": [
            {"type": "EXECUTE_ACTION", "action": "SEARCH_AVAILABLE_SLOTS"},
            {"type": "CHANGE_STATE", "value": "WAITING_SLOT_SELECTION"},
        ],
    },
    # Se il nome completo manca o ha una sola parola → chiedi nome e cognome
    {
        "rule_id": "BOOKING_START_MISSING_NAME",
        "workflow": "BOOKING",
        "state": "START_BOOKING",
        "priority": 10,
        "conditions": {},
        "actions": [
            {"type": "CHANGE_STATE", "value": "COLLECT_CUSTOMER_DATA"},
            {"type": "REQUEST_INFORMATION", "field": "full_name", "message_key": "ask_full_name"},
        ],
    },

    # ── COLLECT_CUSTOMER_DATA ─────────────────────────────────────────────────
    # Se sia Nome + Cognome che la conferma del telefono sono completi → cerca disponibilità!
    {
        "rule_id": "BOOKING_COLLECT_COMPLETE",
        "workflow": "BOOKING",
        "state": "COLLECT_CUSTOMER_DATA",
        "priority": 30,
        "conditions": {
            "customer_fields_complete": ["full_name"],
            "phone_confirmed": True,
        },
        "actions": [
            {"type": "EXECUTE_ACTION", "action": "SEARCH_AVAILABLE_SLOTS"},
            {"type": "CHANGE_STATE", "value": "WAITING_SLOT_SELECTION"},
        ],
    },
    # Se Nome + Cognome e' presente ma il telefono non e' ancora confermato → chiedi conferma telefono
    {
        "rule_id": "BOOKING_COLLECT_ASK_PHONE",
        "workflow": "BOOKING",
        "state": "COLLECT_CUSTOMER_DATA",
        "priority": 20,
        "conditions": {
            "customer_fields_complete": ["full_name"],
            "not": {"phone_confirmed": True},
        },
        "actions": [
            {"type": "REQUEST_INFORMATION", "field": "phone", "message_key": "ask_phone_confirmation"}
        ],
    },
    # Se manca Nome + Cognome → chiedi nome e cognome
    {
        "rule_id": "BOOKING_COLLECT_ASK_NAME",
        "workflow": "BOOKING",
        "state": "COLLECT_CUSTOMER_DATA",
        "priority": 10,
        "conditions": {},
        "actions": [
            {"type": "REQUEST_INFORMATION", "field": "full_name", "message_key": "ask_full_name"}
        ],
    },

    # ── READY_FOR_AVAILABILITY_SEARCH ─────────────────────────────────────────
    {
        "rule_id": "BOOKING_SEARCH_001",
        "workflow": "BOOKING",
        "state": "READY_FOR_AVAILABILITY_SEARCH",
        "priority": 10,
        "conditions": {},
        "actions": [
            {"type": "EXECUTE_ACTION", "action": "SEARCH_AVAILABLE_SLOTS"},
            {"type": "CHANGE_STATE", "value": "WAITING_SLOT_SELECTION"},
        ],
    },

    # ── WAITING_SLOT_SELECTION ────────────────────────────────────────────────
    {
        "rule_id": "BOOKING_SLOT_001",
        "workflow": "BOOKING",
        "state": "WAITING_SLOT_SELECTION",
        "priority": 30,
        "conditions": {"slot_selection_valid": True},
        "actions": [
            {"type": "CHANGE_STATE", "value": "WAITING_CONFIRMATION"}
        ],
    },
    {
        "rule_id": "BOOKING_SLOT_002",
        "workflow": "BOOKING",
        "state": "WAITING_SLOT_SELECTION",
        "priority": 20,
        "conditions": {"has_date": True, "has_time": True},
        "actions": [
            {"type": "CHANGE_STATE", "value": "WAITING_CONFIRMATION"}
        ],
    },
    {
        "rule_id": "BOOKING_SLOT_003",
        "workflow": "BOOKING",
        "state": "WAITING_SLOT_SELECTION",
        "priority": 10,
        "conditions": {},
        "actions": [
            {"type": "REQUEST_INFORMATION", "field": "slot", "message_key": "ask_slot_selection"}
        ],
    },

    # ── WAITING_CONFIRMATION ──────────────────────────────────────────────────
    {
        "rule_id": "BOOKING_CONFIRM_001",
        "workflow": "BOOKING",
        "state": "WAITING_CONFIRMATION",
        "priority": 30,
        "conditions": {"confirmation_is": "YES"},
        "actions": [
            {"type": "EXECUTE_ACTION", "action": "CREATE_APPOINTMENT"},
            {"type": "CHANGE_STATE", "value": "BOOKING_COMPLETED"},
        ],
    },
    {
        "rule_id": "BOOKING_CONFIRM_002",
        "workflow": "BOOKING",
        "state": "WAITING_CONFIRMATION",
        "priority": 20,
        "conditions": {"confirmation_is": "NO"},
        "actions": [
            {"type": "CHANGE_STATE", "value": "WAITING_SLOT_SELECTION"},
            {"type": "SEND_RESPONSE", "message_key": "slot_rejected_ask_again"},
        ],
    },
]

# ---------------------------------------------------------------------------
# Regole — RESCHEDULE workflow
# ---------------------------------------------------------------------------

RESCHEDULE_RULES: list[dict] = [

    # ── START_RESCHEDULE ──────────────────────────────────────────────────────
    # Esegue FIND_APPOINTMENT e SEARCH_AVAILABLE_SLOTS immediatamente nello stesso turno,
    # poi passa direttamente a WAITING_SLOT_SELECTION per proporre subito i nuovi slot!
    {
        "rule_id": "RESCHEDULE_START_001",
        "workflow": "RESCHEDULE",
        "state": "START_RESCHEDULE",
        "priority": 10,
        "conditions": {},
        "actions": [
            {"type": "EXECUTE_ACTION", "action": "FIND_APPOINTMENT"},
            {"type": "EXECUTE_ACTION", "action": "SEARCH_AVAILABLE_SLOTS"},
            {"type": "CHANGE_STATE", "value": "WAITING_SLOT_SELECTION"},
        ],
    },

    # ── WAITING_SLOT_SELECTION ────────────────────────────────────────────────
    {
        "rule_id": "RESCHEDULE_SLOT_001",
        "workflow": "RESCHEDULE",
        "state": "WAITING_SLOT_SELECTION",
        "priority": 30,
        "conditions": {"slot_selection_valid": True},
        "actions": [
            {"type": "CHANGE_STATE", "value": "WAITING_CONFIRMATION"}
        ],
    },
    {
        "rule_id": "RESCHEDULE_SLOT_002",
        "workflow": "RESCHEDULE",
        "state": "WAITING_SLOT_SELECTION",
        "priority": 20,
        "conditions": {"has_date": True, "has_time": True},
        "actions": [
            {"type": "CHANGE_STATE", "value": "WAITING_CONFIRMATION"}
        ],
    },
    {
        "rule_id": "RESCHEDULE_SLOT_003",
        "workflow": "RESCHEDULE",
        "state": "WAITING_SLOT_SELECTION",
        "priority": 10,
        "conditions": {},
        "actions": [
            {"type": "REQUEST_INFORMATION", "field": "slot", "message_key": "ask_slot_selection"}
        ],
    },

    # ── WAITING_CONFIRMATION ──────────────────────────────────────────────────
    {
        "rule_id": "RESCHEDULE_CONFIRM_001",
        "workflow": "RESCHEDULE",
        "state": "WAITING_CONFIRMATION",
        "priority": 30,
        "conditions": {"confirmation_is": "YES"},
        "actions": [
            {"type": "EXECUTE_ACTION", "action": "UPDATE_APPOINTMENT"},
            {"type": "CHANGE_STATE", "value": "RESCHEDULE_COMPLETED"},
        ],
    },
    {
        "rule_id": "RESCHEDULE_CONFIRM_002",
        "workflow": "RESCHEDULE",
        "state": "WAITING_CONFIRMATION",
        "priority": 20,
        "conditions": {"confirmation_is": "NO"},
        "actions": [
            {"type": "CHANGE_STATE", "value": "WAITING_SLOT_SELECTION"},
            {"type": "SEND_RESPONSE", "message_key": "slot_rejected_ask_again"},
        ],
    },
]

# ---------------------------------------------------------------------------
# Regole — CANCELLATION workflow
# ---------------------------------------------------------------------------

CANCELLATION_RULES: list[dict] = [

    {
        "rule_id": "CANCEL_START_001",
        "workflow": "CANCELLATION",
        "state": "START_CANCELLATION",
        "priority": 10,
        "conditions": {},
        "actions": [
            {"type": "EXECUTE_ACTION", "action": "FIND_APPOINTMENT"},
            {"type": "CHANGE_STATE", "value": "WAITING_CONFIRMATION"},
        ],
    },
    {
        "rule_id": "CANCEL_CONFIRM_001",
        "workflow": "CANCELLATION",
        "state": "WAITING_CONFIRMATION",
        "priority": 30,
        "conditions": {"confirmation_is": "YES"},
        "actions": [
            {"type": "EXECUTE_ACTION", "action": "CANCEL_APPOINTMENT"},
            {"type": "CHANGE_STATE", "value": "CANCELLATION_COMPLETED"},
        ],
    },
    {
        "rule_id": "CANCEL_CONFIRM_002",
        "workflow": "CANCELLATION",
        "state": "WAITING_CONFIRMATION",
        "priority": 20,
        "conditions": {"confirmation_is": "NO"},
        "actions": [
            {"type": "SEND_RESPONSE", "message_key": "cancellation_aborted"},
            {"type": "RESET_WORKFLOW"},
        ],
    },
]

# ---------------------------------------------------------------------------
# Regole globali — cambio intent mid-flow (alta priorita')
# ---------------------------------------------------------------------------

GLOBAL_RULES: list[dict] = [

    {
        "rule_id": "GLOBAL_INTENT_CHANGE_CANCEL",
        "workflow": None,
        "state": None,
        "priority": 100,
        "conditions": {
            "intent_is": "CANCEL_BOOKING",
            "not": {"workflow_is": "CANCELLATION"},
        },
        "actions": [
            {"type": "CHANGE_WORKFLOW", "workflow": "CANCELLATION", "state": "START_CANCELLATION"}
        ],
    },
    {
        "rule_id": "GLOBAL_INTENT_CHANGE_RESCHEDULE",
        "workflow": None,
        "state": None,
        "priority": 100,
        "conditions": {
            "intent_is": "RESCHEDULE_BOOKING",
            "not": {"workflow_is": "RESCHEDULE"},
        },
        "actions": [
            {"type": "CHANGE_WORKFLOW", "workflow": "RESCHEDULE", "state": "START_RESCHEDULE"}
        ],
    },
    {
        "rule_id": "GLOBAL_INTENT_CHANGE_BOOKING",
        "workflow": None,
        "state": None,
        "priority": 100,
        "conditions": {
            "intent_is": "NEW_BOOKING",
            "not": {"workflow_is": "BOOKING"},
        },
        "actions": [
            {"type": "CHANGE_WORKFLOW", "workflow": "BOOKING", "state": "START_BOOKING"}
        ],
    },
    {
        "rule_id": "GLOBAL_CONSULT_APPT",
        "workflow": None,
        "state": None,
        "priority": 90,
        "conditions": {"intent_is": "CONSULT_APPOINTMENTS"},
        "actions": [
            {"type": "EXECUTE_ACTION", "action": "FIND_APPOINTMENT"},
            {"type": "SEND_RESPONSE", "message_key": "show_appointment"},
        ],
    },
    {
        "rule_id": "GLOBAL_REQUEST_INFO",
        "workflow": None,
        "state": None,
        "priority": 90,
        "conditions": {"intent_is": "REQUEST_INFORMATION"},
        "actions": [
            {"type": "SEND_RESPONSE", "message_key": "information_response"},
            {"type": "RESET_WORKFLOW"},
        ],
    },
    {
        "rule_id": "GLOBAL_SMALLTALK",
        "workflow": None,
        "state": None,
        "priority": 80,
        "conditions": {
            "intent_is": "SMALLTALK",
            "workflow_is": None,
            "not": {"has_selection": True},
        },
        "actions": [
            {"type": "SEND_RESPONSE", "message_key": "smalltalk_response"},
            {"type": "RESET_WORKFLOW"},
        ],
    },
]

ALL_RULES: list[dict] = GLOBAL_RULES + BOOKING_RULES + RESCHEDULE_RULES + CANCELLATION_RULES


# ---------------------------------------------------------------------------
# Rule Engine
# ---------------------------------------------------------------------------

class RuleEngine:
    """
    Motore decisionale dichiarativo.
    Valuta le regole in ordine di priorita' e restituisce la prima Decision applicabile.
    """

    def __init__(self):
        self._evaluator = ConditionEvaluator()
        self._rules = sorted(ALL_RULES, key=lambda r: -r["priority"])

    def evaluate(
        self,
        workflow: Optional[str],
        state: Optional[str],
        conv_data: dict,
        parsing_result: ParsingResult,
    ) -> Optional[Decision]:
        """
        Valuta le regole e restituisce la prima Decision applicabile.
        """
        for rule in self._rules:
            rule_workflow = rule.get("workflow")
            rule_state = rule.get("state")

            if rule_workflow is not None and rule_workflow != workflow:
                continue
            if rule_state is not None and rule_state != state:
                continue

            if self._evaluator.evaluate(
                rule["conditions"], conv_data, parsing_result, workflow=workflow
            ):
                return self._build_decision(rule)

        return None

    def _build_decision(self, rule: dict) -> Decision:
        """Costruisce un oggetto Decision dalla regola applicata."""
        actions = rule.get("actions", [])
        next_state = None
        next_workflow = None

        for action in actions:
            if action.get("type") == "CHANGE_STATE":
                next_state = action.get("value")
            elif action.get("type") == "CHANGE_WORKFLOW":
                next_workflow = action.get("workflow")
                next_state = action.get("state")

        return Decision(
            rule_id=rule["rule_id"],
            actions=actions,
            next_state=next_state,
            next_workflow=next_workflow,
        )
