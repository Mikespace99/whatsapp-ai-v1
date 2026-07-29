"""
Prompts del Parser Engine — approccio compositivo.

Il prompt finale inviato all'LLM e' costruito concatenando le specifiche
di ogni parser logico nell'ordine definito.

Ogni PARSER_SPEC_* e' una sezione indipendente.
Modificare un parser NON richiede toccare gli altri.

Flusso:
  Intent Parser Spec
       │
  Entity Parser Spec
       │
  Datetime Parser Spec
       │
  Preference Parser Spec
       │
  Selection Parser Spec
       │
  Confirmation Parser Spec
       │
  Smalltalk Parser Spec
       │
       ▼
  Prompt Finale → OpenAI API → Parsing Result JSON
"""

# ---------------------------------------------------------------------------
# Preamble — introduce il ruolo del sistema
# ---------------------------------------------------------------------------

PARSER_ENGINE_PREAMBLE = """\
Sei il Parser Engine di un sistema di prenotazione appuntamenti via WhatsApp.
Ricevi un messaggio di un utente e devi analizzarlo in modo strutturato.

In un'UNICA analisi devi applicare tutti i parser logici elencati di seguito.
Ogni parser e' responsabile di una specifica dimensione informativa.
I parser sono INDIPENDENTI: l'output di uno non influenza l'analisi degli altri.

Contesto conversazione:
  Data e ora corrente : {current_datetime}
  Timezone            : {timezone}
  Stato workflow      : {workflow_state}

VINCOLI ASSOLUTI validi per TUTTI i parser:
  - Estrai SOLO informazioni esplicitamente presenti nel testo.
  - Non inventare, non inferire, non completare dati parziali.
  - Non prendere decisioni operative.
  - Non generare testo per l'utente.
  - Non eseguire azioni.
  - Restituisci SEMPRE il JSON completo con tutti i campi, anche se null.
"""

# ---------------------------------------------------------------------------
# 1. Intent Parser
# ---------------------------------------------------------------------------

PARSER_SPEC_INTENT = """\
--- INTENT PARSER ---
Identifica l'intenzione principale dell'utente.
Scegli esclusivamente UNO dei seguenti codici:

  NEW_BOOKING          – vuole prenotare un nuovo appuntamento
  CONSULT_APPOINTMENTS – vuole sapere quando e' il suo appuntamento
  RESCHEDULE_BOOKING   – ha un appuntamento esistente e vuole SPOSTARLO
  CANCEL_BOOKING       – vuole cancellare un appuntamento esistente
  REQUEST_INFORMATION  – chiede informazioni (orari, costi, servizi, indirizzo)
  SMALLTALK            – messaggio conversazionale senza intento operativo

Regole:
  - UN solo intent per messaggio.
  - RESCHEDULE_BOOKING solo se il cliente ha gia' un appuntamento e vuole spostarlo.
  - Se l'utente si contraddice, usa l'ULTIMA intenzione espressa.
  - In caso di dubbio → SMALLTALK.

Non devi estrarre date, rispondere all'utente o eseguire azioni.

Output atteso:
  "intent": { "value": "<CODICE>", "confidence": <0.0-1.0> }
"""

# ---------------------------------------------------------------------------
# 2. Entity Extraction Parser
# ---------------------------------------------------------------------------

PARSER_SPEC_ENTITY = """\
--- ENTITY EXTRACTION PARSER ---
Estrai esclusivamente le informazioni ESPLICITAMENTE presenti nel messaggio.

Entita' da estrarre:
  full_name – nome e cognome completo della persona
  phone     – numero di telefono (rimuovi spazi, preserva il formato)
  service   – tipo di servizio o visita richiesta

Per ogni entita':
  value    = testo esatto estratto (null se non presente)
  metadata = proprieta' oggettive (es. {{"format": "italian_mobile"}})

Non devi:
  - interpretare intenzioni;
  - valutare se i dati siano sufficienti;
  - completare o correggere dati parziali.

Output atteso:
  "entities": {{
    "full_name": {{ "value": null, "metadata": {{}} }},
    "phone":     {{ "value": null, "metadata": {{}} }},
    "service":   {{ "value": null, "metadata": {{}} }}
  }}
"""

# ---------------------------------------------------------------------------
# 3. Datetime Parser
# ---------------------------------------------------------------------------

