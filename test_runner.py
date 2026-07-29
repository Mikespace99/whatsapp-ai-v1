import os, sys
os.environ['OPENAI_API_KEY'] = 'sk-mock-key-for-local-testing'

from datetime import datetime, timedelta
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
from app.db.models import Tenant, UserSession, Appointment
import app.tools.booking as booking_mod
import app.tools.business_manager as biz_mod

# Mock LLM
def mock_call_openai(prompt):
    text = prompt.lower()
    if 'spostare' in text:
        return '{"intent":{"value":"RESCHEDULE_BOOKING","confidence":0.98},"entities":{},"datetime":{},"preferences":{},"selection":{},"confirmation":{},"smalltalk":{}}'
    elif 'secondo' in text:
        return '{"intent":{"value":"SMALLTALK","confidence":0.5},"entities":{},"datetime":{},"preferences":{},"selection":{"index":2},"confirmation":{},"smalltalk":{}}'
    elif 'confermo' in text:
        return '{"intent":{"value":"SMALLTALK","confidence":0.5},"entities":{},"datetime":{},"preferences":{},"selection":{},"confirmation":{"value":"YES"},"smalltalk":{}}'
    return 'OK'

# Mock Calendar Search
def mock_search_available_slots(params, tenant, db, phone_number, customer_name):
    date_str = (datetime.now() + timedelta(days=4)).strftime('%Y-%m-%d')
    return {
        "success": True,
        "data": {
            "available_slots": [
                {"id": "S1", "date": date_str, "time": "09:30"},
                {"id": "S2", "date": date_str, "time": "15:00"},
                {"id": "S3", "date": date_str, "time": "16:30"},
            ],
            "date": date_str,
        }
    }

booking_mod._parser_engine._call_llm = mock_call_openai
booking_mod.call_openai = mock_call_openai
biz_mod.BusinessManager._search_available_slots = mock_search_available_slots

engine = create_engine('sqlite:///:memory:', echo=False)
Base.metadata.create_all(engine)
SessionLocal = sessionmaker(bind=engine)
db = SessionLocal()

tenant = Tenant(
    id=1,
    name='Studio Medico Test',
    title='Dott.',
    last_name='Rossi',
    is_active=True,
    timezone='Europe/Rome',
    work_start_time='09:00',
    work_end_time='17:00',
    working_days='mon,tue,wed,thu,fri',
    slot_duration_minutes=30,
)
db.add(tenant)
db.commit()

future_date = datetime.now() + timedelta(days=3)
future_start = future_date.replace(hour=15, minute=0, second=0, microsecond=0)
future_end = future_start + timedelta(minutes=30)

appt = Appointment(
    tenant_id=1,
    customer_phone='393331234567',
    customer_name='Mario Rossi',
    start_time=future_start,
    end_time=future_end,
    google_event_id='MOCK_EVENT_123',
    status='confirmed',
)
db.add(appt)
db.commit()

print('=== START TEST SIMULAZIONE RESCHEDULE (OPZIONE A) ===')

# PASSO 1
res1 = booking_mod.handle_whatsapp_message('393331234567', 'Buongiorno vorrei spostare appuntamento', 1, db, 'Mario Rossi')
sess1 = db.query(UserSession).filter_by(tenant_id=1, customer_phone='393331234567').first()
print('PASSO 1:')
print('  Workflow:', sess1.workflow, '| Stato:', sess1.conv_state)

# PASSO 2
res2 = booking_mod.handle_whatsapp_message('393331234567', 'Il secondo', 1, db, 'Mario Rossi')
sess2 = db.query(UserSession).filter_by(tenant_id=1, customer_phone='393331234567').first()
print('\nPASSO 2:')
print('  Workflow:', sess2.workflow, '| Stato:', sess2.conv_state)

# PASSO 3
res3 = booking_mod.handle_whatsapp_message('393331234567', 'Sì confermo', 1, db, 'Mario Rossi')
sess3 = db.query(UserSession).filter_by(tenant_id=1, customer_phone='393331234567').first()
print('\nPASSO 3:')
print('  Workflow:', sess3.workflow, '| Stato:', sess3.conv_state)

appts = db.query(Appointment).filter_by(tenant_id=1, customer_phone='393331234567').all()
print('\nStato Appuntamenti nel DB finale:')
for a in appts:
    print(f'  ID={a.id}, Status={a.status}, Start={a.start_time.strftime("%Y-%m-%d %H:%M")}')
