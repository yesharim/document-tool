# -*- coding: utf-8 -*-
"""
כלי סיווג, קיבוץ, שיוך ומתן-שמות למסמכים.
העובדת מעלה את כל הקבצים (PDF / תמונות), הכלי:
  1) קורא כל קובץ עם הדגם הזול; אם לא בטוח - משדרג לדגם המדויק (מעבר 1)
  2) מקבץ מסמכים שהם אותו דבר לוגי, משייך לסאב-אייטם ונותן שם (מעבר 2)
  3) ממזג כל קבוצה ל-PDF אחד עם השם הסופי
  4) מסמן בנפרד מה שלא בטוח ("לבדיקה") ומה שלא שויך ("מסמכים נוספים")
הכלי מזהה את שם הלקוח בעצמו מתוך המסמכים - אין צורך להקליד כלום.
ההעלאה למונדיי נשארת ידנית.

חדש בגרסה זו (הקשר התיק):
  מדביקים בסרגל הצד את שמות הסאב-אייטמים הפתוחים של התיק. הרשימה עוברת
  לשני המעברים: הראשון קורא עם ידיעה מה התיק מחכה לו, והשני בוחר יעד מתוך
  תפריט סגור. "לא יודע" היא תשובה מותרת ומועדפת על ניחוש.
  ללא רשימה - הכלי מתנהג בדיוק כמו קודם.
"""

import io
import re
import json
import hashlib
import base64
import zipfile

import fitz  # PyMuPDF
import streamlit as st
from anthropic import Anthropic

# ------------------------------------------------------------------ הגדרות בסיס
st.set_page_config(page_title="מיון וקיבוץ מסמכים", page_icon="🗂️", layout="wide")

st.markdown(
    """
    <style>
      .stApp { direction: rtl; text-align: right; }
      textarea, input { direction: rtl; text-align: right; }
      .stDownloadButton, .stButton { direction: rtl; }
    </style>
    """,
    unsafe_allow_html=True,
)

TOOL_BUILD = "sorter-2026-09-16-v10"         # גרסת כלי המיון (נפרד מ-BUILD של המנוע)

CHEAP_MODEL = "claude-haiku-4-5-20251001"   # דגם זול לקריאה
PRECISE_MODEL = "claude-sonnet-5"           # דגם מדויק לשדרוג ולקיבוץ

# שם התיקייה/היעד לקבצים שלא שויכו לשום סאב-אייטם.
UNMATCHED_LABEL = "מסמכים נוספים"

# דיוק הרינדור. גבוה כששכבת הטקסט שבורה והמודל חייב לקרוא הכל בעיניים;
# נמוך כשהיא תקינה, כי אז התמונה משמשת רק ללוגו ולפריסה והטקסט מצורף בנפרד.
DPI_BROKEN_TEXT = 150
DPI_GOOD_TEXT = 110
MAX_PAGES = 8

# מחירון ברירת מחדל, דולר למיליון טוקנים (קלט/פלט). ניתן לעדכון בסרגל הצד.
DEFAULT_PRICES = {
    CHEAP_MODEL: (1.00, 5.00),     # Haiku 4.5
    PRECISE_MODEL: (2.00, 10.00),  # Sonnet 5
}

IMAGE_MIME = {
    "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
    "webp": "image/webp", "gif": "image/gif",
}

FIELDS = {"doc_type": "אחר", "source": None, "date_start": None, "date_end": None,
          "period_label": None, "account_last3": None, "property_address": None,
          "person_name": None, "business_name": None, "page_num": None, "page_total": None,
          "balance_start": None, "balance_end": None, "read_mode": "",
          "summary": "", "confidence": 0.0}

# balance_start / balance_end נאספים כבר עכשיו לצורך בדיקת רצף עתידית:
# אם יתרת הסגירה של דף אחד שווה ליתרת הפתיחה של הדף הבא - הרצף שלם, גם אם
# יש תקופה בלי תנועות. אם היא קופצת - באמת חסר דף. הלוגיקה עצמה עוד לא נבנתה.

