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

API_BUILD = "api-2026-09-23-v46"

import base64
import hashlib
import threading
import time
import os
from typing import List, Optional, Union

from anthropic import Anthropic
from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel, Field

import engine
import reader

app = FastAPI(title="כלי מיון מסמכים", version=API_BUILD)


# ------------------------------------------------------------------ מבנה הבקשה

class InFile(BaseModel):
    """קובץ נכנס: או התוכן עצמו, או כתובת שהשרת יוריד ממנה.

    כתובת עדיפה: Make לא צריך להוריד, להמיר ולאסוף כל קובץ - שתי פעולות
    לכל קובץ שנחסכות. השרת מוריד במקביל, בחינם.
    """
    name: str
    content_b64: Optional[str] = None
    url: Optional[str] = None
    # מזהה הקובץ במונדיי. ייחודי וקבוע, ומשמש כמפתח לזיכרון הקריאות.
    file_id: Optional[Union[str, int]] = None


class InAsset(BaseModel):
    """קובץ כפי שהוא יוצא משאילתת ה-GraphQL במונדיי."""
    id: Optional[Union[str, int]] = None
    name: str
    public_url: str
    created_at: Optional[str] = None


class InItem(BaseModel):
    """התיק כולו, כפי שהשאילתה במודול 3 מחזירה אותו.

    Make שולח את זה כמו שזה, בהמרה אחת. השרת שולף ממנו גם את הקבצים וגם
    את הסאב-אייטמים - ומייתר שישה מודולים ב-Make.
    """
    id: Optional[Union[str, int]] = None
    name: Optional[str] = None
    assets: List[InAsset] = Field(default_factory=list)
    subitems: List["InSubitem"] = Field(default_factory=list)


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


InItem.model_rebuild()


class SortRequest(BaseModel):
    # הדרך החסכונית: התיק כולו מהשאילתה. גובר על files ו-subitems.
    item: Optional[InItem] = None
    files: List[InFile] = Field(default_factory=list)
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
REQUIRED_STATUS = "נדרש מהלקוח"
ALLOWED_STATUSES = {REQUIRED_STATUS, "בבדיקה"}
EXTRAS_NAME = "מסמכים נוספים"


def plan_subitems(subitems: List[InSubitem]) -> dict:
    """מפרק את רשימת הסאב-אייטמים לשלושה דברים שהשרת צריך.

    targets    - שמות הסאב-אייטמים שמותר לשייך אליהם, לפי הסטטוס
    name_to_ids- מיפוי שם -> מזהים. רשימה ולא ערך, כי בתיק ישן עלולים להיות
                 שני סאב-אייטמים באותו שם; במקרה כזה לא מנחשים
    extras_id  - המזהה של "מסמכים נוספים", לאן הולך כל מה שלא שויך
    """
    targets, required, name_to_ids, extras_id = [], [], {}, None
    for s in subitems:
        name = (s.name or "").strip()
        sid = str(s.id)
        if name == EXTRAS_NAME:
            extras_id = extras_id or sid
            continue
        status = s.status_text()
        if status not in ALLOWED_STATUSES:
            continue
        name_to_ids.setdefault(name, []).append(sid)
        if name not in targets:
            targets.append(name)
        # "בבדיקה" פתוח לשיוך, אבל המסמך שלו כבר הגיע - הוא לא חסר.
        # רק מה שבאמת ממתין ללקוח נכנס לרשימת החסרים, כי בהמשך הרשימה הזו
        # תשמש להודעה ללקוח על מה שעוד צריך לשלוח.
        if status == REQUIRED_STATUS and name not in required:
            required.append(name)
    return {"targets": targets, "required": required,
            "name_to_ids": name_to_ids, "extras_id": extras_id}


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


# כמה קבצים נקראים בו-זמנית. מספיק כדי ש-40 תמונות יסתיימו בפחות מדקה,
# ומתחת למגבלת הקצב של שרת הבינה המלאכותית.
PARALLEL_READS = 6

# שגיאות שאין טעם לנסות שוב אחריהן. מפתח שגוי יישאר שגוי בכל ניסיון.
_FATAL_ERRORS = ("AuthenticationError", "PermissionDeniedError", "NotFoundError")


def _error_text(e: Exception) -> str:
    name = type(e).__name__
    if name == "AuthenticationError":
        return "מפתח ה-API של Anthropic שגוי או חסר בהגדרות השרת"
    if name == "PermissionDeniedError":
        return "למפתח ה-API אין הרשאה לדגם"
    if name == "NotFoundError":
        return "הדגם לא נמצא - ייתכן ששמו השתנה"
    if name == "RateLimitError":
        return "חריגה ממגבלת הקצב של Anthropic"
    if "Overloaded" in name or "529" in str(e):
        return "שרת הבינה המלאכותית עמוס"
    return f"{name}: {str(e)[:200]}"


# מאילו אתרים מותר להוריד. הכתובות מגיעות מבחוץ, ובלי רשימה סגורה כל אחד
# היה יכול לגרום לשרת להוריד מכל מקום.
ALLOWED_HOSTS = ("amazonaws.com", "monday.com")
MAX_FILE_BYTES = 25 * 1024 * 1024