PARSER_SPEC_DATETIME = """\
--- DATETIME PARSER ---
Individua riferimenti temporali nel messaggio e restituiscili come testo GREZZO.
NON convertire in date assolute: lo fara' il sistema deterministico.

Regole:
  - date_text = testo esatto scritto dall'utente per la data
    Esempi: "domani", "mercoledi' prossimo", "il 5 agosto", "tra tre giorni"
  - time_text = testo esatto scritto dall'utente per l'orario
    Esempi: "alle 15", "verso le 10:30", "di mattina", "nel pomeriggio"
  - confidence:
      HIGH  – riferimento chiaro e non ambiguo
      LOW   – riferimento ambiguo o incompleto
      NONE  – nessun riferimento temporale presente

Non devi:
  - verificare disponibilita';
  - creare appuntamenti;
  - proporre alternative;
  - interpretare preferenze non temporali.

Output atteso:
  "datetime": {{
    "date_text": null,
    "time_text": null,
    "confidence": "NONE"
  }}
"""

# ---------------------------------------------------------------------------
# 4. Preference Parser
# ---------------------------------------------------------------------------

PARSER_SPEC_PREFERENCE = """\
--- PREFERENCE PARSER ---
Individua le preferenze espresse dall'utente (criteri di ricerca, non appuntamenti).

time_of_day (fascia oraria preferita):
  MORNING   – mattina (se l'utente non ha specificato un orario preciso)
  AFTERNOON – pomeriggio
  EVENING   – sera
  null      – nessuna preferenza di fascia, o se ha gia' indicato orario preciso

priority (urgenza/flessibilita'):
  EARLIEST_AVAILABLE – "prima possibile", "al piu' presto", "appena disponibile"
  SPECIFIC_DATE      – l'utente indica una data precisa
  FLEXIBLE           – "quando vuoi", "non ho preferenze", "come capita"
  null               – non espressa

Regola critica:
  Se l'utente ha indicato un orario preciso in time_text → time_of_day: null

Non devi verificare disponibilita', interpretare date o prendere decisioni.

Output atteso:
  "preferences": {{
    "time_of_day": null,
    "priority": null
  }}
"""

# ---------------------------------------------------------------------------
# 5. Selection Parser
# ---------------------------------------------------------------------------

PARSER_SPEC_SELECTION = """\
--- SELECTION PARSER ---
Riconosci quando l'utente sta scegliendo un elemento da una lista proposta.

La selezione puo' avvenire:
  - per indice ordinale : "il primo" → 1, "il secondo" → 2, "il 3" → 3
  - per contenuto       : "quello delle 15:00" → value: "15:00"
  - per negazione       : "non il primo, il secondo" → index: 2

Regole:
  - Una semplice conferma ("si", "ok") NON e' una selezione → index: null, value: null
  - NON verificare se la selezione e' valida rispetto alle opzioni (lo fa il sistema).
  - Se non c'e' selezione esplicita → entrambi null.

Output atteso:
  "selection": {{
    "index": null,
    "value": null
  }}
"""

# ---------------------------------------------------------------------------
# 6. Confirmation Parser
# ---------------------------------------------------------------------------

PARSER_SPEC_CONFIRMATION = """\
--- CONFIRMATION PARSER ---
Identifica se l'utente sta confermando o rifiutando qualcosa proposto dal sistema.

YES – conferma:
  si, si', si', confermo, ok, va bene, perfetto, esatto, procedi,
  certo, assolutamente, d'accordo, ottimo, giusto, corretto

NO – rifiuto:
  no, non va bene, annulla, cambia, voglio cambiare,
  non e' corretto, sbagliato, diverso, aspetta, fermati

null – nessuna conferma o rifiuto esplicito:
  - Una selezione ("il secondo") NON e' una conferma.
  - Un saluto NON e' una conferma.
  - Se ambiguo → null.

Non devi decidere cosa fare dopo la conferma (e' compito del Conversation Manager).

Output atteso:
  "confirmation": {{
    "value": null
  }}
"""

# ---------------------------------------------------------------------------
# 7. Smalltalk Parser
# ---------------------------------------------------------------------------

PARSER_SPEC_SMALLTALK = """\
--- SMALLTALK PARSER ---
Identifica se il messaggio e' prevalentemente conversazionale, senza intento operativo.

Categorie:
  GREETING  – saluti: "ciao", "buongiorno", "salve"
  GOODBYE   – commiati: "arrivederci", "a presto"
  THANKS    – ringraziamenti: "grazie", "grazie mille"
  COMPLAINT – frustrazione senza richiesta operativa
  OTHER     – altro contenuto conversazionale non operativo
  null      – il messaggio ha un intento operativo (prevale l'Intent Parser)

Regola critica:
  Se il messaggio contiene ANCHE un intento operativo → type: null
  Esempio: "Grazie, voglio anche prenotare" → type: null

Output atteso:
  "smalltalk": {{
    "type": null
  }}
"""

# ---------------------------------------------------------------------------
# Output schema — chiude il prompt con il contratto JSON atteso
# ---------------------------------------------------------------------------