# כללי מתן-שמות ברירת מחדל (ניתן לערוך במסך). מבוסס על טבלת "איך לקרוא לדוח".
DEFAULT_NAMING_RULES = """בנה שם קובץ (בלי סיומת) לפי סוג המסמך, בסדר: מילת-סוג + מזהים.
כלל: הוסף רכיב רק אם הוא מופיע במסמך או ברור. רכיב בסוגריים מרובעים [ ] הוסף
רק אם יש יותר מאחד מאותו סוג בין הקבצים שהועלו (כמה חשבונות/נכסים/מקומות עבודה).
את "שם פרטי לקוח" קח מהשדה person_name שחולץ מהמסמך.

— בנקאות וחשבונות —
תנועות עו״ש:  "תנועות עוש <DD.MM-DD.MM> <בנק> <שם פרטי לקוח> [<3 ספרות אחרונות של החשבון>]"
אישור ניהול חשבון:  "אישור ניהול חשבון <בנק> <שם פרטי לקוח> [<3 ספרות אחרונות>]"
ריכוז יתרות:  "ריכוז יתרות <בנק> <שם פרטי לקוח> [<3 ספרות אחרונות>]"
   (בדיסקונט קרא לו "פירוט תיק לקוח דיסקונט"; בבינלאומי קרא לו "שערוך יתרות")
פירוט הלוואות:  "פירוט הלוואות <בנק> <שם פרטי לקוח> [<3 ספרות אחרונות>]"
תעודת זהות בנקאית:  "ת.ז בנקאית <שנת הדוח> <שם פרטי לקוח> <בנק> [<3 ספרות אחרונות>]"

— משכנתאות (לפי בנק) —
יתרה לסילוק משכנתה:  "יתרה לסילוק <בנק> [<כתובת הנכס>]"
התנהלות משכנתה:  "התנהלות משכנתה <בנק> [<כתובת הנכס>]"

— תעסוקה והכנסה —
תלושי משכורת:  "תלושים <MM-MM> <שם פרטי לקוח> [<שם עסק>]"
תלוש אחרון ממקום קודם:  "תלוש אחרון ממקום עבודה קודם <שם פרטי לקוח> [<שם עסק>]"
טופס 106:  "טופס 106 <שם פרטי לקוח> [<שם עסק>]"
אישור העסקה:  "אישור העסקה <שם פרטי לקוח> <שם עסק>"

— מקרקעין ונכסים —
נסח טאבו מלא:  "נסח טאבו [<כתובת נכס>]"
נסח טאבו מרוכז:  "נסח טאבו מרוכז [<כתובת נכס>]"
אישור זכויות (רמי/עמידר/עמיגור/חברה משכנת):  "אישור זכויות <הגוף> [<כתובת נכס>]"
שובר ארנונה:  "שובר ארנונה [<כתובת נכס>]"
צו רישום בית:  "צו רישום בית [<כתובת נכס>]"
גרמושקה:  "גרמושקה [<כתובת נכס>]"
היתר בניה:  "היתר בניה [<כתובת נכס>]"
הסכם שכירות:  "הסכם שכירות [<כתובת>]"
הסכם מכר:  "הסכם מכר [<כתובת>]"

— אשראי, חובות והוצאה לפועל —
דוח נתוני אשראי:  "דנא <שם פרטי לקוח>"
דוח נתוני אשראי לפני מחיקה:  "דנא לפני מחיקה <שם פרטי לקוח>"
דוח תמצית נתונים בי.די.איי:  "בידיאי <שם פרטי לקוח>"
דוח אשראי צרכני די.אנד.בי:  "דיאנבי <שם פרטי לקוח>"
דוח קרדיטצ׳ק:  "דוח קרדיטצ׳ק <שם חברה>"
אישור היעדר חובות הוצל״פ:  "אישור היעדר חובות הוצלפ <שם פרטי לקוח>"
דוח תיקים לחייב:  "דוח תיקים לחייב <שם פרטי לקוח>"
צו הפטר:  "צו הפטר <שם פרטי לקוח>"

— זהות ומסמכים אישיים —
ספח/כרטיס ת.ז:  "ת.ז <שם פרטי לקוח>"
רישיון נהיגה:  "רשיון נהיגה <שם פרטי לקוח>"
צוואה:  "צוואה <שם פרטי הנפטר> ז״ל"
תעודת פטירה:  "תעודת פטירה <שם פרטי הנפטר> ז״ל"
תעודת לידה:  "תעודת לידה <שם פרטי הילד>"
הסכם גירושין:  "הסכם גירושין <שם פרטי לקוח>"

— עסק וחברה —
תעודת התאגדות:  "תעודת התאגדות <שם חברה>"
נסח חברה:  "נסח חברה <שם חברה>"
עוסק מורשה/פטור:  "עוסק מורשה/פטור <שם פרטי לקוח>"

— רואה חשבון ומס —
אישור רו״ח על הכנסות:  "אישור רוח <שנת הדוח> [<שם עסק>]"
אישור ארכה רו״ח:  "אישור ארכה רוח <שנת הארכה>"
שומת מס:  "שומת מס <שנת הדוח>"
דוח רווח והפסד:  "דוח רוו״ה <שנת הדוח> [<שם עסק>]"
דוח מבוקר:  "דוח מבוקר <שנת הדוח> [<שם חברה>]"
דוח מע״מ:  "דוח מעמ <שנת הדוח> [<שם עסק>]"
ביטוח לאומי:  "ביטוח לאומי <שנת הדוח> [<שם עסק>]"

— קצבאות ורפואי —
אישור נכות רפואית:  "אישור נכות רפואית <שם פרטי לקוח>"
אישור על תשלומי קצבאות:  "אישור על תשלומי קצבאות <שם פרטי לקוח>"

אם סוג המסמך לא מופיע כאן: "<תיאור קצר> <תאריך>". אם אינך בטוח בסוג – החזר confidence נמוך."""

# --------------------------------------------------------------- פונקציות עזר


# מפתחות שערכם מספרי, לצורך המרה אוטומטית אחרי הקריאה
_NUM_KEYS = {"confidence", "target_confidence", "page_num", "page_total",
             "balance_start", "balance_end", "target_index"}


def _coerce(key: str, val: str):
    """ממיר ערך טקסטואלי לטיפוס הנכון. ריק / 'null' / '-' הופכים ל-None."""
    v = val.strip()
    if v == "" or v.lower() in ("null", "none", "-", "אין", "לא ידוע"):
        return None
    if key == "indices":
        out = []
        for part in v.replace("[", " ").replace("]", " ").replace(",", " ").split():
            try:
                out.append(int(part))
            except ValueError:
                pass
        return out
    if key in _NUM_KEYS:
        cleaned = v.replace(",", "").replace("%", "").strip()
        try:
            f = float(cleaned)
            return int(f) if f == int(f) and key not in (
                "confidence", "target_confidence", "balance_start", "balance_end") else f
        except ValueError:
            return None
    return v


def parse_kv(text: str, allowed: set) -> list:
    """קורא תשובה בפורמט שורות 'מפתח: ערך' ומחזיר רשימת רשומות.

    למה לא JSON: שמות ישראליים מכילים גרש כפול (בע"מ, עו"ש, נספח "יב"), והוא
    תו מבנה ב-JSON. כל טלאי שתיקן מקרה אחד נשבר על מקרה אחר. כאן הגרש הוא תו
    רגיל לחלוטין - מה שמפריד בין שדות הוא סוף שורה, ובין מפתח לערך הנקודתיים
    הראשונות. שניהם אינם מופיעים בתוך שמות מסמכים, ולכן אין מה לשבור.

    שורה שמתחילה ב--- פותחת רשומה חדשה. שורה שאינה 'מפתח מוכר: ערך' מתעלמים
    ממנה, כך שפתיח או הסבר מהמודל לא מפילים את הקריאה.
    """
    records, cur = [], {}
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("---"):
            if cur:
                records.append(cur)
            cur = {}
            continue
        if ":" not in s:
            continue
        key, _, val = s.partition(":")
        key = key.strip().strip("*-# ").lower()
        if key not in allowed:
            continue
        if key in cur and cur[key] not in (None, ""):
            records.append(cur)       # אותו מפתח שוב = רשומה חדשה בלי מפריד
            cur = {}
        cur[key] = _coerce(key, val)
    if cur:
        records.append(cur)
    return records


def _extract_json(text: str):
    """מחלץ את אובייקט ה-JSON מהתשובה, גם אם יש טקסט מסביב או גרשיים שבורים."""
    t = _clean_json(text)
    i, j = t.find("{"), t.rfind("}")
    core = t[i:j + 1] if (i != -1 and j > i) else t
    for candidate in (t, core, _repair_json_quotes(t), _repair_json_quotes(core)):
        try:
            return json.loads(candidate)
        except Exception:
            continue
    raise ValueError("no json")


