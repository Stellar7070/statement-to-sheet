"""Minimal self-hosted web UI: upload -> convert -> download. No external calls.

Uploaded files and outputs live in a per-request temp dir that is deleted
before the response finishes streaming.
"""
from __future__ import annotations

import io
import os
import shutil
import tempfile
import zipfile
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, Response

from .extract import process

ALLOWED = {".csv", ".ofx", ".qfx", ".pdf"}
MAX_BYTES = int(os.environ.get("MAX_UPLOAD_MB", "20")) * 1024 * 1024

app = FastAPI(title="Statement to Sheet", docs_url=None, redoc_url=None)

PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Statement to Sheet</title>
<style>body{font-family:system-ui,sans-serif;max-width:640px;margin:3rem auto;padding:0 1rem;color:#1a1a1a}
h1{margin-bottom:.2rem}.sub{color:#555;margin-top:0}form{border:1px solid #ddd;border-radius:10px;padding:1.5rem;margin-top:1.5rem}
label{display:block;margin:.8rem 0}button{background:#0b5fff;color:#fff;border:0;border-radius:6px;padding:.7rem 1.4rem;font-size:1rem;cursor:pointer}
.note{font-size:.9rem;color:#555;margin-top:1.5rem}</style></head><body>
<h1>Statement &rarr; Sheet</h1>
<p class="sub">Private bank statement converter. Account numbers are masked before anything is written.</p>
<form action="/convert" method="post" enctype="multipart/form-data">
<label>Statements (CSV, OFX, QFX, PDF)<br><input type="file" name="files" multiple required accept=".csv,.ofx,.qfx,.pdf"></label>
<label>Output<br><select name="fmt"><option value="xlsx">Excel (.xlsx)</option><option value="csv">CSV</option><option value="zip">ZIP (xlsx + csv + redaction report)</option></select></label>
<label><input type="checkbox" name="keep_account_ref" value="1"> Keep last-4 account reference (****1234)</label>
<button type="submit">Convert</button></form>
<p class="note">Runs entirely on this server. No third-party APIs, no AI services, no tracking.
Uploaded files are deleted as soon as the conversion finishes. PDF support is best-effort; CSV/OFX exports from your bank are most reliable.</p>
</body></html>"""


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return PAGE


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/convert")
async def convert(
    files: list[UploadFile] = File(...),
    fmt: str = Form("xlsx"),
    keep_account_ref: str | None = Form(None),
) -> Response:
    if fmt not in {"xlsx", "csv", "zip"}:
        raise HTTPException(400, "Unsupported output format")
    work = Path(tempfile.mkdtemp(prefix="sts-"))
    try:
        inp = work / "in"
        inp.mkdir()
        saved: list[Path] = []
        for i, f in enumerate(files):
            ext = Path(f.filename or "").suffix.lower()
            if ext not in ALLOWED:
                raise HTTPException(400, f"Unsupported file type: {ext or 'none'}")
            data = await f.read(MAX_BYTES + 1)
            if len(data) > MAX_BYTES:
                raise HTTPException(413, "File too large")
            p = inp / f"upload_{i}{ext}"
            p.write_bytes(data)
            saved.append(p)
        try:
            df, _stats, paths = process(saved, work / "out", keep_account_ref=bool(keep_account_ref))
        except SystemExit:
            raise HTTPException(422, "No transactions could be extracted from the upload.")
        if fmt == "zip":
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                for p in paths.values():
                    z.write(p, p.name)
            body, media, name = buf.getvalue(), "application/zip", "transactions.zip"
        elif fmt == "csv":
            body, media, name = paths["csv"].read_bytes(), "text/csv", "transactions.csv"
        else:
            body = paths["xlsx"].read_bytes()
            media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            name = "transactions.xlsx"
        return Response(body, media_type=media,
                        headers={"Content-Disposition": f'attachment; filename="{name}"',
                                 "Cache-Control": "no-store"})
    finally:
        shutil.rmtree(work, ignore_errors=True)


def run() -> None:
    import uvicorn
    uvicorn.run(app, host=os.environ.get("HOST", "0.0.0.0"), port=int(os.environ.get("PORT", "8000")))


if __name__ == "__main__":
    run()
