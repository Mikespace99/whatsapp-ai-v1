"""
Temporal Parser — parser deterministico per date e orari italiani.

Converte stringhe di date relative (es. "domani", "mercoledi' prossimo", "il 15 agosto")
in date assolute "YYYY-MM-DD" e orari "HH:MM".

NON usa LLM. Risoluzione deterministica al 100%.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Optional

# Mappatura giorni della settimana in italiano
DAY_NAMES = {
    "lunedi": 0, "lunedì": 0, "lun": 0,
    "martedi": 1, "martedì": 1, "mar": 1,
    "mercoledi": 2, "mercoledì": 2, "mer": 2,
    "giovedi": 3, "giovedì": 3, "gio": 3,
    "venerdi": 4, "venerdì": 4, "ven": 4,
    "sabato": 5, "sab": 5,
    "domenica": 6, "dom": 6,
}

# Mappatura mesi in italiano
MONTH_NAMES = {
    "gennaio": 1, "gen": 1,
    "febbraio": 2, "feb": 2,
    "marzo": 3, "mar": 3,
    "aprile": 4, "apr": 4,
    "maggio": 5, "mag": 5,
    "giugno": 6, "giu": 6,
    "luglio": 7, "lug": 7,
    "agosto": 8, "ago": 8,
    "settembre": 9, "set": 9, "sett": 9,
    "ottobre": 10, "ott": 10,
    "novembre": 11, "nov": 11,
    "dicembre": 12, "dic": 12,
}


def parse_date_text(
    date_text: str,
    now: Optional[datetime] = None,
    tz_str: str = "Europe/Rome",
) -> dict:
    """
    Risolve un testo di data relativo/italiano in "YYYY-MM-DD".

    Returns:
        { "date": "YYYY-MM-DD" | None, "certainty": "full" | "partial" | "none" }
    """
    if not date_text or not date_text.strip():
        return {"date": None, "certainty": "none"}

    if now is None:
        now = datetime.now()

    text = date_text.strip().lower()

    # 1. Caso "oggi"
    if "oggi" in text:
        return {"date": now.strftime("%Y-%m-%d"), "certainty": "full"}

    # 2. Caso "domani" / "dopodomani"
    if "dopodomani" in text:
        dt = now + timedelta(days=2)
        return {"date": dt.strftime("%Y-%m-%d"), "certainty": "full"}
    if "domani" in text:
        dt = now + timedelta(days=1)
        return {"date": dt.strftime("%Y-%m-%d"), "certainty": "full"}

    # 3. Caso "tra X giorni"
    match_days = re.search(r"tra\s+(\d+)\s+giorn[io]", text)
    if match_days:
        days = int(match_days.group(1))
        dt = now + timedelta(days=days)
        return {"date": dt.strftime("%Y-%m-%d"), "certainty": "full"}

    # 4. Giorni della settimana (es. "mercoledì", "lunedì prossimo")
    for day_name, target_weekday in DAY_NAMES.items():
        if day_name in text:
            is_next_week = any(w in text for w in ["prossim", "prossimo", "prossima", "dopo"])
            current_weekday = now.weekday()

            days_ahead = target_weekday - current_weekday
            if days_ahead <= 0 or is_next_week:
                days_ahead += 7
            if is_next_week and days_ahead < 7:
                days_ahead += 7

            dt = now + timedelta(days=days_ahead)
            return {"date": dt.strftime("%Y-%m-%d"), "certainty": "full"}

    # 5. Data esplicita con mese (es. "15 agosto", "5/8", "05-08-2026")
    # Formato DD/MM o DD-MM
    match_dm = re.search(r"(\d{1,2})[/\-](\d{1,2})(?:[/\-](\d{2,4}))?", text)
    if match_dm:
        day = int(match_dm.group(1))
        month = int(match_dm.group(2))
        year = int(match_dm.group(3)) if match_dm.group(3) else now.year
        if year < 100:
            year += 2000
        try:
            dt = datetime(year, month, day)
            if dt < now and not match_dm.group(3):
                dt = datetime(year + 1, month, day)
            return {"date": dt.strftime("%Y-%m-%d"), "certainty": "full"}
        except ValueError:
            pass

    # Formato DD Mese (es. "15 agosto", "15 di agosto")
    for month_name, month_num in MONTH_NAMES.items():
        if month_name in text:
            match_d = re.search(r"(\d{1,2})", text)
            day = int(match_d.group(1)) if match_d else 1
            year = now.year
            try:
                dt = datetime(year, month_num, day)
                if dt < now:
                    dt = datetime(year + 1, month_num, day)
                return {"date": dt.strftime("%Y-%m-%d"), "certainty": "full"}
            except ValueError:
                pass

    return {"date": None, "certainty": "none"}


def parse_time_text(time_text: str) -> Optional[str]:
    """
    Risolve un testo di orario in "HH:MM".

    Examples:
        "alle 15" -> "15:00"
        "15:30" -> "15:30"
        "alle 9 di mattina" -> "09:00"
        "pomeriggio" -> None (e' una preferenza di fascia, non orario)
    """
    if not time_text or not time_text.strip():
        return None

    text = time_text.strip().lower()

    # Formato HH:MM o HH.MM
    match_hhmm = re.search(r"(\d{1,2})[:\.](\d{2})", text)
    if match_hhmm:
        h = int(match_hhmm.group(1))
        m = int(match_hhmm.group(2))
        if 0 <= h <= 23 and 0 <= m <= 59:
            return f"{h:02d}:{m:02d}"

    # Formato "alle 15", "ore 9", "15"
    match_h = re.search(r"(?:alle|ore|h)?\s*(\d{1,2})\b", text)
    if match_h:
        h = int(match_h.group(1))
        if "pomeriggio" in text or "sera" in text:
            if h < 12:
                h += 12
        if 0 <= h <= 23:
            return f"{h:02d}:00"

    return None
