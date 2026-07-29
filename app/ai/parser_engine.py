"""
Parser Engine — unico punto di accesso al sistema LLM.

Effettua UNA SOLA chiamata al modello linguistico e restituisce
un ParsingResult strutturato con tutti i 7 domini informativi.

Il Parser Engine:
  - NON prende decisioni
  - NON verifica disponibilita'
  - NON risolve date (lo fa il temporal_parser dopo)
  - Estrae e basta

Flusso:
  messaggio utente
        ↓
  Parser Engine (1 chiamata LLM)
        ↓
  ParsingResult JSON strutturato
        ↓
  Conversation Manager (logica deterministica)
"""

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from app.ai.prompts import build_parser_engine_prompt
from app.tools.temporal_parser import parse_date_text, parse_time_text


# ---------------------------------------------------------------------------
# Struttura dati del Parsing Result
# ---------------------------------------------------------------------------

@dataclass
class IntentResult:
    value: str = "UNKNOWN"          # BOOK_APPOINTMENT | RESCHEDULE_APPOINTMENT | ...
    confidence: float = 0.0


@dataclass
class EntitiesResult:
    full_name: Optional[str] = None
    phone: Optional[str] = None
    service: Optional[str] = None


@dataclass
class DatetimeResult:
    date_text: Optional[str] = None     # testo grezzo, es. "mercoledi' prossimo"
    time_text: Optional[str] = None     # testo grezzo, es. "alle 15"
    resolved_date: Optional[str] = None # YYYY-MM-DD — risolto da temporal_parser
    resolved_time: Optional[str] = None # HH:MM — risolto da temporal_parser
    date_certainty: str = "none"        # full | partial | none


@dataclass
class PreferencesResult:
    time_of_day: Optional[str] = None  # MORNING | AFTERNOON | EVENING
    priority: Optional[str] = None     # EARLIEST_AVAILABLE | SPECIFIC_DATE


@dataclass
class SelectionResult:
    index: Optional[int] = None        # 1-based (il primo=1, il secondo=2, ...)
    value: Optional[str] = None        # testo esatto selezionato


@dataclass
class ConfirmationResult:
    value: Optional[str] = None        # YES | NO | None


@dataclass
class SmalltalkResult:
    type: Optional[str] = None         # GREETING | GOODBYE | THANKS | OTHER


@dataclass
class ParsingResult:
    """
    Output strutturato del Parser Engine.
    Rappresenta la comprensione completa di un messaggio utente.
    """
    intent: IntentResult = field(default_factory=IntentResult)
    entities: EntitiesResult = field(default_factory=EntitiesResult)
    datetime_info: DatetimeResult = field(default_factory=DatetimeResult)
    preferences: PreferencesResult = field(default_factory=PreferencesResult)
    selection: SelectionResult = field(default_factory=SelectionResult)
    confirmation: ConfirmationResult = field(default_factory=ConfirmationResult)
    smalltalk: SmalltalkResult = field(default_factory=SmalltalkResult)

    # Messaggio originale (per debug/logging)
    raw_message: str = ""

    # JSON grezzo restituito dall'LLM (per debug)
    raw_llm_output: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        """Serializza in dict per logging/storage."""
        return {
            "intent": {"value": self.intent.value, "confidence": self.intent.confidence},
            "entities": {
                "full_name": self.entities.full_name,
                "phone": self.entities.phone,
                "service": self.entities.service,
            },
            "datetime": {
                "date_text": self.datetime_info.date_text,
                "time_text": self.datetime_info.time_text,
                "resolved_date": self.datetime_info.resolved_date,
                "resolved_time": self.datetime_info.resolved_time,
                "date_certainty": self.datetime_info.date_certainty,
            },
            "preferences": {
                "time_of_day": self.preferences.time_of_day,
                "priority": self.preferences.priority,
            },
            "selection": {
                "index": self.selection.index,
                "value": self.selection.value,
            },
            "confirmation": {"value": self.confirmation.value},
            "smalltalk": {"type": self.smalltalk.type},
        }


# ---------------------------------------------------------------------------
# Parser Engine
# ---------------------------------------------------------------------------

