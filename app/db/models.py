from sqlalchemy import Column, Integer, String, DateTime, Boolean, Text, ForeignKey, UniqueConstraint
from datetime import datetime
from app.db.database import Base


class Tenant(Base):
    __tablename__ = "tenants"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)

    # Dati anagrafici
    title = Column(String, nullable=True)
    first_name = Column(String, nullable=True)
    last_name = Column(String, nullable=True)
    email = Column(String, unique=True, index=True, nullable=True)
    contact_phone = Column(String, nullable=True)

    # WhatsApp credentials
    whatsapp_phone_number_id = Column(String, unique=True, index=True, nullable=True)
    whatsapp_access_token = Column(String, nullable=True)

    # Google Calendar OAuth 2.0
    google_access_token = Column(String, nullable=True)
    google_refresh_token = Column(String, nullable=True)
    google_token_expiry = Column(DateTime, nullable=True)

    # Orari di lavoro — Blocco 1
    work_start_time = Column(String, default="09:00", nullable=False)
    work_end_time = Column(String, default="17:00", nullable=False)

    # Orari di lavoro — Blocco 2 (orari spezzati, opzionale)
    work_start_time_2 = Column(String, nullable=True)
    work_end_time_2 = Column(String, nullable=True)

    # Giorni lavorativi: "mon,tue,wed,thu,fri"
    working_days = Column(String, default="mon,tue,wed,thu,fri", nullable=False)

    # Durata slot in minuti
    slot_duration_minutes = Column(Integer, default=30, nullable=False)

    # Buffer tra appuntamenti in minuti
    buffer_minutes = Column(Integer, default=10, nullable=False)

    # Preavviso minimo per prenotare (ore)
    minimum_notice_hours = Column(Integer, default=2, nullable=False)

    # Finestra massima di prenotazione (giorni nel futuro)
    maximum_booking_days = Column(Integer, default=60, nullable=False)

    # Timezone IANA
    timezone = Column(String, default="Europe/Rome", nullable=False)

    # Istruzioni custom per l'AI
    custom_instructions = Column(String, nullable=True)

    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class UserSession(Base):
    """
    Modello della conversazione persistente.

    Struttura JSON in conv_data:
    {
        "customer": {
            "full_name": "Mario Rossi",
            "phone": "3331234567"
        },
        "request": {
            "service": null
        },
        "availability": {
            "slots": [
                { "id": "S1", "date": "2026-08-10", "time": "15:00" }
            ]
        },
        "appointment": {
            "id": null,
            "google_event_id": null,
            "status": null
        }
    }

    Workflow validi:
        BOOKING       – nuova prenotazione
        RESCHEDULE    – modifica appuntamento esistente
        CANCELLATION  – cancellazione appuntamento
        INFORMATION   – richiesta informazioni

    Stati per workflow BOOKING:
        START_BOOKING → COLLECT_CUSTOMER_DATA → READY_FOR_AVAILABILITY_SEARCH
        → WAITING_SLOT_SELECTION → WAITING_CONFIRMATION → BOOKING_COMPLETED

    Stati per workflow RESCHEDULE:
        START_RESCHEDULE → IDENTIFY_APPOINTMENT → COLLECT_NEW_PREFERENCES
        → WAITING_SLOT_SELECTION → WAITING_CONFIRMATION → RESCHEDULE_COMPLETED

    Stati per workflow CANCELLATION:
        START_CANCELLATION → IDENTIFY_APPOINTMENT → WAITING_CONFIRMATION → CANCELLATION_COMPLETED

    Stati per workflow INFORMATION:
        START_INFORMATION → ANSWER_USER → COMPLETED
    """
    __tablename__ = "user_sessions"
    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    customer_phone = Column(String, nullable=False)

    # Workflow attivo (BOOKING / RESCHEDULE / CANCELLATION / INFORMATION)
    workflow = Column(String, nullable=True)

    # Stato corrente all'interno del workflow
    conv_state = Column(String, nullable=True)

    # Dati strutturati della conversazione (JSON)
    # Contiene: customer, request, availability, appointment
    conv_data = Column(Text, nullable=True)

    # --- Campi legacy (mantenuti per retrocompatibilità) ---
    state = Column(String, default="idle")
    temp_date = Column(String, nullable=True)
    temp_time = Column(String, nullable=True)
    pending_action = Column(String, nullable=True)

    last_interaction = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint('tenant_id', 'customer_phone', name='uix_tenant_customer'),
    )


class Appointment(Base):
    __tablename__ = "appointments"
    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    customer_phone = Column(String, nullable=False)
    customer_name = Column(String, nullable=True)

    start_time = Column(DateTime, nullable=False)
    end_time = Column(DateTime, nullable=False)
    google_event_id = Column(String, unique=True, nullable=True)
    status = Column(String, default="confirmed")  # confirmed, cancelled
    created_at = Column(DateTime, default=datetime.utcnow)