def download(url: str) -> bytes:
    """מוריד קובץ מכתובת חתומה של מונדיי. זמינה שעה מרגע השאילתה."""
    from urllib.parse import urlparse
    from urllib.request import urlopen, Request

    p = urlparse(url or "")
    if p.scheme != "https" or not any(p.hostname and p.hostname.endswith(h)
                                      for h in ALLOWED_HOSTS):
        raise ValueError("כתובת מאתר שאינו מורשה")
    with urlopen(Request(url, headers={"User-Agent": "document-sorter"}),
                 timeout=30) as r:
        data = r.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise ValueError("הקובץ גדול מ-25 מגה")
    return data


# ---------------------------------------------------------------- זיכרון קריאות
#
# הקריאה של קובץ בבינה המלאכותית היא הדבר היחיד שעולה כסף אמיתי. כל לולאה,
# מאיזו סיבה שתהיה, נראית אותו דבר: אותם קבצים נשלחים שוב. לכן במקום לנסות
# לחזות את כל הסיבות ללולאה, הזיכרון מנטרל את המחיר שלה - כל קובץ נקרא פעם
# אחת, והקריאות הבאות נשלפות.
#
# שני עקרונות:
#   לעולם לא חוסם - הזיכרון מחזיר תוצאה, אף פעם לא סירוב. אין מצב שבו הרצה
#                    חוזרת נתקעת בגללו.
#   מפתח יציב     - מזהה הקובץ במונדיי. קובץ שהלקוח שולח שוב מקבל מזהה חדש
#                    ולכן ייקרא מחדש, כרצוי.

READ_CACHE_TTL = 6 * 3600        # שש שעות
READ_CACHE_MAX = 5000            # רק השדות שחולצו נשמרים, לא הקובץ עצמו
_READ_CACHE = {}
_CACHE_LOCK = threading.Lock()
_CACHE_STATS = {"hits": 0, "misses": 0}


def _cache_key(name: str, file_id, data: bytes) -> str:
    """מזהה מונדיי כשיש; אחרת טביעת אצבע של התוכן."""
    if file_id:
        return f"id:{file_id}"
    return "sha:" + hashlib.sha256(data).hexdigest()[:32] if data else ""


def cache_get(key: str):
    if not key:
        return None
    now = time.time()
    with _CACHE_LOCK:
        hit = _READ_CACHE.get(key)
        if hit and now - hit[0] <= READ_CACHE_TTL:
            _CACHE_STATS["hits"] += 1
            return dict(hit[1])
        if hit:
            _READ_CACHE.pop(key, None)
        _CACHE_STATS["misses"] += 1
    return None


