"""
Utility for filtering and balancing available time slots.

Functions
---------
filter_slots_by_preference  – filter by morning / afternoon / evening
balanced_slot_mix           – return a balanced mix across all active bands
"""

_BANDS: dict[str, tuple[int, int]] = {
    "morning":   (6,  12),
    "afternoon": (12, 18),
    "evening":   (18, 24),
}


def filter_slots_by_preference(
    slots: list[str],
    preference: str | None,
    n: int = 3,
) -> list[str]:
    """
    Return up to *n* slots matching user time-of-day preference.
    """
    if preference and preference in _BANDS:
        start_h, end_h = _BANDS[preference]
        filtered = [
            s for s in slots
            if start_h <= int(s.split(":")[0]) < end_h
        ]
        return filtered[:n] if filtered else []

    return slots[:n]


def balanced_slot_mix(slots: list[str], per_band: int = 2) -> list[str]:
    """
    Return a balanced mix of slots across active time bands (morning, afternoon, evening).
    """
    result = []
    for band_name in ("morning", "afternoon", "evening"):
        start_h, end_h = _BANDS[band_name]
        band_slots = [s for s in slots if start_h <= int(s.split(":")[0]) < end_h]
        result.extend(band_slots[:per_band])
    return result
