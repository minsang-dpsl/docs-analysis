from pathlib import Path

from app.data import read_sheet_rows

FIX = Path(__file__).parent / "fixtures"


def load_doc(name: str) -> dict:
    """샘플 결과 xlsx(kind, code, name, score, evidence_page) -> 문서 집계 dict."""
    rows = read_sheet_rows(FIX / f"{name}__result.xlsx")
    doc = {"naics": [], "countries": []}
    for r in rows[1:]:
        if len(r) < 5 or not r[0]:
            continue
        item = {"code": r[1], "name": r[2], "score": float(r[3]), "evidence_page": int(r[4])}
        doc["naics" if r[0] == "naics" else "countries"].append(item)
    return doc