def cache_put(key: str, fields: dict) -> None:
    if not key or not fields:
        return
    with _CACHE_LOCK:
        if len(_READ_CACHE) >= READ_CACHE_MAX:
            # מפנים את הרבע הישן ביותר, כדי לא לפנות אחד-אחד בכל כתיבה
            for k in sorted(_READ_CACHE, key=lambda k: _READ_CACHE[k][0])[:READ_CACHE_MAX // 4]:
                _READ_CACHE.pop(k, None)
        _READ_CACHE[key] = (time.time(), dict(fields))


def read_all(client, files: list, case_docs: list, only_edges: bool):
    """קורא את כל הקבצים במקביל. מחזיר (תוצאות, שגיאה_קבועה).

    שלושה עקרונות:
      סדר - התוצאות חוזרות בסדר שבו הקבצים נשלחו, כי הקיבוץ תלוי בו
      בידוד - קובץ שנכשל לא מפיל את השאר; הוא מסומן ונשלח לבדיקה ידנית
      עצירה - שגיאה קבועה (מפתח שגוי) עוצרת הכל מיד, בלי לבזבז קריאות
    """
    from concurrent.futures import ThreadPoolExecutor

    def one(item):
        name, data, url, file_id = item
        key = _cache_key(name, file_id, data or b"")
        cached = cache_get(key)
        if cached is not None and data is None and url:
            # התוכן עדיין נדרש למיזוג ה-PDF, אבל ההורדה אינה עולה כסף.
            try:
                data = download(url)
            except Exception as e:
                return {"name": name, "data": b"", "error": f"הורדה נכשלה: {e}",
                        "fatal": False, "a": _failed_fields(name, "הורדה נכשלה")}
        if cached is not None and data:
            return {"name": name, "data": data, "a": cached,
                    "error": None, "fatal": False, "cached": True}
        if data is None and url:
            try:
                data = download(url)
            except Exception as e:
                return {"name": name, "data": b"", "error": f"הורדה נכשלה: {e}",
                        "fatal": False, "a": _failed_fields(name, "הורדה נכשלה")}
        if not data:
            return {"name": name, "data": data, "error": "הקובץ פגום ולא ניתן לפענוח",
                    "fatal": False, "a": _failed_fields(name, "הקובץ פגום")}
        try:
            a = reader.read_file(client, name, data, case_docs, only_edges)
            cache_put(key, a)
            return {"name": name, "data": data, "a": a, "error": None, "fatal": False}
        except Exception as e:
            fatal = type(e).__name__ in _FATAL_ERRORS
            return {"name": name, "data": data, "error": _error_text(e),
                    "fatal": fatal, "a": _failed_fields(name, _error_text(e))}

    with ThreadPoolExecutor(max_workers=PARALLEL_READS) as pool:
        results = list(pool.map(one, files))     # map שומר על הסדר

    fatal = next((r["error"] for r in results if r["fatal"]), None)
    return results, fatal


def _failed_fields(name: str, reason: str) -> dict:
    """שדות לקובץ שלא נקרא. ביטחון אפס מבטיח שילך לבדיקה ידנית."""
    return {"doc_type": "קובץ שלא נקרא", "summary": reason, "confidence": 0.0,
            "person_name": None, "source": None}


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
def sort(req: SortRequest, x_sorter_token: Optional[str] = Header(default=None)):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise HTTPException(500, "חסר ANTHROPIC_API_KEY בהגדרות השרת")

    # אסימון גישה. הכתובת של השרת פתוחה לכל העולם, וכל קריאה עולה כסף
    # בבינה המלאכותית. אם מוגדר SORTER_TOKEN בהגדרות השרת - רק מי שמכיר
    # אותו יכול לקרוא. אם לא מוגדר - פתוח, כמו היום.
    token = os.environ.get("SORTER_TOKEN")
    if token and x_sorter_token != token:
        raise HTTPException(401, "אסימון גישה שגוי או חסר")

    if req.item:
        files = [InFile(name=a.name, url=a.public_url, file_id=a.id)
                 for a in req.item.assets]
        subitems = req.item.subitems
    else:
        files, subitems = req.files, req.subitems
    if not files:
        raise HTTPException(400, "לא התקבלו קבצים")

    client = Anthropic(api_key=key)
    warnings = []
    if subitems:
        plan = plan_subitems(subitems)
        case_docs = plan["targets"]
        if not plan["extras_id"]:
            warnings.append(f'בתיק אין סאב-אייטם "{EXTRAS_NAME}". מסמכים שלא '
                            'שויכו לא יקבלו יעד להעלאה.')
    else:
        # תאימות לאחור: רשימת שמות בלבד, בלי מזהים
        case_docs = _clean_docs(req.case_docs)
        plan = {"targets": case_docs, "required": case_docs,
                "name_to_ids": {}, "extras_id": None}

    # שלב 1 — קריאה. במקביל, בדגם המדויק.
    decoded = []
    for f in files:
        if f.content_b64:
            try:
                decoded.append((f.name, base64.b64decode(f.content_b64), None, f.file_id))
            except Exception:
                # קובץ שלא ניתן לפענח לא עוצר את החבילה - הוא הולך לבדיקה ידנית
                decoded.append((f.name, b"", None, f.file_id))
        else:
            # התוכן יורד בתוך הקריאה המקבילית, יחד עם הקריאה עצמה
            decoded.append((f.name, None, f.url, f.file_id))

    results, fatal = read_all(client, decoded, case_docs, req.only_edges)
    if fatal:
        # שגיאה קבועה - מפתח שגוי, הרשאה חסרה. אין טעם לנסות שוב, וכל
        # ניסיון נוסף רק עולה כסף. קוד 422 אומר ל-Make לא לנסות שוב אוטומטית.
        raise HTTPException(422, fatal)

    failed = [r for r in results if r["error"]]
    if decoded and len(failed) == len(decoded):
        # כל הקבצים נכשלו בשגיאה זמנית - עומס, זמן המתנה. כאן כן כדאי לנסות
        # שוב, ולכן 503: Make ינסה שוב בעוד כמה דקות.
        raise HTTPException(503, "כל הקבצים נכשלו בקריאה, כנראה עומס זמני: "
                                 + failed[0]["error"])

    # קובץ שהתוכן שלו לא התקבל בכלל לא נכנס לחבילה - אין מה למזג או להעלות.
    # קובץ שהתוכן שלו תקין אבל הקריאה נכשלה - נכנס, ויישלח לבדיקה ידנית
    # עם התוכן המקורי, כך שהנציג יראה אותו.
    per_file = [{"filename": r["name"], "bytes": r["data"], "a": r["a"],
                 "file_pages": reader.file_page_count(r["name"], r["data"])}
                for r in results if r["data"]]
    for r in failed:
        if r["data"]:
            warnings.append(f'הקובץ {r["name"]} לא נקרא ונשלח לבדיקה ידנית: {r["error"]}')
        else:
            warnings.append(f'הקובץ {r["name"]} הגיע פגום ולא הועלה. הוא נשאר '
                            'בשיחת הוואטסאפ של הלקוח.')

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
        still_missing=[d for d in plan["required"] if d not in covered],
        extras_subitem_id=plan["extras_id"],
        warnings=warnings,
        usage=reader.usage_snapshot(),
    )