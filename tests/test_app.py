import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from statement_to_sheet.web import app

SAMPLE = Path(__file__).resolve().parent.parent / "sample"
client = TestClient(app)


def test_health():
    assert client.get("/health").json() == {"status": "ok"}


def test_index():
    assert "Statement" in client.get("/").text


def test_convert_csv_redacts_and_cleans_up():
    before = set(Path(tempfile.gettempdir()).glob("sts-*"))
    with open(SAMPLE / "fake_statement.csv", "rb") as f:
        r = client.post("/convert", files={"files": ("s.csv", f, "text/csv")}, data={"fmt": "csv"})
    assert r.status_code == 200
    assert "123456789012" not in r.text
    assert "9988776655" not in r.text
    assert "GB29NWBK60161331926819" not in r.text
    assert "GROCERY MART" in r.text
    assert set(Path(tempfile.gettempdir()).glob("sts-*")) == before


def test_convert_ofx_xlsx_and_zip():
    for fmt in ("xlsx", "zip"):
        with open(SAMPLE / "fake_statement.ofx", "rb") as f:
            r = client.post("/convert", files={"files": ("s.ofx", f)}, data={"fmt": fmt})
        assert r.status_code == 200 and r.content[:2] == b"PK"


def test_rejects_bad_type():
    r = client.post("/convert", files={"files": ("x.exe", b"hi")}, data={"fmt": "csv"})
    assert r.status_code == 400
