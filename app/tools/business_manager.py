"""
Business Manager — esecutore delle operazioni applicative.

Riceve un'Action dal Conversation Manager e la instrada al servizio corretto.
NON interpreta messaggi. NON gestisce lo stato della conversazione.
NON genera risposte testuali.

Responsabilita':
  ricevere un comando → eseguire l'operazione → restituire un risultato strutturato

Struttura result:
  { "success": True, "data": {...} }
  { "success": False, "error": { "code": "...", "message": "..." } }

Actions supportate:
  SEARCH_AVAILABLE_SLOTS  → calendar.get_available_slots()
  CREATE_APPOINTMENT      → calendar.create_calendar_event() + DB insert
  UPDATE_APPOINTMENT      → calendar.delete + create + DB update
  CANCEL_APPOINTMENT      → calendar.delete + DB update
  FIND_APPOINTMENT        → query DB
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from sqlalchemy.orm import Session as DbSession

from app.db.models import Appointment
from app.tools.calendar import (
    get_available_slots,
    create_calendar_event,
    delete_calendar_event,
    CalendarNotConnectedError,
    CalendarTemporarilyUnavailableError,
)
from app.tools.slot_filter import filter_slots_by_preference, balanced_slot_mix


# ---------------------------------------------------------------------------
# Action Router
# ---------------------------------------------------------------------------

class BusinessManager:
    """
    Instrada le Action ai servizi applicativi corretti.
    Restituisce sempre un dict strutturato { success, data | error }.
    """

    def execute(
        self,
        action: str,
        params: dict,
        tenant,
        db: DbSession,
        phone_number: str,
        customer_name: str = "",
    ) -> dict:
        """
        Esegue l'azione richiesta.

        Args:
            action:        Codice azione (es. "SEARCH_AVAILABLE_SLOTS")
            params:        Parametri specifici dell'azione
            tenant:        Oggetto Tenant SQLAlchemy
            db:            Sessione database
            phone_number:  Numero telefono cliente
            customer_name: Nome cliente (per il titolo dell'evento calendario)

        Returns:
            { "success": bool, "data": dict } o { "success": False, "error": dict }
        """
        handlers = {
            "SEARCH_AVAILABLE_SLOTS": self._search_available_slots,
            "CREATE_APPOINTMENT":     self._create_appointment,
            "UPDATE_APPOINTMENT":     self._update_appointment,
            "CANCEL_APPOINTMENT":     self._cancel_appointment,
            "FIND_APPOINTMENT":       self._find_appointment,
        }

        handler = handlers.get(action)
        if not handler:
            return self._error("UNKNOWN_ACTION", f"Azione non supportata: {action}")

        try:
            return handler(params, tenant, db, phone_number, customer_name)
        except CalendarNotConnectedError as e:
            return self._error("CALENDAR_NOT_CONNECTED", str(e))
        except CalendarTemporarilyUnavailableError as e:
            return self._error("CALENDAR_UNAVAILABLE", str(e))
        except Exception as e:
            print(f"[BusinessManager] Errore inatteso in {action}: {e}")
            return self._error("INTERNAL_ERROR", str(e))

    # -----------------------------------------------------------------------
    # SEARCH_AVAILABLE_SLOTS
    # -----------------------------------------------------------------------

    def _search_available_slots(self, params, tenant, db, phone_number, customer_name) -> dict:
        """
        Cerca slot disponibili nel calendario per la data/preferenza indicata.

        params:
            date_str:     YYYY-MM-DD — data specifica (opzionale)
            time_of_day:  MORNING | AFTERNOON | EVENING (opzionale)
            max_days:     quanti giorni cercare se nessuna data specifica (default 7)
        """
        date_str = params.get("date_str")
        time_of_day = params.get("time_of_day")
        max_days = int(params.get("max_days", 7))

        if date_str:
            # Data specifica richiesta
            all_slots = get_available_slots(tenant, date_str, db)
            found_date = date_str
        else:
            # Nessuna data: cerca dal primo giorno disponibile
            from datetime import datetime, timedelta
            today = datetime.now().strftime("%Y-%m-%d")
            found_date, all_slots = _find_next_available(tenant, today, db, max_days)

        if not all_slots:
            return {
                "success": True,
                "data": {
                    "available_slots": [],
                    "date": found_date,
                    "message": "no_slots_found",
                }
            }

        # Applica filtro fascia oraria
        if time_of_day:
            pref_map = {"MORNING": "morning", "AFTERNOON": "afternoon", "EVENING": "evening"}
            filtered = filter_slots_by_preference(all_slots, pref_map.get(time_of_day))
        else:
            filtered = balanced_slot_mix(all_slots)

        # Formato slot per il Conversation State
        slot_objects = [
            {"id": f"S{i+1}", "date": found_date, "time": t}
            for i, t in enumerate(filtered)
        ]

        return {
            "success": True,
            "data": {
                "available_slots": slot_objects,
                "date": found_date,
            }
        }

    # -----------------------------------------------------------------------
    # CREATE_APPOINTMENT
    # -----------------------------------------------------------------------

    def _create_appointment(self, params, tenant, db, phone_number, customer_name) -> dict:
        """
        Crea l'appuntamento sul calendario Google e nel DB.

        params:
            date_str:  YYYY-MM-DD
            time_str:  HH:MM
        """
        date_str = params.get("date_str")
        time_str = params.get("time_str")

        if not date_str or not time_str:
            return self._error("MISSING_PARAMS", "date_str e time_str richiesti per CREATE_APPOINTMENT")

        slot_minutes = getattr(tenant, "slot_duration_minutes", None) or 30
        summary = f"Appuntamento con {customer_name or 'Cliente'}"
        description = f"Creato tramite Assistente WhatsApp AI\nCliente: {phone_number}"

        event_id = create_calendar_event(
            tenant=tenant,
            date_str=date_str,
            time_str=time_str,
            summary=summary,
            description=description,
            db=db,
        )

        start_dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
        end_dt = start_dt + timedelta(minutes=slot_minutes)

        appt = Appointment(
            tenant_id=tenant.id,
            customer_phone=phone_number,
            customer_name=customer_name or "",
            start_time=start_dt,
            end_time=end_dt,
            google_event_id=event_id,
            status="confirmed",
        )
        db.add(appt)
        db.commit()
        db.refresh(appt)

        return {
            "success": True,
            "data": {
                "appointment": {
                    "id": str(appt.id),
                    "google_event_id": event_id,
                    "status": "CONFIRMED",
                    "date": date_str,
                    "time": time_str,
                }
            }
        }

    # -----------------------------------------------------------------------
    # UPDATE_APPOINTMENT (reschedule)
    # -----------------------------------------------------------------------

    def _update_appointment(self, params, tenant, db, phone_number, customer_name) -> dict:
        """
        Sposta un appuntamento: cancella il vecchio e crea il nuovo.

        params:
            date_str:       YYYY-MM-DD — nuova data
            time_str:       HH:MM — nuovo orario
            old_event_id:   Google Event ID del vecchio appuntamento (opzionale)
        """
        date_str = params.get("date_str")
        time_str = params.get("time_str")
        old_event_id = params.get("old_event_id")

        if not date_str or not time_str:
            return self._error("MISSING_PARAMS", "date_str e time_str richiesti per UPDATE_APPOINTMENT")

        # Cancella vecchio evento
        if old_event_id:
            delete_calendar_event(tenant, old_event_id, db)

        # Marca vecchio appuntamento come cancellato nel DB
        old_appt = (
            db.query(Appointment)
            .filter(
                Appointment.tenant_id == tenant.id,
                Appointment.customer_phone == phone_number,
                Appointment.status == "confirmed",
                Appointment.start_time >= datetime.now(),
            )
            .order_by(Appointment.start_time.asc())
            .first()
        )
        if old_appt:
            old_appt.status = "cancelled"
            db.commit()

        # Crea nuovo appuntamento
        return self._create_appointment(
            {"date_str": date_str, "time_str": time_str},
            tenant, db, phone_number, customer_name
        )

    # -----------------------------------------------------------------------
    # CANCEL_APPOINTMENT
    # -----------------------------------------------------------------------

    def _cancel_appointment(self, params, tenant, db, phone_number, customer_name) -> dict:
        """
        Cancella l'appuntamento attivo del cliente.

        params:
            google_event_id: (opzionale, se non passato lo cerca nel DB)
        """
        appt = (
            db.query(Appointment)
            .filter(
                Appointment.tenant_id == tenant.id,
                Appointment.customer_phone == phone_number,
                Appointment.status == "confirmed",
                Appointment.start_time >= datetime.now(),
            )
            .order_by(Appointment.start_time.asc())
            .first()
        )

        if not appt:
            return self._error("NO_APPOINTMENT", "Nessun appuntamento attivo trovato")

        if appt.google_event_id:
            delete_calendar_event(tenant, appt.google_event_id, db)

        cancelled_at = appt.start_time.strftime("%Y-%m-%d %H:%M")
        appt.status = "cancelled"
        db.commit()

        return {
            "success": True,
            "data": {
                "appointment": {
                    "id": str(appt.id),
                    "status": "CANCELLED",
                    "was_scheduled_at": cancelled_at,
                }
            }
        }

    # -----------------------------------------------------------------------
    # FIND_APPOINTMENT
    # -----------------------------------------------------------------------

    def _find_appointment(self, params, tenant, db, phone_number, customer_name) -> dict:
        """Recupera il prossimo appuntamento attivo del cliente."""
        appt = (
            db.query(Appointment)
            .filter(
                Appointment.tenant_id == tenant.id,
                Appointment.customer_phone == phone_number,
                Appointment.status == "confirmed",
                Appointment.start_time >= datetime.now(),
            )
            .order_by(Appointment.start_time.asc())
            .first()
        )

        if not appt:
            return {
                "success": True,
                "data": {"appointment": None}
            }

        return {
            "success": True,
            "data": {
                "appointment": {
                    "id": str(appt.id),
                    "google_event_id": appt.google_event_id,
                    "status": "CONFIRMED",
                    "date": appt.start_time.strftime("%Y-%m-%d"),
                    "time": appt.start_time.strftime("%H:%M"),
                    "datetime_display": appt.start_time.strftime("%d/%m/%Y alle %H:%M"),
                }
            }
        }

    # -----------------------------------------------------------------------
    # Helper
    # -----------------------------------------------------------------------

    @staticmethod
    def _error(code: str, message: str) -> dict:
        return {
            "success": False,
            "error": {"code": code, "message": message}
        }


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def _find_next_available(tenant, start_date_str: str, db: DbSession, max_days: int = 7):
    """Cerca il primo giorno con slot disponibili a partire da start_date_str."""
    base = datetime.strptime(start_date_str, "%Y-%m-%d")
    for i in range(max_days):
        check = (base + timedelta(days=i)).strftime("%Y-%m-%d")
        slots = get_available_slots(tenant, check, db)
        if slots:
            return check, slots
    return None, []
