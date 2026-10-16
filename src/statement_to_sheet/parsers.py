"""Local parsers for CSV, OFX/QFX, and PDF bank statements.

No network calls. Redaction is applied by the caller before writing outputs.
"""

from __future__ import annotations

import csv
import io
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Optional


STANDARD_COLUMNS = (
    "date",
    "description",
    "amount",
    "type",
    "balance",
    "source_file",
    "currency",
)


def _parse_date(value: Any) -> Optional[str]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    s = str(value).strip()
    for fmt in (
        "%Y-%m-%d",
        "%m/%d/%Y",
        "%m/%d/%y",
        "%d/%m/%Y",
        "%d-%m-%Y",
        "%Y/%m/%d",
        "%b %d, %Y",
        "%B %d, %Y",
        "%d %b %Y",
        "%Y%m%d",
    ):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    # Already ISO-ish
    m = re.match(r"(\d{4}-\d{2}-\d{2})", s)
    if m:
        return m.group(1)
    return s


def _parse_amount(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    # Parentheses for negatives: (123.45)
    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg = True
        s = s[1:-1]
    s = s.replace("$", "").replace(",", "").replace(" ", "")
    if s.startswith("+"):
        s = s[1:]
    try:
        amt = float(s)
        return -amt if neg else amt
    except ValueError:
        return None


def _infer_type(amount: Optional[float], explicit: Any = None) -> Optional[str]:
    if explicit:
        e = str(explicit).strip().lower()
        if e in {"debit", "dr", "withdrawal", "payment", "charge"}:
            return "debit"
        if e in {"credit", "cr", "deposit", "refund"}:
            return "credit"
    if amount is None:
        return None
    if amount < 0:
        return "debit"
    if amount > 0:
        return "credit"
    return None


def _normalize_header(h: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", h.strip().lower()).strip("_")


# Map common bank CSV headers → our fields
_HEADER_MAP = {
    "date": "date",
    "transaction_date": "date",
    "posted_date": "date",
    "posting_date": "date",
    "trans_date": "date",
    "description": "description",
    "memo": "description",
    "payee": "description",
    "narrative": "description",
    "details": "description",
    "transaction_description": "description",
    "amount": "amount",
    "transaction_amount": "amount",
    "debit": "debit",
    "credit": "credit",
    "withdrawal": "debit",
    "deposit": "credit",
    "type": "type",
    "transaction_type": "type",
    "balance": "balance",
    "running_balance": "balance",
    "currency": "currency",
    "curr": "currency",
    "account": "account",
    "account_number": "account",
    "account_no": "account",
    "acct": "account",
}


def parse_csv(path: Path) -> list[dict]:
    """Parse a bank CSV into raw row dicts (pre-redaction)."""
    text = path.read_text(encoding="utf-8", errors="replace")
    # Skip BOM
    if text.startswith("\ufeff"):
        text = text[1:]

    # Try to find header row (some banks put metadata first)
    lines = text.splitlines()
    header_idx = 0
    for i, line in enumerate(lines[:30]):
        lower = line.lower()
        if "date" in lower and (
            "amount" in lower or "debit" in lower or "credit" in lower or "description" in lower
        ):
            header_idx = i
            break

    usable = "\n".join(lines[header_idx:])
    reader = csv.DictReader(io.StringIO(usable))
    if not reader.fieldnames:
        return []

    mapped_fields = {}
    for h in reader.fieldnames:
        if h is None:
            continue
        key = _HEADER_MAP.get(_normalize_header(h))
        if key:
            mapped_fields[h] = key

    rows: list[dict] = []
    for raw in reader:
        if not any((v or "").strip() for v in raw.values() if v is not None):
            continue
        item: dict[str, Any] = {"source_file": path.name}
        debit = credit = None
        for src, dest in mapped_fields.items():
            val = raw.get(src)
            if dest == "debit":
                debit = _parse_amount(val)
            elif dest == "credit":
                credit = _parse_amount(val)
            elif dest == "date":
                item["date"] = _parse_date(val)
            elif dest == "amount":
                item["amount"] = _parse_amount(val)
            elif dest == "balance":
                item["balance"] = _parse_amount(val)
            elif dest == "description":
                # Prefer longer description if multiple map to same
                existing = item.get("description") or ""
                candidate = (val or "").strip()
                if len(candidate) > len(existing):
                    item["description"] = candidate
            elif dest == "type":
                item["type"] = val
            elif dest == "currency":
                item["currency"] = (val or "").strip() or None
            elif dest == "account":
                item["account"] = val

        if item.get("amount") is None:
            if debit is not None and debit != 0:
                item["amount"] = -abs(debit)
            elif credit is not None and credit != 0:
                item["amount"] = abs(credit)
            elif debit is not None or credit is not None:
                # both zero / empty → skip empty transaction rows sometimes
                d = debit or 0.0
                c = credit or 0.0
                if d or c:
                    item["amount"] = abs(c) - abs(d)

        item["type"] = _infer_type(item.get("amount"), item.get("type"))
        if not item.get("description"):
            item["description"] = ""
        if item.get("date") or item.get("amount") is not None:
            rows.append(item)
    return rows


def parse_ofx(path: Path) -> list[dict]:
    """Parse OFX/QFX using ofxparse."""
    try:
        from ofxparse import OfxParser
    except ImportError as e:
        raise RuntimeError(
            "ofxparse is required for OFX/QFX. Install with: pip install ofxparse"
        ) from e

    with path.open("rb") as fh:
        ofx = OfxParser.parse(fh)

    rows: list[dict] = []
    accounts = getattr(ofx, "accounts", None) or []
    if not accounts and getattr(ofx, "account", None):
        accounts = [ofx.account]

    for acct in accounts:
        currency = None
        try:
            currency = getattr(acct.statement, "currency", None)
        except Exception:
            pass
        # Capture account id only so redaction can mask it — never write raw later
        acct_id = getattr(acct, "account_id", None) or getattr(acct, "number", None)
        statement = getattr(acct, "statement", None)
        if statement is None:
            continue
        for txn in getattr(statement, "transactions", []) or []:
            amount = getattr(txn, "amount", None)
            try:
                amount = float(amount) if amount is not None else None
            except (TypeError, ValueError):
                amount = None
            payee = getattr(txn, "payee", None) or ""
            memo = getattr(txn, "memo", None) or ""
            desc = " ".join(p for p in (payee, memo) if p).strip()
            date = getattr(txn, "date", None)
            row: dict[str, Any] = {
                "date": _parse_date(date),
                "description": desc,
                "amount": amount,
                "type": _infer_type(amount, getattr(txn, "type", None)),
                "balance": None,
                "source_file": path.name,
                "currency": currency,
            }
            if acct_id:
                row["account"] = str(acct_id)
            rows.append(row)

        # Closing balance if present
        bal = getattr(statement, "balance", None)
        if bal is not None and rows:
            try:
                rows[-1].setdefault("balance", float(bal))
            except (TypeError, ValueError):
                pass
    return rows


_PDF_LINE_RE = re.compile(
    r"^(\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{4}-\d{2}-\d{2})\s+"
    r"(.+?)\s+"
    r"(-?\$?\d{1,3}(?:,\d{3})*(?:\.\d{2})?|\(\$?\d[\d,]*\.\d{2}\))"
    r"(?:\s+(-?\$?\d{1,3}(?:,\d{3})*(?:\.\d{2})?))?\s*$"
)


def parse_pdf(path: Path) -> list[dict]:
    """Best-effort PDF text extraction → transaction rows.

    PDF layouts vary widely; prefer CSV/OFX from the bank when available.
    """
    try:
        import pdfplumber
    except ImportError as e:
        raise RuntimeError(
            "pdfplumber is required for PDF. Install with: pip install pdfplumber"
        ) from e

    rows: list[dict] = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            # Prefer tables when present
            tables = page.extract_tables() or []
            for table in tables:
                if not table:
                    continue
                header = None
                for i, row in enumerate(table):
                    cells = [(c or "").strip() for c in row]
                    if header is None:
                        joined = " ".join(cells).lower()
                        if "date" in joined and (
                            "amount" in joined or "debit" in joined or "description" in joined
                        ):
                            header = [_normalize_header(c) for c in cells]
                        continue
                    if not header:
                        continue
                    raw = {}
                    for h, c in zip(header, cells):
                        if h:
                            raw[h] = c
                    # Build a synthetic CSV-like dict via temporary mapping
                    item: dict[str, Any] = {"source_file": path.name}
                    date_val = None
                    for k in ("date", "transaction_date", "posted_date"):
                        if k in raw:
                            date_val = raw[k]
                            break
                    item["date"] = _parse_date(date_val)
                    desc = ""
                    for k in ("description", "memo", "payee", "details", "narrative"):
                        if raw.get(k):
                            desc = raw[k]
                            break
                    if not desc:
                        # take first non-date non-amount looking cell
                        for k, v in raw.items():
                            if k in ("date", "amount", "balance", "debit", "credit", "type"):
                                continue
                            if v:
                                desc = v
                                break
                    item["description"] = desc
                    amt = None
                    if "amount" in raw:
                        amt = _parse_amount(raw["amount"])
                    elif "debit" in raw or "credit" in raw:
                        d = _parse_amount(raw.get("debit"))
                        c = _parse_amount(raw.get("credit"))
                        if d:
                            amt = -abs(d)
                        elif c:
                            amt = abs(c)
                    item["amount"] = amt
                    item["balance"] = _parse_amount(raw.get("balance"))
                    item["type"] = _infer_type(amt, raw.get("type"))
                    item["currency"] = raw.get("currency")
                    if item.get("date") or item.get("amount") is not None:
                        rows.append(item)

            # Fallback: line-oriented text parse
            if not rows:
                text = page.extract_text() or ""
                for line in text.splitlines():
                    line = line.strip()
                    m = _PDF_LINE_RE.match(line)
                    if not m:
                        continue
                    date_s, desc, amt_s, bal_s = m.group(1), m.group(2), m.group(3), m.group(4)
                    amt = _parse_amount(amt_s)
                    rows.append(
                        {
                            "date": _parse_date(date_s),
                            "description": desc.strip(),
                            "amount": amt,
                            "type": _infer_type(amt),
                            "balance": _parse_amount(bal_s) if bal_s else None,
                            "source_file": path.name,
                            "currency": None,
                        }
                    )
    return rows


def parse_file(path: Path) -> list[dict]:
    """Dispatch by extension."""
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return parse_csv(path)
    if suffix in {".ofx", ".qfx"}:
        return parse_ofx(path)
    if suffix == ".pdf":
        return parse_pdf(path)
    raise ValueError(f"Unsupported file type: {suffix} ({path.name})")
