"""שרת כלי המיון — נקודת הכניסה ש-Make פונה אליה.

למה זה קיים:
    הממשק ב-app.py הוא מסך שאדם עומד מולו. Make אינו יכול לפנות אליו.
    כאן אותו קוד בדיוק נחשף כנקודת כניסה: מקבלים חבילת קבצים ורשימת
    סאב-אייטמים פתוחים, ומחזירים מסמכים מקובצים, בעלי שם, ומשויכים.

מה חולק עם המסך:
    reader.py לקריאת הקבצים ו-engine.py לקיבוץ, לשיוך ולמתן-שמות. שני
    הצדדים משתמשים באותו קוד, ולכן מה שנבדק במסך הוא מה שרץ באוטומציה.

נקודות כניסה:
    GET  /            בדיקת חיים, מחזירה את גרסאות שלושת הקבצים
    POST /sort        החבילה המלאה. זו הנקודה ש-Make משתמש בה
"""

API_BUILD = "api-2026-09-17-v38"

import base64
import os
from typing import List, Optional

from anthropic import Anthropic
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

import engine
import reader

app = FastAPI(title="כלי מיון מסמכים", version=API_BUILD)


# ------------------------------------------------------------------ מבנה הבקשה

class InFile(BaseModel):
    name: str
    content_b64: str


class SortRequest(BaseModel):
    files: List[InFile]
    # שמות הסאב-אייטמים הפתוחים של התיק, כפי שהם במונדיי. בלעדיהם הכלי
    # מסווג ונותן שמות אך אינו משייך.
    case_docs: List[str] = Field(default_factory=list)
    # מזהה התיק, מוחזר כמות שהוא כדי ש-Make יידע לאן להעלות
    monday_case_id: Optional[str] = None
    only_edges: bool = True


class OutDoc(BaseModel):
    name: str
    category: str
    target: Optional[str]
    target_conf: float
    confidence: float
    bucket: str                 # matched / extra / review
    gaps: List[str] = Field(default_factory=list)
    source_files: List[str]
    pdf_b64: str


class SortResponse(BaseModel):
    build: dict
    monday_case_id: Optional[str]
    documents: List[OutDoc]
    still_missing: List[str]
    usage: dict


# ------------------------------------------------------------------ עזר

def _clean_docs(raw: List[str]) -> List[str]:
    """מנקה סימני רשימה שנגררים בהעתקה ממונדיי."""
    out = []
    for ln in raw or []:
        ln = (ln or "").strip().lstrip("*-•·–— \t").strip()
        if ln:
            out.append(ln)
    return out


def _bucket(conf: float, target, target_conf: float,
            conf_threshold: float, match_threshold: float) -> str:
    """לאיזה סל המסמך שייך. אותו סדר הכרעה כמו במסך.

    ביטחון זיהוי נמוך גובר על הכל: אם לא יודעים מה המסמך, אין טעם לשייך.
    """
    if conf < conf_threshold:
        return "review"
    if target and target_conf >= match_threshold:
        return "matched"
    return "extra"


# ------------------------------------------------------------------ נקודות כניסה

@app.get("/")
def health():
    return {"status": "ok", "api": API_BUILD,
            "engine": engine.ENGINE_BUILD, "reader": reader.READER_BUILD}


@app.post("/sort", response_model=SortResponse)
def sort(req: SortRequest):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise HTTPException(500, "חסר ANTHROPIC_API_KEY בהגדרות השרת")
    if not req.files:
        raise HTTPException(400, "לא התקבלו קבצים")

    client = Anthropic(api_key=key)
    case_docs = _clean_docs(req.case_docs)

    # שלב 1 — קריאה. קובץ אחד בכל פעם, בדגם המדויק.
    per_file = []
    for f in req.files:
        try:
            data = base64.b64decode(f.content_b64)
        except Exception:
            raise HTTPException(400, f"קובץ פגום: {f.name}")
        a = reader.read_file(client, f.name, data, case_docs, req.only_edges)
        per_file.append({"filename": f.name, "bytes": data, "a": a,
                         "file_pages": reader.file_page_count(f.name, data)})

    # שלב 2 — קיבוץ, שיוך ומתן-שמות. בקוד, בלי מודל.
    groups = engine.build_groups(per_file)
    engine.assign(groups, case_docs)

    docs, covered = [], set()
    for g in groups:
        conf = engine.group_confidence(g)
        name = engine.build_name(g)
        bucket = _bucket(conf, g["target"], g["target_conf"], 0.7, 0.6)
        if bucket == "matched":
            covered.add(g["target"])

        members = g["members"]
        pdf = reader.merge_to_pdf([per_file[m["index"]]["bytes"] for m in members],
                                  [m["filename"] for m in members])
        docs.append(OutDoc(
            name=reader.safe_filename(name) + ".pdf",
            category=g["cat"],
            target=g["target"] if bucket == "matched" else None,
            target_conf=g["target_conf"],
            confidence=round(conf, 2),
            bucket=bucket,
            gaps=engine.find_gaps(g),
            source_files=[m["filename"] for m in members],
            pdf_b64=base64.b64encode(pdf).decode("utf-8"),
        ))

    return SortResponse(
        build={"api": API_BUILD, "engine": engine.ENGINE_BUILD,
               "reader": reader.READER_BUILD},
        monday_case_id=req.monday_case_id,
        documents=docs,
        still_missing=[d for d in case_docs if d not in covered],
        usage=reader.usage_snapshot(),
    )