def _repair_json_quotes(text: str) -> str:
    """מתקן גרשיים לא-מוברחים בתוך ערכי מחרוזת ב-JSON.

    למה צריך: שמות ישראליים מכילים גרש כפול רגיל - בע"מ, עו"ש, נספח "יב".
    כשהמודל מעתיק אותם לתוך ערך JSON, הגרש נקרא כסוגר מחרוזת והפענוח נשבר.

    איך: סורקים תו-תו. כשאנחנו בתוך מחרוזת ונתקלים בגרש, מציצים קדימה: אם אחריו
    בא תו מבנה (פסיק, נקודתיים, סוגר) או סוף הטקסט - זה גרש סוגר אמיתי. אחרת
    הוא חלק מהתוכן, ומבריחים אותו. גישה זו כללית ואינה תלויה בשפה.
    """
    out, in_str, esc, n = [], False, False, len(text)
    for i, ch in enumerate(text):
        if esc:
            out.append(ch)
            esc = False
            continue
        if ch == "\\":
            out.append(ch)
            esc = True
            continue
        if ch == '"':
            if not in_str:
                in_str = True
                out.append(ch)
                continue
            j = i + 1
            while j < n and text[j] in " \t\r\n":
                j += 1
            if j >= n or text[j] in ",:}]":
                in_str = False
                out.append(ch)
            else:
                out.append('\\"')      # גרש תוכן - מבריחים
            continue
        out.append(ch)
    return "".join(out)