class ParserEngine:
    """
    Unico punto di accesso all'LLM per l'estrazione di informazioni.

    Usage:
        engine = ParserEngine(llm_caller)
        result: ParsingResult = engine.parse(message, workflow_state, tenant, now)
    """

    def __init__(self, llm_caller):
        """
        Args:
            llm_caller: callable che accetta un prompt str e restituisce str
                        (es. call_openai da engine.py)
        """
        self._call_llm = llm_caller

    def parse(
        self,
        message: str,
        workflow_state: str = "",
        tenant=None,
        now: Optional[datetime] = None,
    ) -> ParsingResult:
        """
        Analizza il messaggio utente e restituisce un ParsingResult strutturato.

        Args:
            message:        Testo del messaggio WhatsApp.
            workflow_state: Stato corrente del workflow (es. "BOOKING/WAITING_SLOT_SELECTION")
                            Fornito al modello solo come contesto, non per prendere decisioni.
            tenant:         Oggetto Tenant (per timezone e contesto).
            now:            Datetime di riferimento (default: datetime.now()).

        Returns:
            ParsingResult con tutti i campi popolati.
        """
        if now is None:
            now = datetime.now()

        tz = getattr(tenant, "timezone", None) or "Europe/Rome"
        current_datetime = now.strftime("%Y-%m-%d %H:%M (%A)")

        # --- Costruisci il prompt compositivo ---
        # Ogni parser contribuisce alla propria sezione.
        # Modificare un parser non richiede toccare gli altri.
        base_prompt = build_parser_engine_prompt(
            current_datetime=current_datetime,
            timezone=tz,
            workflow_state=workflow_state or "nessuno",
        )
        prompt = base_prompt + f"\n\nMessaggio utente: {message}"

        # --- Chiamata LLM ---
        raw_text = self._call_llm(prompt).strip()

        # --- Parse JSON ---
        raw_json = self._extract_json(raw_text)

        # --- Mappa in ParsingResult ---
        result = self._map_to_parsing_result(raw_json, message)

        # --- Risolvi date/ore con temporal_parser (deterministico) ---
        self._resolve_datetime(result, now, tz)

        # Log
        print(
            f"[ParserEngine] intent={result.intent.value} "
            f"date={result.datetime_info.resolved_date} "
            f"time={result.datetime_info.resolved_time} "
            f"confirm={result.confirmation.value} "
            f"selection={result.selection.index}"
        )

        return result

    # -----------------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------------

    def _extract_json(self, text: str) -> dict:
        """Estrae il JSON dalla risposta LLM, tollerante ai markdown wrapper."""
        # Rimuovi blocchi ```json ... ```
        text = re.sub(r"```(?:json)?\s*", "", text).strip().rstrip("`").strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            # Fallback: cerca la prima { ... } nel testo
            match = re.search(r"\{.*\}", text, re.DOTALL)
            if match:
                try:
                    return json.loads(match.group())
                except json.JSONDecodeError:
                    pass
        return {}

    def _get(self, d: dict, *keys, default=None):
        """Navigazione sicura in dict annidati."""
        for key in keys:
            if not isinstance(d, dict):
                return default
            d = d.get(key, default)
            if d is None:
                return default
        return d

    def _map_to_parsing_result(self, raw: dict, message: str) -> ParsingResult:
        """Mappa il dict JSON grezzo nella struttura ParsingResult."""
        get = self._get

        # Intent
        intent = IntentResult(
            value=get(raw, "intent", "value", default="UNKNOWN"),
            confidence=float(get(raw, "intent", "confidence", default=0.0) or 0.0),
        )

        # Entities — schema: { "full_name": { "value": ..., "metadata": {} } }
        entities = EntitiesResult(
            full_name=get(raw, "entities", "full_name", "value"),
            phone=get(raw, "entities", "phone", "value"),
            service=get(raw, "entities", "service", "value"),
        )

        # Datetime — testo grezzo; la risoluzione e' deterministica (temporal_parser)
        dt_info = DatetimeResult(
            date_text=get(raw, "datetime", "date_text"),
            time_text=get(raw, "datetime", "time_text"),
        )

        # Preferences
        preferences = PreferencesResult(
            time_of_day=get(raw, "preferences", "time_of_day"),
            priority=get(raw, "preferences", "priority"),
        )

        # Selection
        raw_index = get(raw, "selection", "index")
        selection = SelectionResult(
            index=int(raw_index) if raw_index is not None else None,
            value=get(raw, "selection", "value"),
        )

        # Confirmation
        confirmation = ConfirmationResult(
            value=get(raw, "confirmation", "value"),
        )

        # Smalltalk
        smalltalk = SmalltalkResult(
            type=get(raw, "smalltalk", "type"),
        )

        return ParsingResult(
            intent=intent,
            entities=entities,
            datetime_info=dt_info,
            preferences=preferences,
            selection=selection,
            confirmation=confirmation,
            smalltalk=smalltalk,
            raw_message=message,
            raw_llm_output=raw,
        )

    def _resolve_datetime(
        self,
        result: ParsingResult,
        now: datetime,
        tz: str,
    ) -> None:
        """
        Risolve i testi grezzi di data/ora in valori assoluti
        usando il temporal_parser deterministico.
        Modifica result in-place.
        """
        dt = result.datetime_info

        if dt.date_text:
            parsed = parse_date_text(dt.date_text, now, tz)
            dt.resolved_date = parsed.get("date")
            dt.date_certainty = parsed.get("certainty", "none")
        
        if dt.time_text:
            dt.resolved_time = parse_time_text(dt.time_text)
