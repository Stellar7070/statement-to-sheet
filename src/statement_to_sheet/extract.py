"""CLI: bank statement → redacted spreadsheet (CSV + XLSX).

Privacy-first: redacts account numbers before any enrichment / before writing.
Never calls external APIs or uploads files.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Iterable, Optional

import pandas as pd

from .parsers import STANDARD_COLUMNS, parse_file
from .redact import RedactionStats, redact_dataframe_columns

SUPPORTED_SUFFIXES = {".csv", ".ofx", ".qfx", ".pdf"}


def collect_inputs(path: Path) -> list[Path]:
    if path.is_file():
        if path.suffix.lower() not in SUPPORTED_SUFFIXES:
            raise SystemExit(
                f"Unsupported file type '{path.suffix}'. Use CSV, OFX/QFX, or PDF."
            )
        return [path]
    if path.is_dir():
        files = sorted(
            p
            for p in path.iterdir()
            if p.is_file() and p.suffix.lower() in SUPPORTED_SUFFIXES
        )
        if not files:
            raise SystemExit(f"No CSV/OFX/QFX/PDF files found in {path}")
        return files
    raise SystemExit(f"Path not found: {path}")


def rows_to_dataframe(
    rows: list[dict],
    *,
    keep_account_ref: bool,
) -> tuple[pd.DataFrame, RedactionStats]:
    cleaned, stats = redact_dataframe_columns(
        rows,
        text_keys=("description", "memo", "payee", "name"),
        keep_account_ref=keep_account_ref,
    )
    if not cleaned:
        cols = list(STANDARD_COLUMNS)
        if keep_account_ref:
            cols.append("account_ref")
        return pd.DataFrame(columns=cols), stats

    df = pd.DataFrame(cleaned)
    # Ensure standard columns exist
    for col in STANDARD_COLUMNS:
        if col not in df.columns:
            df[col] = None
    ordered = list(STANDARD_COLUMNS)
    if keep_account_ref:
        if "account_ref" not in df.columns:
            df["account_ref"] = None
        ordered.append("account_ref")
    # Drop any leftover sensitive columns that slipped through
    drop = [
        c
        for c in df.columns
        if c not in ordered
        and any(
            x in c.lower()
            for x in ("account", "iban", "routing", "acct")
        )
        and c != "account_ref"
    ]
    df = df.drop(columns=drop, errors="ignore")
    df = df[ordered]

    # Sort by date when parseable
    if "date" in df.columns:
        try:
            df = df.assign(_sort=pd.to_datetime(df["date"], errors="coerce")).sort_values(
                "_sort", kind="mergesort"
            ).drop(columns=["_sort"])
        except Exception:
            pass
    df = df.reset_index(drop=True)
    return df, stats


def write_outputs(
    df: pd.DataFrame,
    stats: RedactionStats,
    out_dir: Path,
    stem: str = "transactions",
) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / f"{stem}.csv"
    xlsx_path = out_dir / f"{stem}.xlsx"
    report_path = out_dir / "REDACTION_REPORT.txt"

    df.to_csv(csv_path, index=False)
    df.to_excel(xlsx_path, index=False, engine="openpyxl")
    report_path.write_text("\n".join(stats.as_report_lines()) + "\n", encoding="utf-8")
    return {"csv": csv_path, "xlsx": xlsx_path, "report": report_path}


def process(
    inputs: Iterable[Path],
    out_dir: Path,
    *,
    keep_account_ref: bool = False,
) -> tuple[pd.DataFrame, RedactionStats, dict[str, Path]]:
    all_rows: list[dict] = []
    errors: list[str] = []
    for path in inputs:
        try:
            rows = parse_file(path)
            all_rows.extend(rows)
            print(f"Parsed {path.name}: {len(rows)} transaction(s)", file=sys.stderr)
        except Exception as exc:
            # Do not dump file contents
            msg = f"Failed to parse {path.name}: {type(exc).__name__}: {exc}"
            errors.append(msg)
            print(msg, file=sys.stderr)

    if not all_rows and errors:
        raise SystemExit("No transactions extracted.")

    df, stats = rows_to_dataframe(all_rows, keep_account_ref=keep_account_ref)
    paths = write_outputs(df, stats, out_dir)
    print(f"Wrote {len(df)} row(s) → {paths['csv']}", file=sys.stderr)
    print(f"Wrote {len(df)} row(s) → {paths['xlsx']}", file=sys.stderr)
    print(f"Redaction report → {paths['report']}", file=sys.stderr)
    return df, stats, paths


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="statement_to_sheet",
        description=(
            "Convert bank statements (CSV, OFX/QFX, PDF) to a clean spreadsheet. "
            "Account numbers are redacted before writing. Fully local — no uploads."
        ),
    )
    p.add_argument(
        "path",
        type=Path,
        help="Statement file or folder of statements (CSV / OFX / QFX / PDF)",
    )
    p.add_argument(
        "--out",
        type=Path,
        default=Path("out"),
        help="Output directory (default: ./out)",
    )
    p.add_argument(
        "--keep-account-ref",
        action="store_true",
        help="Keep optional account_ref column as ****1234 (last 4 only). Default: omit.",
    )
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    inputs = collect_inputs(args.path)
    process(inputs, args.out, keep_account_ref=args.keep_account_ref)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
