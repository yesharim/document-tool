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

API_BUILD = "api-2026-09-22-v43"

import base64
import os
from typing import List, Optional, Union

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


class InColumnValue(BaseModel):
    text: Optional[str] = None


class InSubitem(BaseModel):
    """סאב-אייטם כפי שהוא יוצא משאילתת ה-GraphQL ב-Make, בלי עיבוד.

    Make שולח את המערך כמו שהוא, והשרת מפרק אותו. כך אין צורך בנוסחאות
    סינון ב-Make - המקום שבו דברים נשברים בשקט.
    """
    id: Union[str, int]
    name: str
    status: Optional[str] = None
    column_values: List[InColumnValue] = Field(default_factory=list)

    def status_text(self) -> str:
        if self.status:
            return self.status.strip()
        for cv in self.column_values:
            if cv.text:
                return cv.text.strip()
        return ""


class SortRequest(BaseModel):
    files: List[InFile]
    # הדרך המועדפת: כל הסאב-אייטמים של התיק, עם מזהה וסטטוס. השרת מסנן
    # לבד מה מותר לשייך אליו, ומחזיר לכל מסמך את המזהה שאליו להעלות.
    subitems: List[InSubitem] = Field(default_factory=list)
    # הדרך הישנה, נשמרת לתאימות: שמות בלבד, בלי מזהים. בלעדיהם הכלי
    # מסווג ונותן שמות אך אינו יודע לאן להעלות.
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
    # לאן Make מעלה את הקובץ. תמיד מלא כשיש סאב-אייטם "מסמכים נוספים":
    # מסמך משויך - לסאב-אייטם שלו; כל השאר - ל"מסמכים נוספים".
    destination_subitem_id: Optional[str] = None
    destination_name: Optional[str] = None
    gaps: List[str] = Field(default_factory=list)
    source_files: List[str]
    pdf_b64: str


class SortResponse(BaseModel):
    build: dict
    monday_case_id: Optional[str]
    documents: List[OutDoc]
    still_missing: List[str]
    extras_subitem_id: Optional[str] = None
    warnings: List[str] = Field(default_factory=list)
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


# הסטטוסים שמותר להעלות אליהם מסמך חדש. "תקין" חסום: הנציג כבר אישר,
# ומסמך נוסף שם היה יוצר בלבול במה שכבר נסגר. "בבדיקה" פתוח: לקוח שולח
# בטפטופים, ותלוש שני צריך להגיע לאותו סאב-אייטם שקיבל את הראשון.
ALLOWED_STATUSES = {"נדרש מהלקוח", "בבדיקה"}
EXTRAS_NAME = "מסמכים נוספים"


def plan_subitems(subitems: List[InSubitem]) -> dict:
    """מפרק את רשימת הסאב-אייטמים לשלושה דברים שהשרת צריך.

    targets    - שמות הסאב-אייטמים שמותר לשייך אליהם, לפי הסטטוס
    name_to_ids- מיפוי שם -> מזהים. רשימה ולא ערך, כי בתיק ישן עלולים להיות
                 שני סאב-אייטמים באותו שם; במקרה כזה לא מנחשים
    extras_id  - המזהה של "מסמכים נוספים", לאן הולך כל מה שלא שויך
    """
    targets, name_to_ids, extras_id = [], {}, None
    for s in subitems:
        name = (s.name or "").strip()
        sid = str(s.id)
        if name == EXTRAS_NAME:
            extras_id = extras_id or sid
            continue
        if s.status_text() not in ALLOWED_STATUSES:
            continue
        name_to_ids.setdefault(name, []).append(sid)
        if name not in targets:
            targets.append(name)
    return {"targets": targets, "name_to_ids": name_to_ids, "extras_id": extras_id}


def destination(target, bucket: str, plan: dict):
    """לאן להעלות מסמך. מחזיר (מזהה, שם).

    רק מסמך משויך הולך לסאב-אייטם שלו. כל השאר - גם "לבדיקה" וגם "מסמכים
    נוספים" - הולכים ל"מסמכים נוספים", והנציג מכריע. ואם לשם יש שני
    סאב-אייטמים זהים, אי אפשר לדעת לאיזה מהם התכוונו - גם זה ל"נוספים".
    """
    if bucket == "matched" and target:
        ids = plan["name_to_ids"].get(target, [])
        if len(ids) == 1:
            return ids[0], target
    return plan["extras_id"], EXTRAS_NAME


def _bucket(conf: float, target, target_conf: float,
            conf_threshold: float, match_threshold: float,
            gaps: list = None) -> str:
    """לאיזה סל המסמך שייך. אותו סדר הכרעה כמו במסך.

    חוסר ידוע גובר על הכל: מסמך שחסרים בו עמודים לא יעלה אוטומטית, גם אם
    זוהה ושויך בוודאות. אחרת Make יעלה דוח חלקי לסאב-אייטם וישנה סטטוס
    ל"בבדיקה" - והסאב-אייטם ייצא מרשימת התזכורות בזמן שהמסמך עדיין חסר.
    אף אחד לא יידע.

    ואחריו ביטחון זיהוי נמוך: אם לא יודעים מה המסמך, אין טעם לשייך.
    """
    if gaps:
        return "review"
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
    warnings = []
    if req.subitems:
        plan = plan_subitems(req.subitems)
        case_docs = plan["targets"]
        if not plan["extras_id"]:
            warnings.append(f'בתיק אין סאב-אייטם "{EXTRAS_NAME}". מסמכים שלא '
                            'שויכו לא יקבלו יעד להעלאה.')
    else:
        # תאימות לאחור: רשימת שמות בלבד, בלי מזהים
        case_docs = _clean_docs(req.case_docs)
        plan = {"targets": case_docs, "name_to_ids": {}, "extras_id": None}

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
        gaps = engine.find_gaps(g)
        bucket = _bucket(conf, g["target"], g["target_conf"], 0.7, 0.6, gaps)
        if bucket == "matched":
            covered.add(g["target"])

        members = g["members"]
        pdf = reader.merge_to_pdf([per_file[m["index"]]["bytes"] for m in members],
                                  [m["filename"] for m in members])
        dest_id, dest_name = destination(g["target"], bucket, plan)
        docs.append(OutDoc(
            name=reader.safe_filename(name) + ".pdf",
            category=g["cat"],
            destination_subitem_id=dest_id,
            destination_name=dest_name,
            target=g["target"] if bucket == "matched" else None,
            target_conf=g["target_conf"],
            confidence=round(conf, 2),
            bucket=bucket,
            gaps=gaps,
            source_files=[m["filename"] for m in members],
            pdf_b64=base64.b64encode(pdf).decode("utf-8"),
        ))

    return SortResponse(
        build={"api": API_BUILD, "engine": engine.ENGINE_BUILD,
               "reader": reader.READER_BUILD},
        monday_case_id=req.monday_case_id,
        documents=docs,
        still_missing=[d for d in case_docs if d not in covered],
        extras_subitem_id=plan["extras_id"],
        warnings=warnings,
        usage=reader.usage_snapshot(),
    )