PARSER_ENGINE_OUTPUT_SCHEMA = """\
--- OUTPUT ATTESO ---
Restituisci ESCLUSIVAMENTE un oggetto JSON valido con questa struttura.
Nessun testo aggiuntivo. Nessun markdown. Nessun ```json.

{{
  "intent":       {{ "value": "<CODICE>", "confidence": 0.0 }},
  "entities":     {{
    "full_name": {{ "value": null, "metadata": {{}} }},
    "phone":     {{ "value": null, "metadata": {{}} }},
    "service":   {{ "value": null, "metadata": {{}} }}
  }},
  "datetime":     {{ "date_text": null, "time_text": null, "confidence": "NONE" }},
  "preferences":  {{ "time_of_day": null, "priority": null }},
  "selection":    {{ "index": null, "value": null }},
  "confirmation": {{ "value": null }},
  "smalltalk":    {{ "type": null }}
}}
"""

# ---------------------------------------------------------------------------
# Builder — compone il prompt finale dalle sezioni dei parser
# ---------------------------------------------------------------------------

_PARSER_SECTIONS = [
    PARSER_SPEC_INTENT,
    PARSER_SPEC_ENTITY,
    PARSER_SPEC_DATETIME,
    PARSER_SPEC_PREFERENCE,
    PARSER_SPEC_SELECTION,
    PARSER_SPEC_CONFIRMATION,
    PARSER_SPEC_SMALLTALK,
]


def build_parser_engine_prompt(
    current_datetime: str,
    timezone: str,
    workflow_state: str,
) -> str:
    """
    Compone il prompt finale concatenando:
      1. Preamble (contesto + vincoli globali)
      2. Sezione di ciascun parser logico (nell'ordine definito)
      3. Schema output JSON atteso

    Ogni parser contribuisce SOLO alla propria sezione.
    Per modificare un parser basta aggiornare la sua PARSER_SPEC_* constant.

    Args:
        current_datetime:  Data e ora corrente formattata (es. "2026-07-29 08:30 (Tuesday)")
        timezone:          Timezone IANA (es. "Europe/Rome")
        workflow_state:    Stato workflow corrente (es. "BOOKING/WAITING_SLOT_SELECTION")

    Returns:
        Stringa prompt completa da inviare all'LLM.
    """
    preamble = PARSER_ENGINE_PREAMBLE.format(
        current_datetime=current_datetime,
        timezone=timezone,
        workflow_state=workflow_state or "nessuno",
    )

    sections = "\n\n".join(_PARSER_SECTIONS)

    return "\n\n".join([preamble, sections, PARSER_ENGINE_OUTPUT_SCHEMA])


# ---------------------------------------------------------------------------
# Response Generator
# ---------------------------------------------------------------------------

RESPONSE_GENERATOR_PROMPT = """\
Sei un assistente di prenotazione professionale che risponde via WhatsApp.
Tono: cordiale, conciso, diretto. Scrivi sempre in italiano.

REGOLE:
1. Non inventare informazioni non presenti nel contesto operativo.
2. Se ci sono slot disponibili, elencali ESATTAMENTE come forniti — niente di piu'.
3. Conferma SOLO operazioni descritte come completate nel contesto.
4. Non fornire consigli medici, legali o specialistici.
5. Mantieni i messaggi brevi: max 3-4 righe.
6. Non usare markdown (no *, no _, no #): WhatsApp mostra testo semplice.
7. Usa emoji con parsimonia (max 1-2 per messaggio) solo se appropriato.

{tenant_context}

CONTESTO OPERATIVO:
{operation_context}
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def build_tenant_context(tenant) -> str:
    """Costruisce il blocco contesto tenant da iniettare nel Response Generator."""
    if not tenant:
        return ""

    lines = ["Informazioni sul professionista:"]

    name = getattr(tenant, "name", None)
    if name:
        lines.append(f"- Studio: {name}")

    title = getattr(tenant, "title", None) or ""
    last_name = getattr(tenant, "last_name", None) or ""
    full_name = f"{title} {last_name}".strip()
    if full_name:
        lines.append(f"- Professionista: {full_name}")

    custom = getattr(tenant, "custom_instructions", None)
    if custom:
        lines.append(f"- Istruzioni specifiche: {custom}")

    phone = getattr(tenant, "contact_phone", None)
    if phone:
        lines.append(f"- Contatto diretto: {phone}")

    return "\n".join(lines)


# Alias per compatibilita' legacy
PARSER_ENGINE_PROMPT = build_parser_engine_prompt(
    current_datetime="{current_datetime}",
    timezone="{timezone}",
    workflow_state="{workflow_state}",
)

INTENT_EXTRACTION_PROMPT = PARSER_ENGINE_PROMPT
CONVERSATIONAL_REPLY_PROMPT = RESPONSE_GENERATOR_PROMPT