def _clean_json(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.lstrip().lower().startswith("json"):
            text = text.lstrip()[4:]
    return text.strip().strip("`").strip()


def reading_bytes(name: str, data: bytes, only_edges: bool) -> bytes:
    """מחזיר את הבייטים לקריאה. ב-PDF במצב חסכוני – רק עמוד ראשון + אחרון."""
    ext = name.rsplit(".", 1)[-1].lower()
    if ext != "pdf" or not only_edges:
        return data
    doc = fitz.open(stream=data, filetype="pdf")
    n = doc.page_count
    if n <= 2:
        doc.close()
        return data
    new = fitz.open()
    new.insert_pdf(doc, from_page=0, to_page=0)
    new.insert_pdf(doc, from_page=n - 1, to_page=n - 1)
    out = new.tobytes()
    new.close()
    doc.close()
    return out


def _text_layer_verdict(doc) -> str:
    """מאבחן את שכבת הטקסט של ה-PDF. מחזיר "broken" או "ok".

    שלושה מצבי כשל נפוצים במסמכים ישראליים:
      1. קידוד עברי ישן - הטקסט יוצא כתווים לטיניים משובשים ואפס עברית.
      2. מסמך סרוק - כמעט אין טקסט בכלל.
      3. טקסט דליל מדי מכדי להסתמך עליו.
    בכל אחד מהם חייבים לקרוא את המסמך כתמונה.
    """
    sample = "".join(doc[i].get_text() for i in range(min(2, doc.page_count)))
    if len(sample.strip()) < 50 * min(2, doc.page_count):
        return "broken"                      # סרוק או ריק
    heb = sum(1 for c in sample if "\u0590" <= c <= "\u05FF")
    mojibake = sum(1 for c in sample if "\u00C0" <= c <= "\u00FF")
    if heb == 0 and mojibake > 20:
        return "broken"                      # קידוד עברי ישן
    if heb < len(sample) * 0.05:
        return "broken"                      # כמעט בלי עברית - חשוד
    return "ok"


def _render(page, dpi: int, cap: int = 1568) -> bytes:
    """מרנדר עמוד ל-PNG, עם תקרת גודל (מעבר לה התמונה ממילא מוקטנת בצד השני)."""
    pix = page.get_pixmap(dpi=dpi)
    if max(pix.width, pix.height) > cap:
        pix = page.get_pixmap(dpi=int(dpi * cap / max(pix.width, pix.height)))
    return pix.tobytes("png")


def file_page_count(name: str, data: bytes) -> int:
    """מספר העמודים האמיתי בקובץ. נמדד מקומית, בלי לשאול את המודל."""
    if not name.lower().endswith(".pdf"):
        return 1
    try:
        d = fitz.open(stream=data, filetype="pdf")
        n = d.page_count
        d.close()
        return n
    except Exception:
        return 1


def _file_blocks(name: str, data: bytes, only_edges: bool) -> tuple:
    """בונה את הבלוקים לשליחה. מחזיר (בלוקים, תיאור_מצב_הקריאה).

    PDF נקרא תמיד כתמונות, ולא כמסמך. הסיבה כלכלית ולא רק איכותית: שליחת PDF
    כמסמך גורמת לשרת לחייב גם על הטקסט וגם על תמונה של כל עמוד. תמונות בלבד
    עולות פחות, ומונעות את בעיית הקידוד העברי השבור.

    הדיוק נקבע לפי איכות שכבת הטקסט: כשהיא שבורה המודל חייב לקרוא הכל בעיניים,
    ולכן דיוק גבוה. כשהיא תקינה מצרפים את הטקסט המחולץ כרשת ביטחון, והתמונה
    נחוצה רק ללוגו ולפריסה - אז אפשר להסתפק בדיוק נמוך וזול יותר.
    """
    ext = name.rsplit(".", 1)[-1].lower()

    def img(b: bytes, mime: str):
        return {"type": "image",
                "source": {"type": "base64", "media_type": mime,
                           "data": base64.standard_b64encode(b).decode("utf-8")}}

    if ext in IMAGE_MIME:
        return [img(data, IMAGE_MIME[ext])], "תמונה"

    if ext != "pdf":
        return [], "לא נתמך"

    try:
        doc = fitz.open(stream=data, filetype="pdf")
        n = doc.page_count
        # במצב חסכוני קוראים את שני העמודים הראשונים ואת האחרון. עמוד ראשון
        # לבדו לא מספיק: במסמכים ארוכים (שומה, הסכם) הוא לעתים דף שער או נספח,
        # וזיהוי לפיו בלבד מטעה.
        idxs = sorted({0, 1, n - 1}) if (only_edges and n > 3) else list(range(n))
        idxs = [i for i in idxs if 0 <= i < n][:MAX_PAGES]

        verdict = _text_layer_verdict(doc)
        dpi = DPI_BROKEN_TEXT if verdict == "broken" else DPI_GOOD_TEXT
        blocks = [img(_render(doc[i], dpi), "image/png") for i in idxs]

        if verdict == "ok":
            txt = "\n".join(doc[i].get_text() for i in idxs).strip()
            if txt:
                blocks.append({
                    "type": "text",
                    "text": ("טקסט שחולץ מהקובץ (ייתכן שסדר המילים משובש - "
                             "התמונות הן המקור המהימן):\n" + txt[:12000])
                })
        doc.close()
        note = "ויזואלי (טקסט שבור)" if verdict == "broken" else "ויזואלי + טקסט"
        return blocks, note
    except Exception:
        # נפילה חזרה בטוחה: אם הרינדור נכשל, נשלח את ה-PDF כמסמך כמו קודם
        return ([{"type": "document",
                  "source": {"type": "base64", "media_type": "application/pdf",
                             "data": base64.standard_b64encode(data).decode("utf-8")}}],
                "מסמך (נפילה חזרה)")


def _track_usage(model: str, resp) -> None:
    """צובר טוקנים בפועל מתשובת השרת.

    נספרים רק קריאות אמיתיות; קובץ שנשלף מהזיכרון לא עובר כאן, כי לא שולם
    עליו. המספרים מגיעים מהשרת עצמו ולא מאומדן שלנו.
    """
    u = getattr(resp, "usage", None)
    if u is None:
        return
    st.session_state.setdefault("usage", {})
    d = st.session_state["usage"].setdefault(model, {"in": 0, "out": 0, "calls": 0})
    d["in"] += getattr(u, "input_tokens", 0) or 0
    d["out"] += getattr(u, "output_tokens", 0) or 0
    d["calls"] += 1


def _case_context_block(case_docs: list) -> str:
    """טקסט הקשר קצר למעבר 1: מה התיק מחכה לו. ריק אם אין רשימה."""
    if not case_docs:
        return ""
    lines = "\n".join(f"- {d}" for d in case_docs)
    return (
        "\nהקשר: התיק שאליו שייך המסמך ממתין למסמכים הבאים:\n" + lines +
        "\nהשתמש בהקשר כדי לדייק את הזיהוי, אך אל תכריח התאמה: אם המסמך אינו "
        "אחד מהם - דווח מה שהוא באמת.\n"
    )


def analyze_one(client: Anthropic, model: str, name: str, data: bytes,
                case_docs: list = None, only_edges: bool = True) -> dict:
    """מעבר 1 – קריאת קובץ בודד. מחזיר תמיד dict עם כל השדות."""
    result = dict(FIELDS)
    blocks, read_note = _file_blocks(name, data, only_edges)
    result["read_mode"] = read_note
    if not blocks:
        result["summary"] = f"סוג קובץ לא נתמך: {name}"
        return result
    prompt = (
        "זהו מסמך שלקוח שלח למשרד ייעוץ פיננסי. קרא אותו והחזר את השדות הבאים,\n"
        "כל שדה בשורה נפרדת, בפורמט 'מפתח: ערך'. בלי JSON, בלי סוגריים, בלי\n"
        "מרכאות מסביב לערכים, ובלי טקסט נוסף לפני או אחרי.\n"
        "שדה שאין לו ערך - השאר ריק אחרי הנקודתיים.\n\n"
        "doc_type: תיאור קצר בעברית של סוג המסמך (תנועות עו״ש / נסח טאבו / תלוש שכר)\n"
        "source: שם בנק, מוסד או מעסיק\n"
        "date_start: YYYY-MM-DD\n"
        "date_end: YYYY-MM-DD\n"
        "period_label: חודשים או שנה, למשל 04-06/2026\n"
        "account_last3: שלוש ספרות אחרונות של מספר החשבון\n"
        "property_address: כתובת נכס אם רלוונטי\n"
        "person_name: שם האדם שהמסמך שייך לו - העובד בתלוש, בעל החשבון בדוח\n"
        "business_name: שם עסק או חברה\n"
        "page_num: מספר הדף לפי המספור המודפס\n"
        "page_total: סך הדפים באותו מספור\n"
        "balance_start: יתרת הפתיחה בדף, אם זה דף תנועות\n"
        "balance_end: יתרת הסגירה בדף, אם זה דף תנועות\n"
        "summary: משפט קצר בעברית\n"
        "confidence: מספר בין 0 ל-1\n\n"
        "הנחיות תוכן:\n"
        "date_start/date_end הם טווח התאריכים שבמסמך. יום בודד - שים בשניהם.\n"
        "balance_start/balance_end משמשים לבדיקת רצף בין דפים, אז דייק בהם.\n"
        "person_name הוא שם אדם בלבד. לא כתובת מגורים, לא שם רחוב, ולא שם "
        "המעסיק. אם יש ספק - השאר ריק.\n"
        "את שם הבנק (source) קח מהלוגו או מהכותרת, לא משורות התנועות: בדוח של "
        "בנק אחד מופיעות תנועות בבנקים אחרים (למשל כספומט לאומי בדוח של מזרחי), "
        "והן אינן מעידות על מנפיק הדוח. אם הלוגו לא קריא - השאר ריק והורד "
        "confidence, אל תנחש לפי שכיחות מילים.\n"
        "מסמך שיש בו תלוש שכר לחודש, ברוטו, נטו וניכויי חובה - הוא תלוש שכר, "
        "גם אם מופיע בו מספר חשבון בנק.\n"
        "ייתכן שקיבלת כמה תמונות של אותו קובץ. page_num מתייחס לעמוד הראשון "
        "שקיבלת, לא לאחרון."
        + _case_context_block(case_docs)
    )
    resp = client.messages.create(
        model=model, max_tokens=700,
        messages=[{"role": "user",
                   "content": blocks + [{"type": "text", "text": prompt}]}],
    )
    _track_usage(model, resp)
    raw = "".join(b.text for b in resp.content if b.type == "text")
    try:
        recs = parse_kv(raw, set(FIELDS))
        if not recs:
            recs = [_extract_json(raw)]          # נפילה חזרה אם חזר JSON בכל זאת
        parsed = recs[0]
        for k in FIELDS:
            if parsed.get(k) is not None:
                result[k] = parsed[k]
    except Exception as e:
        # חושפים את הסיבה במקום להבליע: בלי זה הקובץ יוצא "אחר / 0.0" בלי הסבר,
        # וזו בדיוק הסיטואציה שקשה לאבחן בה מה קרה.
        result["summary"] = f"כשל בפענוח התשובה: {type(e).__name__}"
        result["raw_error"] = raw[:400]
    return result


def _attach_target(group: dict, case_docs: list) -> None:
    """ממיר target_index למחרוזת השם המדויקת של הסאב-אייטם.

    מוודא שהאינדקס תקין ובטווח. כל ערך אחר (null, מחוץ לטווח, לא מספר) הופך
    ל-target=None, כלומר "מסמכים נוספים". עדיף לא לשייך מאשר לשייך לא נכון.
    """
    group.setdefault("note", "")
    if not case_docs:
        group["target"], group["target_conf"] = None, 0.0
        return
    idx = group.get("target_index")
    try:
        idx = int(idx)
    except (TypeError, ValueError):
        idx = None
    if idx is None or not (0 <= idx < len(case_docs)):
        group["target"], group["target_conf"] = None, 0.0
        return
    group["target"] = case_docs[idx]
    try:
        group["target_conf"] = float(group.get("target_confidence") or 0.0)
    except (TypeError, ValueError):
        group["target_conf"] = 0.0


def _targeting_prompt(case_docs: list) -> str:
    """הנחיית השיוך לסאב-אייטם. ריקה כשאין רשימה (אז הכלי מתנהג כמו קודם)."""
    if not case_docs:
        return ""
    menu = "\n".join(f"{i}. {d}" for i, d in enumerate(case_docs))
    return (
        "שיוך לסאב-אייטם\n"
        "התיק ממתין למסמכים הבאים, ממוספרים:\n" + menu + "\n\n"
        "לכל קבוצה החזר target_index – המספר מהרשימה שאליו המסמך שייך, "
        "ו-target_confidence בין 0 ל-1.\n"
        "כללי השיוך:\n"
        "0. פריט ברשימה יכול להיות שם מסמך מדויק ('עובר ושב 3 חודשים - בנק "
        "דיסקונט') או דלי-קטגוריה רחב ('מסמכי בנקים', 'מסמכי הכנסות', "
        "'אישור זכויות'). זהה לבד באיזה סוג מדובר.\n"
        "   כשהפריט הוא דלי-קטגוריה - שייך אליו כל מסמך שנופל בקטגוריה, גם אם "
        "שמו המדויק שונה. דוגמאות: נסח טאבו, אישור זכויות, שובר ארנונה וצו רישום "
        "בית שייכים כולם לדלי של מסמכי זכויות בנכס; תלוש שכר, טופס 106 ואישור "
        "רו\"ח שייכים לדלי הכנסות; תנועות עו\"ש, אישור ניהול חשבון וריכוז יתרות "
        "שייכים לדלי מסמכי בנקים. אל תדרוש התאמת שם מילולית בדלי רחב.\n"
        "   אם קיים גם דלי רחב וגם פריט מדויק שמתאים - בחר במדויק.\n"
        "1. ההתאמה היא לפי מהות, לא לפי מילים. השווה סוג מסמך, מוסד, ובעל המסמך.\n"
        "2. שם הבנק ברשימה הוא השם הרשמי המלא (למשל 'בנק דיסקונט לישראל בע\"מ') "
        "ובמסמך הוא לרוב מקוצר ('דיסקונט'). זו התאמה תקפה.\n"
        "3. חוק ברזל: אם ברשימה יש כמה פריטים מאותו סוג שנבדלים בשם האדם או בבנק – "
        "חייבים להתאים גם את השם וגם את הבנק. אל תבחר על סמך הסוג בלבד.\n"
        "   זה חל גם על דליים: אם יש 'מסמכי הכנסות' וגם 'מסמכי הכנסות של האישה', "
        "הכרעה לפי person_name היא חובה. אם השם במסמך לא זוהה - "
        "החזר null ואל תנחש מי מבני הזוג.\n"
        "4. אם אין התאמה ברורה, או שיש שתי אפשרויות ואינך יכול להכריע – "
        "החזר target_index: null. זו תשובה נכונה ועדיפה על ניחוש; "
        "הקובץ יטופל ידנית. אל תכריח שיוך.\n"
        "5. target_confidence נפרד מ-confidence: אפשר לזהות מסמך בוודאות מלאה "
        "ועדיין לא לדעת לאיזה פריט הוא שייך.\n\n"
    )


def group_files(client: Anthropic, model: str, per_file: list, naming_rules: str,
                case_docs: list = None):
    """מעבר 2 – קיבוץ, שיוך לסאב-אייטם ומתן-שמות. מחזיר (קבוצות, שגיאה_אם_יש).

    כשמועברת רשימת סאב-אייטמים (case_docs), כל קבוצה מקבלת גם target_index –
    אינדקס מתוך הרשימה, או null אם אין התאמה ברורה. עבודה באינדקס ולא במחרוזת
    מונעת אי-התאמות של ניסוח (למשל "בנק דיסקונט" מול "בנק דיסקונט לישראל בע״מ").
    """
    case_docs = case_docs or []
    payload = [
        {"index": i, "filename": f["filename"],
         "file_pages": f.get("file_pages", 1),
         **{k: f["a"].get(k) for k in FIELDS if k != "read_mode"}}
        for i, f in enumerate(per_file)
    ]
    prompt = (
        "קיבלת רשימת דפים/קבצים שלקוח שלח. קבץ יחד קבצים שהם אותו מסמך לוגי.\n\n"
        "סימני היכר לאותו מסמך:\n"
        "1. מספור דפים – זה הסימן החזק ביותר. אם page_total זהה (למשל כמה דפים עם 'מתוך 5'), "
        "ואותו סוג ומקור – זה מסמך אחד, וכל הדפים 1..page_total שייכים לו.\n"
        "2. אותו סוג מסמך + אותו מקור (בנק) + אותו מספר חשבון.\n"
        "3. תאריכים רציפים או חופפים.\n\n"
        "file_pages = מספר העמודים האמיתי בקובץ, נמדד ולא משוער. אם file_pages "
        "שווה ל-page_total, הקובץ שלם ואין עמודים חסרים - אל תדווח חוסר.\n"
        "שים לב: שני מסמכים מאותו בנק אך בטווחי תאריכים שונים = שני מסמכים נפרדים.\n"
        "חריג חשוב - מסמכים תקופתיים שנדרשים כסדרה: תלושי שכר, וכן כל מסמך "
        "שהסאב-אייטם מבקש ממנו כמה חודשים ('3 חודשים אחרונים'). תלושים של אותו "
        "אדם ואותו מעסיק בחודשים עוקבים הם מסמך לוגי אחד ויש לאחד אותם לקבוצה "
        "אחת, שתמוזג לקובץ אחד. הכלל של 'טווחי תאריכים שונים = מסמכים נפרדים' "
        "אינו חל עליהם.\n"
        "לעומת זאת תלושים של שני אנשים שונים לעולם לא באותה קבוצה.\n"
        "חוק ברזל: לעולם אל תשים באותה קבוצה מסמכים של שני אנשים שונים.\n"
        "שם האדם מופיע לרוב רק בדף הראשון; אם דף אחד בקבוצה מכיל person_name – הוא תקף לכל הקבוצה.\n"
        "בשם הקובץ: קח את התאריך המוקדם ביותר ואת המאוחר ביותר מכל דפי הקבוצה.\n"
        "אם המסמך ברור – תן confidence גבוה (0.8-1). הורד רק אם באמת לא ברור.\n\n"
        f"{naming_rules}\n\n"
        + _targeting_prompt(case_docs) +
        "פורמט התשובה: לכל קבוצה בלוק שמתחיל בשורת --- ואחריו שדות, כל שדה\n"
        "בשורה נפרדת בפורמט 'מפתח: ערך'. בלי JSON, בלי מרכאות מסביב לערכים,\n"
        "ובלי טקסט נוסף לפני או אחרי. לדוגמה:\n\n"
        "---\n"
        "indices: 0,1,2\n"
        "doc_type: תלושי שכר\n"
        "final_name: תלושים 05-07 טלי\n"
        "confidence: 0.95\n"
        "target_index: 1\n"
        "target_confidence: 0.9\n"
        "note: \n"
        "---\n"
        "indices: 3\n"
        "doc_type: תנועות עו״ש\n"
        "final_name: תנועות עוש 04.06-05.08 מזרחי אנדרגה\n"
        "confidence: 0.9\n"
        "target_index: \n"
        "target_confidence: 0\n"
        "note: לא נמצא יעד מתאים\n\n"
        "כל קובץ חייב להופיע בקבוצה אחת בדיוק. סדר את ה-indices לפי page_num "
        "(ואם אין – לפי תאריך). target_index ריק פירושו שאין התאמה.\n\n"
        "הקבצים:\n" + json.dumps(payload, ensure_ascii=False, indent=1)
    )
    try:
        resp = client.messages.create(
            model=model, max_tokens=8000,
            messages=[{"role": "user", "content": [{"type": "text", "text": prompt}]}],
        )
        _track_usage(model, resp)
        raw = "".join(b.text for b in resp.content if b.type == "text")
        stop = getattr(resp, "stop_reason", None)
        allowed = {"indices", "doc_type", "final_name", "confidence",
                   "target_index", "target_confidence", "note"}
        groups = [g for g in parse_kv(raw, allowed) if g.get("indices")]
        if not groups:
            groups = _extract_json(raw).get("groups", [])   # נפילה חזרה ל-JSON
        if not groups:
            raise ValueError("empty groups")
        # ודא שכל קובץ שויך; מה שנשמט – לקבוצה משלו
        seen = {i for g in groups for i in g.get("indices", [])}
        for i in range(len(per_file)):
            if i not in seen:
                groups.append({"indices": [i], "doc_type": per_file[i]["a"].get("doc_type"),
                               "final_name": per_file[i]["filename"].rsplit(".", 1)[0],
                               "confidence": 0.0, "note": "לא שויך לקבוצה"})
        for g in groups:
            _attach_target(g, case_docs)
        err = "התשובה נחתכה באמצע – ייתכן שהקיבוץ חלקי." if stop == "max_tokens" else None
        return groups, err
    except Exception as e:
        fallback = [{"indices": [i], "doc_type": f["a"].get("doc_type", "אחר"),
                     "final_name": f["filename"].rsplit(".", 1)[0],
                     "confidence": 0.0, "target": None, "target_conf": 0.0,
                     "note": "לא ניתן לקבץ אוטומטית"}
                    for i, f in enumerate(per_file)]
        return fallback, f"שלב הקיבוץ נכשל: {type(e).__name__}: {e}"


def merge_to_pdf(files_bytes: list, names: list) -> bytes:
    out = fitz.open()
    for data, name in zip(files_bytes, names):
        ext = name.rsplit(".", 1)[-1].lower()
        if ext == "pdf":
            out.insert_pdf(fitz.open(stream=data, filetype="pdf"))
        elif ext in IMAGE_MIME:
            img = fitz.open(stream=data, filetype=ext if ext != "jpg" else "jpeg")
            out.insert_pdf(fitz.open("pdf", img.convert_to_pdf()))
    buf = out.tobytes()
    out.close()
    return buf


def html_table(rows: list) -> str:
    """בונה טבלה כ-HTML, בלי pandas ובלי st.dataframe.

    כלל זהב בפרויקט: pandas / st.table / st.dataframe קרסו בפריסה. לכן כל
    טבלה נבנית כאן. מקבל רשימת מילונים; מפתחות השורה הראשונה הם הכותרות.
    """
    if not rows:
        return ""
    heads = list(rows[0].keys())

    def esc(v):
        s = "" if v is None else str(v)
        return (s.replace("&", "&amp;").replace("<", "&lt;")
                 .replace(">", "&gt;").replace('"', "&quot;"))

    th_style = "padding:6px 8px;text-align:right"
    td_style = "padding:6px 8px;border-bottom:1px solid #e2e7ea"
    th = "".join(f"<th style='{th_style}'>{esc(h)}</th>" for h in heads)
    tr = "".join(
        "<tr>" + "".join(f"<td style='{td_style}'>{esc(r.get(h))}</td>" for h in heads)
        + "</tr>"
        for r in rows
    )
    return (
        "<div style='overflow-x:auto'>"
        "<table style='width:100%;border-collapse:collapse;direction:rtl;"
        "font-size:0.88rem'>"
        "<thead><tr style='background:#0f2b3d;color:#fff'>" + th + "</tr></thead>"
        "<tbody>" + tr + "</tbody></table></div>"
    )


def render_cost_panel(prices: dict, slot=None) -> None:
    """מצייר את מד העלות בסרגל הצד.

    סרגל הצד מצויר לפני שההרצה מתחילה, ולכן אי אפשר פשוט לכתוב אותו שם - הוא
    יראה תמיד את ההרצה הקודמת. הפתרון: שומרים מקום ריק בסרגל בזמן הציור,
    וממלאים אותו אחרי שההרצה הסתיימה.
    """
    box = slot.container() if slot is not None else st
    usage = st.session_state.get("usage", {})
    if not usage:
        box.caption("עוד לא בוצעו קריאות בהפעלה הזו.")
        return
    rows, total = [], 0.0
    for mdl, d in usage.items():
        pin, pout = prices.get(mdl, (0.0, 0.0))
        cost = d["in"] / 1e6 * pin + d["out"] / 1e6 * pout
        total += cost
        rows.append({"דגם": "זול" if mdl == CHEAP_MODEL else "מדויק",
                     "קריאות": d["calls"],
                     "טוקני קלט": f'{d["in"]:,}', "טוקני פלט": f'{d["out"]:,}',
                     "עלות": f"${cost:.4f}"})
    box.metric("עלות בהפעלה הזו", f"${total:.4f}")
    box.markdown(html_table(rows), unsafe_allow_html=True)
    box.caption("המספרים מדווחים על ידי השרת ולא מאומדן. קבצים שנשלפו "
                "מהזיכרון אינם נספרים - לא שולם עליהם.")
    if box.button("אפס מונה עלות"):
        st.session_state["usage"] = {}
        st.rerun()


def safe_filename(name: str) -> str:
    for ch in '\\/:*?"<>|':
        name = name.replace(ch, "-")
    return name.strip() or "מסמך"


# ------------------------------------------------------------------ ממשק


st.title("🗂️ מיון, קיבוץ ומתן-שמות למסמכים")
st.caption("העלי את כל הקבצים של לקוח. הכלי מזהה, מקבץ, נותן שם וממזג כל קבוצה ל-PDF אחד להורדה.")

with st.sidebar:
    st.header("הגדרות")
    try:
        api_key = st.secrets.get("ANTHROPIC_API_KEY", "")
    except Exception:
        api_key = ""
    if not api_key:
        api_key = st.text_input("מפתח גישה (Anthropic API Key)", type="password")

    mode = st.radio("מצב קריאה",
                    ["חסכוני (זול + שדרוג בעת ספק)", "מדויק (סונט לכל הקבצים)"], index=0)
    economical = mode.startswith("חסכוני")

    only_edges = st.checkbox("ב-PDF לקרוא רק תחילת המסמך + עמוד אחרון (חוסך)",
                             value=True,
                             help="קורא שני עמודים ראשונים ואת האחרון. כבי את זה "
                                  "אם מסמכים ארוכים מזוהים לפי נספח במקום לפי גופם.")
    st.caption("קריאת PDF ויזואלית ואוטומטית: הכלי מאבחן לבד את שכבת הטקסט "
               "ובוחר דיוק בהתאם. אין מה להגדיר.")
    threshold = st.slider("סף ביטחון (מתחתיו: שדרוג לסונט, ואם עדיין נמוך – 'לבדיקה')",
                          0.0, 1.0, 0.7, 0.05)
    match_threshold = st.slider("סף שיוך לסאב-אייטם (מתחתיו: 'מסמכים נוספים')",
                                0.0, 1.0, 0.6, 0.05)
    st.divider()

    st.subheader("הקשר התיק")
    st.caption("הדביקי את שמות הסאב-אייטמים הפתוחים של התיק, שורה לשורה. "
               "אפשר להשאיר ריק – אז הכלי רק מסווג ולא משייך.")
    case_docs_raw = st.text_area(
        "סאב-אייטמים פתוחים",
        placeholder=("תלושי שכר 3 חודשים אחרונים\n"
                     "עובר ושב 3 חודשים — בנק דיסקונט לישראל בע\"מ\n"
                     "אישור ניהול חשבון — בנק דיסקונט לישראל בע\"מ"),
        height=170,
    )
    case_docs = [ln.strip() for ln in (case_docs_raw or "").splitlines() if ln.strip()]
    if case_docs:
        st.success(f"{len(case_docs)} מסמכים ברשימה")

    st.divider()
    naming_rules = st.text_area("כללי מתן-שמות (ניתן לעריכה)", DEFAULT_NAMING_RULES, height=300)

    st.divider()
    st.subheader("עלות")
    with st.expander("מחירון (דולר למיליון טוקנים)"):
        st.caption("ברירת המחדל נכונה לספטמבר 2026. אם המחירים ישתנו – עדכני כאן.")
        p_cheap_in = st.number_input("דגם זול – קלט", value=DEFAULT_PRICES[CHEAP_MODEL][0],
                                     step=0.25, format="%.2f")
        p_cheap_out = st.number_input("דגם זול – פלט", value=DEFAULT_PRICES[CHEAP_MODEL][1],
                                      step=0.25, format="%.2f")
        p_prec_in = st.number_input("דגם מדויק – קלט", value=DEFAULT_PRICES[PRECISE_MODEL][0],
                                    step=0.25, format="%.2f")
        p_prec_out = st.number_input("דגם מדויק – פלט", value=DEFAULT_PRICES[PRECISE_MODEL][1],
                                     step=0.25, format="%.2f")
    prices = {CHEAP_MODEL: (p_cheap_in, p_cheap_out),
              PRECISE_MODEL: (p_prec_in, p_prec_out)}

    st.divider()
    st.subheader("עלות")
    cost_slot = st.empty()

    st.caption(f"גרסת כלי: {TOOL_BUILD}")

uploaded = st.file_uploader(
    "גררי לכאן את כל הקבצים של הלקוח",
    type=["pdf", "jpg", "jpeg", "png", "webp", "gif"],
    accept_multiple_files=True,
)

# --------------------------------------------------- זיכרון תוצאות (למניעת איבוד/כפל תשלום)
if "cache" not in st.session_state:
    st.session_state["cache"] = {}     # קריאות שכבר שולמו
if "results" not in st.session_state:
    st.session_state["results"] = None  # התוצאות האחרונות


def analyze_cached(client, model, name, data, only_edges, case_docs=None):
    """קורא קובץ, אבל אם כבר נקרא בעבר – מחזיר מהזיכרון בלי לשלם שוב.

    ההקשר נכנס למפתח הזיכרון: אותו קובץ תחת רשימת סאב-אייטמים שונה הוא קריאה
    אחרת, ולכן אסור להחזיר תשובה ישנה שנקראה בלי ההקשר.
    """
    rb = reading_bytes(name, data, only_edges)
    ctx = hashlib.sha256("|".join(case_docs or []).encode("utf-8")).hexdigest()[:12]
    key = hashlib.sha256(rb).hexdigest() + "|" + model + "|" + ctx + "|" + TOOL_BUILD
    if key in st.session_state["cache"]:
        return st.session_state["cache"][key], True
    a = analyze_one(client, model, name, rb, case_docs, only_edges)
    st.session_state["cache"][key] = a
    return a, False


def sort_key(m):
    """מיון בתוך קבוצה: קודם לפי מספר הדף, אחרת לפי תאריך, אחרת לפי שם."""
    p = m["a"].get("page_num")
    try:
        p = int(p)
    except (TypeError, ValueError):
        p = None
    d = m["a"].get("date_start") or m["a"].get("date_end") or ""
    return (p is None, p or 0, d == "", d, m["filename"])


c1, c2 = st.columns([1, 1])
with c1:
    run = st.button("עבד קבצים", type="primary", disabled=not (uploaded and api_key))
with c2:
    if st.button("נקה זיכרון קריאות"):
        st.session_state["cache"] = {}
        st.session_state["results"] = None
        st.success("הזיכרון נוקה.")

if run:
    client = Anthropic(api_key=api_key)
    files = [{"filename": f.name, "bytes": f.getvalue()} for f in uploaded]

    # מעבר 1 – קריאה, עם שדרוג בעת ספק
    per_file, from_cache = [], 0
    prog = st.progress(0.0, text="קורא קבצים...")
    for i, f in enumerate(files):
        if economical:
            a, hit = analyze_cached(client, CHEAP_MODEL, f["filename"], f["bytes"],
                                    only_edges, case_docs)
            used = "זול"
            # שדרוג גם כשלא חולץ שם אדם: זה סימן מובהק לקריאה כושלת, גם אם
            # המודל דיווח ביטחון גבוה. בלי שם אין שיוך אפשרי בתיק זוגי.
            if (float(a.get("confidence") or 0) < threshold
                    or not (a.get("person_name") or "").strip()):
                a, hit2 = analyze_cached(client, PRECISE_MODEL, f["filename"], f["bytes"],
                                         only_edges, case_docs)
                used, hit = "שודרג לסונט", hit and hit2
        else:
            a, hit = analyze_cached(client, PRECISE_MODEL, f["filename"], f["bytes"],
                                    only_edges, case_docs)
            used = "סונט"
        from_cache += 1 if hit else 0
        per_file.append({"filename": f["filename"], "bytes": f["bytes"], "a": a,
                         "used": used,
                         "file_pages": file_page_count(f["filename"], f["bytes"])})
        prog.progress((i + 1) / len(files), text=f"נקרא: {f['filename']}")
    prog.progress(1.0, text="מקבץ ונותן שמות...")

    groups, group_err = group_files(client, PRECISE_MODEL, per_file, naming_rules, case_docs)
    if group_err:
        st.error(f"⚠️ {group_err}")
    prog.empty()

    # בניית קבצים ממוזגים
    zip_buf = io.BytesIO()
    ok_groups, review_groups, extra_groups = [], [], []
    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for gi, g in enumerate(groups):
            idxs = g.get("indices", [])
            members = [per_file[i] for i in idxs if 0 <= i < len(per_file)]
            if not members:
                continue
            members.sort(key=sort_key)          # מיון לפי תאריך לפני המיזוג
            pdf = merge_to_pdf([m["bytes"] for m in members], [m["filename"] for m in members])
            fname = safe_filename(g.get("final_name", "מסמך")) + ".pdf"

            # ביטחון: אם המודל לא החזיר – ניקח את המינימום של הקבצים בקבוצה
            conf = g.get("confidence")
            if conf in (None, 0, "0"):
                conf = min((float(m["a"].get("confidence") or 0) for m in members), default=0.0)
            conf = float(conf)

            # שיוך: יעד מתקבל רק אם עבר את סף השיוך. אחרת – מסמכים נוספים.
            target = g.get("target")
            tconf = float(g.get("target_conf") or 0.0)
            if target and tconf < match_threshold:
                target = None

            # סדר ההכרעה: ביטחון זיהוי נמוך גובר על הכל, כי אם לא יודעים מה
            # המסמך – אין טעם לשייך אותו. אחר כך היעד, ולבסוף מסמכים נוספים.
            if conf < threshold:
                bucket, folder = "review", "לבדיקה/"
            elif target:
                bucket, folder = "ok", safe_filename(target) + "/"
            else:
                bucket, folder = "extra", UNMATCHED_LABEL + "/"

            zf.writestr(folder + fname, pdf)
            entry = {"name": fname, "pdf": pdf, "conf": conf, "note": g.get("note", ""),
                     "target": target, "tconf": tconf,
                     "sources": [m["filename"] for m in members], "key": f"g{gi}"}
            {"ok": ok_groups, "review": review_groups, "extra": extra_groups}[bucket].append(entry)

    # אילו סאב-אייטמים מהרשימה לא קיבלו אף מסמך – זה מה שעדיין חסר מהלקוח
    covered = {e["target"] for e in ok_groups if e.get("target")}
    still_missing = [d for d in case_docs if d not in covered]

    st.session_state["results"] = {
        "zip": zip_buf.getvalue(), "ok": ok_groups, "review": review_groups,
        "extra": extra_groups, "missing": still_missing, "has_context": bool(case_docs),
        "table": [{"קובץ": m["filename"], "זוהה כ": m["a"].get("doc_type"),
                   "שם שזוהה": m["a"].get("person_name"),
                   "דף": (f'{m["a"].get("page_num")}/{m["a"].get("page_total")}'
                          if m["a"].get("page_num") else ""),
                   "ביטחון": round(float(m["a"].get("confidence") or 0), 2),
                   "אופן קריאה": m["a"].get("read_mode", ""),
                   "נקרא ב": m["used"]} for m in per_file],
        "n_files": len(files), "from_cache": from_cache,
        "upgraded": sum(1 for m in per_file if m["used"] == "שודרג לסונט"),
        "economical": economical,
    }

# מילוי מד העלות בסרגל – חייב לקרות כאן, אחרי שההרצה הסתיימה
render_cost_panel(prices, cost_slot)

# --------------------------------------------------- הצגת תוצאות (נשמרות גם אחרי הורדה)
R = st.session_state.get("results")
if R:
    n_docs = len(R["ok"]) + len(R["review"]) + len(R.get("extra", []))
    st.success(f"עובדו {R['n_files']} קבצים → {n_docs} מסמכים.")
    if R["from_cache"]:
        st.caption(f"({R['from_cache']} קבצים נלקחו מהזיכרון – לא שולם עליהם שוב)")
    st.download_button("⬇️ הורד הכל (ZIP)", R["zip"], file_name="מסמכים_ממוינים.zip",
                       mime="application/zip", key="zip_all")

    if R["economical"]:
        st.info(f"קריאה: {R['n_files'] - R['upgraded']} קבצים הסתדרו עם הדגם הזול, "
                f"{R['upgraded']} שודרגו לסונט.")
    st.markdown(html_table(R["table"]), unsafe_allow_html=True)

    def show(entry):
        cols = st.columns([3, 1])
        with cols[0]:
            st.markdown(f"**{entry['name']}**")
            if entry.get("target"):
                st.caption(f"↖ לסאב-אייטם: {entry['target']}  ·  ביטחון שיוך "
                           f"{round(entry.get('tconf', 0), 2)}")
            st.caption(f"מ-{len(entry['sources'])} קבצים: {', '.join(entry['sources'])}")
            if entry["note"]:
                st.caption(f"הערה: {entry['note']}")
        with cols[1]:
            st.download_button("הורד", entry["pdf"], file_name=entry["name"],
                               mime="application/pdf", key=entry["key"])

    if R["ok"]:
        st.subheader("✅ שויך ומוכן להעלאה")
        for e in R["ok"]:
            show(e)
    if R.get("extra"):
        st.subheader(f"📁 {UNMATCHED_LABEL} (זוהו, לא שויכו לסאב-אייטם)")
        st.caption("מסמכים תקינים שאין להם יעד ברור ברשימה. "
                   "לא ננחש – העלי אותם ידנית לאן שנכון.")
        for e in R["extra"]:
            show(e)
    if R["review"]:
        st.subheader("⚠️ לבדיקה ידנית (ביטחון זיהוי נמוך)")
        for e in R["review"]:
            show(e)

    if R.get("has_context"):
        st.divider()
        if R["missing"]:
            st.subheader("⏳ עדיין חסר מהלקוח")
            for d in R["missing"]:
                st.markdown(f"- {d}")
        else:
            st.subheader("🎉 כל המסמכים ברשימה התקבלו")