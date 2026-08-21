"""Business-model routing.

Which schema a company gets is a structural decision, not a cosmetic one: it
determines which metrics can even be computed.  Getting it wrong on a Canadian
name is expensive, because the TSX is roughly a third financials by weight.
"""

from __future__ import annotations

import re
from typing import Literal

Schema = Literal["industrial", "bank", "insurer"]

BANK_PATTERNS = (
    r"\bbank(s|ing)?\b", r"\bcredit union\b", r"\bschedule i\b", r"\btrust compan",
    r"\bdiversified bank", r"\bregional bank",
)
INSURER_PATTERNS = (
    r"\binsuranc", r"\binsurer", r"\bassuranc", r"\breinsur", r"\blife (co|insur)",
    r"\bproperty (&|and) casualty\b",
)
#: Financial-sector industries that are NOT banks or insurers and DO admit
#: industrial metrics -- an exchange or an asset manager has real operating
#: margins and a meaningful EV.
NON_BANK_FINANCIAL_PATTERNS = (
    r"\basset manage", r"\bcapital markets\b", r"\bexchange", r"\bbrokerage",
    r"\bfinancial exchange", r"\bmortgage (reit|trust)\b",
)


def classify(sector: str = "", industry: str = "", name: str = "") -> Schema:
    """Route to a schema from sector/industry/name.

    Order matters: insurer patterns are tested before bank patterns because
    several Canadian insurers carry 'Bank' subsidiaries in their names, and
    bank patterns are tested before the sector fallback.
    """
    hay = " ".join((industry or "", name or "")).lower()

    if any(re.search(p, hay) for p in INSURER_PATTERNS):
        return "insurer"
    if any(re.search(p, hay) for p in BANK_PATTERNS):
        return "bank"
    if any(re.search(p, hay) for p in NON_BANK_FINANCIAL_PATTERNS):
        return "industrial"
    if (sector or "").strip().lower() == "financials":
        # Unclassifiable financial: route to bank, the more restrictive schema.
        # Erring toward the restrictive side means we withhold a metric we might
        # have been able to compute; erring the other way means we print an
        # Altman Z for a bank. The first is an inconvenience, the second is a
        # defect.
        return "bank"
    return "industrial"


def is_financial(schema: str) -> bool:
    return schema in ("bank", "insurer")
