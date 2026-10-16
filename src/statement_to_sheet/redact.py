"""Account-number and sensitive-pattern redaction.

Redacts BEFORE enrichment or writing outputs. Never returns raw account numbers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional


# Patterns that look like account / routing / IBAN identifiers.
# More specific patterns first. Word boundaries avoid eating words like "PHARMACY".

_ACCOUNT_LABEL = re.compile(
    r"(?i)\b(?:account\s*(?:number|no\.?|#)?|acct\.?\s*(?:number|no\.?|#)?|"
    r"a/?c\s*(?:number|no\.?|#))\s*[:=]?\s*"
    r"([A-Z0-9*][\dA-Z*\s-]{3,30}\d)\b",
)

_IBAN = re.compile(
    r"\b([A-Z]{2}\d{2}[A-Z0-9]{4}\d{7}([A-Z0-9]?){0,16})\b",
)

# Masked forms already present (XXXX1234, ****1234, XXXX-XXXX-1234)
_ALREADY_MASKED = re.compile(
    r"\b([X*]{2,}[\dX*\-]{0,20}\d{4})\b",
    re.IGNORECASE,
)

# Routing numbers (optionally followed by an account number nearby)
_ROUTING_ACCOUNT = re.compile(
    r"(?i)\b(?:routing|aba|rtn)\s*(?:number|no\.?|#)?\s*[:=]?\s*"
    r"(\d{9})\b"
    r"(?:[^0-9]{0,40}?"
    r"(?:account|acct|a/?c)\s*(?:number|no\.?|#)?\s*[:=]?\s*"
    r"(\d{4,17}))?",
)

# Bare digit runs of length 8–17 (common account lengths)
_DIGIT_RUN = re.compile(r"(?<![.\d])(\d{8,17})(?![.\d])")


@dataclass
class RedactionStats:
    """Counts of patterns found and masked (no raw values)."""

    labeled_accounts: int = 0
    ibans: int = 0
    routing_numbers: int = 0
    digit_runs: int = 0
    already_masked_normalized: int = 0
    description_fields_cleaned: int = 0
    total_masked: int = 0
    account_refs_kept: int = 0

    def as_report_lines(self) -> list[str]:
        return [
            "REDACTION REPORT",
            "================",
            "Account-like patterns found and masked (raw values never stored):",
            f"  Labeled account numbers (Account Number: …): {self.labeled_accounts}",
            f"  IBAN-like identifiers:                       {self.ibans}",
            f"  Routing numbers (ABA/rtn):                   {self.routing_numbers}",
            f"  Bare 8–17 digit runs:                        {self.digit_runs}",
            f"  Already-masked forms normalized:             {self.already_masked_normalized}",
            f"  Description fields with redaction applied:   {self.description_fields_cleaned}",
            f"  Total mask operations:                       {self.total_masked}",
            f"  Optional account_ref (****NNNN) columns set: {self.account_refs_kept}",
            "",
            "No full account numbers were written to intermediate or final files.",
        ]


def _mask_keep_last4(raw: str) -> str:
    """Mask a digit/alphanumeric identifier to **** + last 4 digits if present."""
    digits = re.sub(r"\D", "", raw)
    if len(digits) >= 4:
        return f"****{digits[-4:]}"
    return "****"


def _looks_like_date_or_amount(digits: str) -> bool:
    """Heuristic: skip YYYYMMDD-style dates."""
    if len(digits) == 8:
        y, m, d = int(digits[:4]), int(digits[4:6]), int(digits[6:8])
        if 1900 <= y <= 2100 and 1 <= m <= 12 and 1 <= d <= 31:
            return True
    if len(digits) == 6:
        m, d = int(digits[2:4]), int(digits[4:6])
        if 1 <= m <= 12 and 1 <= d <= 31:
            return True
    return False


def redact_text(
    text: str,
    stats: Optional[RedactionStats] = None,
    *,
    keep_account_ref: bool = False,
) -> tuple[str, Optional[str]]:
    """Redact account-like patterns from *text*.

    Returns (redacted_text, account_ref_or_None).
    account_ref is ****NNNN only when keep_account_ref and a clear last-4 exists.
    """
    if not text or not isinstance(text, str):
        return text if text is not None else "", None

    if stats is None:
        stats = RedactionStats()

    account_ref: Optional[str] = None
    result = text

    def _apply(pattern: re.Pattern, kind: str, s: str) -> str:
        nonlocal account_ref

        def repl(m: re.Match) -> str:
            nonlocal account_ref
            raw = m.group(1) if m.lastindex and m.group(1) else m.group(0)
            if kind == "digit_run" and _looks_like_date_or_amount(raw):
                return m.group(0)
            masked = _mask_keep_last4(raw)
            if keep_account_ref and account_ref is None:
                account_ref = masked
                stats.account_refs_kept += 1
            stats.total_masked += 1
            if kind == "labeled":
                stats.labeled_accounts += 1
                full = m.group(0)
                idx = full.find(raw)
                if idx >= 0:
                    return full[:idx] + masked
                return masked
            if kind == "iban":
                stats.ibans += 1
                return masked
            if kind == "routing":
                stats.routing_numbers += 1
                out = m.group(0)
                out = out.replace(m.group(1), "****" + m.group(1)[-4:], 1)
                if m.lastindex and m.lastindex >= 2 and m.group(2):
                    acct = m.group(2)
                    acct_masked = _mask_keep_last4(acct)
                    stats.digit_runs += 1
                    stats.total_masked += 1
                    if keep_account_ref and account_ref is None:
                        account_ref = acct_masked
                        stats.account_refs_kept += 1
                    out = out.replace(acct, acct_masked, 1)
                return out
            if kind == "digit_run":
                stats.digit_runs += 1
                return masked
            if kind == "masked":
                stats.already_masked_normalized += 1
                return masked
            return masked

        return pattern.sub(repl, s)

    result = _apply(_ACCOUNT_LABEL, "labeled", result)
    result = _apply(_IBAN, "iban", result)
    result = _apply(_ROUTING_ACCOUNT, "routing", result)
    result = _apply(_ALREADY_MASKED, "masked", result)
    result = _apply(_DIGIT_RUN, "digit_run", result)

    if not keep_account_ref:
        account_ref = None

    return result, account_ref


def redact_dataframe_columns(
    rows: list[dict],
    text_keys: tuple[str, ...] = ("description", "memo", "payee", "name"),
    *,
    keep_account_ref: bool = False,
    stats: Optional[RedactionStats] = None,
) -> tuple[list[dict], RedactionStats]:
    """Redact sensitive patterns in row dicts."""
    if stats is None:
        stats = RedactionStats()

    sensitive_keys = {
        "account",
        "account_number",
        "account_no",
        "acct",
        "acct_number",
        "iban",
        "routing",
        "routing_number",
    }

    cleaned: list[dict] = []
    for row in rows:
        new_row = dict(row)
        for drop_key in list(new_row.keys()):
            lk = drop_key.lower().replace(" ", "_")
            if lk in sensitive_keys:
                raw_val = str(new_row.pop(drop_key) or "")
                _, ref = redact_text(raw_val, stats, keep_account_ref=keep_account_ref)
                if keep_account_ref and ref and "account_ref" not in new_row:
                    new_row["account_ref"] = ref

        field_hit = False
        for key in text_keys:
            if key in new_row and new_row[key]:
                before = str(new_row[key])
                after, ref = redact_text(before, stats, keep_account_ref=keep_account_ref)
                if after != before:
                    field_hit = True
                new_row[key] = after
                if keep_account_ref and ref and "account_ref" not in new_row:
                    new_row["account_ref"] = ref

        for key, val in list(new_row.items()):
            if key in text_keys or key == "account_ref" or not isinstance(val, str):
                continue
            after, ref = redact_text(val, stats, keep_account_ref=keep_account_ref)
            new_row[key] = after
            if keep_account_ref and ref and "account_ref" not in new_row:
                new_row["account_ref"] = ref

        if field_hit:
            stats.description_fields_cleaned += 1
        cleaned.append(new_row)

    return cleaned, stats
