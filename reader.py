"""קריאת מסמכים — הקוד המשותף למסך ולשרת.

למה קובץ נפרד:
    המסך (app.py) והשרת (api.py) חייבים לזהות מסמך בדיוק אותו דבר. אם קוד
    הקריאה יישב בשניהם, הם יתפצלו בתיקון הראשון - ואז מה שנבדק במסך לא
    יהיה מה שרץ באוטומציה.

מה יש כאן:
    המרת PDF לתמונות, אבחון שכבת הטקסט, ההנחיה למודל, פענוח התשובה,
    ומיזוג קבוצה ל-PDF אחד. אין כאן קיבוץ, שיוך או מתן-שמות - אלה
    ב-engine.py.
"""

READER_BUILD = "reader-2026-09-22-v44"

import io
import re
import json
import base64
import hashlib
import zipfile
from datetime import date

import fitz  # PyMuPDF
from anthropic import Anthropic



# מצב ברמת המודול, במקום session_state של סטרימליט: איזו צורת קריאה עובדת,
# ומונה השימוש. כך אותו קוד רץ גם בשרת שאין בו ממשק.
_STATE = {}
_USAGE = {}

# הקבצים נקראים במקביל, ושני המונים האלה משותפים לכל הקריאות. בלי נעילה,
# שתי קריאות שמסתיימות באותו רגע עלולות לדרוס זו את ספירת הטוקנים של זו.
import threading
_LOCK = threading.Lock()


def usage_snapshot() -> dict:
    """מחזיר את צריכת הטוקנים שנצברה, ומאפס."""
    with _LOCK:
        out = {k: dict(v) for k, v in _USAGE.items()}
        _USAGE.clear()
    return out


def _track_usage(model: str, resp) -> None:
    u = getattr(resp, "usage", None)
    if u is None:
        return
    with _LOCK:
        d = _USAGE.setdefault(model, {"in": 0, "out": 0, "calls": 0})
        d["in"] += getattr(u, "input_tokens", 0) or 0
        d["out"] += getattr(u, "output_tokens", 0) or 0
        d["calls"] += 1


def read_file(client: Anthropic, name: str, data: bytes,
              case_docs: list = None, only_edges: bool = True) -> dict:
    """קורא קובץ בודד ומחזיר את השדות שחולצו ממנו.

    תמיד בדגם המדויק: הדגם הזול טעה במסמכים קשים ודיווח ביטחון גבוה על
    תשובה שגויה, ולכן הוא לא משתתף.
    """
    return analyze_one(client, PRECISE_MODEL, name, data, case_docs, only_edges)


CHEAP_MODEL = "claude-haiku-4-5-20251001"   # דגם זול לקריאה


PRECISE_MODEL = "claude-sonnet-5"           # דגם מדויק לשדרוג ולקיבוץ


# שם התיקייה/היעד לקבצים שלא שויכו לשום סאב-אייטם.
UNMATCHED_LABEL = "מסמכים נוספים"


# דיוק הרינדור. גבוה כששכבת הטקסט שבורה והמודל חייב לקרוא הכל בעיניים;
# נמוך כשהיא תקינה, כי אז התמונה משמשת רק ללוגו ולפריסה והטקסט מצורף בנפרד.
DPI_BROKEN_TEXT = 150


DPI_GOOD_TEXT = 110


MAX_PAGES = 8


IMAGE_MIME = {
    "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
    "webp": "image/webp", "gif": "image/gif",
}


FIELDS = {"doc_type": "אחר", "source": None, "date_start": None, "date_end": None,
          "period_label": None, "account_last3": None, "property_address": None,
          "person_name": None, "business_name": None, "branch": None,
          "page_num": None, "page_total": None,
          "balance_start": None, "balance_end": None, "read_mode": "",
          "raw_error": "", "summary": "", "confidence": 0.0}


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
    if key in ("confidence", "target_confidence"):
        words = {"גבוה מאוד": 0.95, "גבוה": 0.9, "בינוני": 0.6,
                 "נמוך": 0.3, "נמוך מאוד": 0.1, "high": 0.9, "medium": 0.6,
                 "low": 0.3}
        if v in words:
            return words[v]
    if key in _NUM_KEYS:
        cleaned = v.replace(",", "").strip()
        pct = cleaned.endswith("%")
        cleaned = cleaned.rstrip("%").strip()
        try:
            f = float(cleaned)
            if pct:
                f = f / 100.0
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
        # שני עמודים ראשונים ושניים אחרונים. העמוד האחרון לבדו לא הספיק:
        # בדוח תנועות ארוך טווח התאריכים נקרא שגוי, ובמסמכים ארוכים העמודים
        # הראשונים הם לעתים דף שער או דף הסבר כללי.
        idxs = (sorted({0, 1, n - 2, n - 1}) if (only_edges and n > 4)
                else list(range(n)))
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


_HEB_MONTHS = ("ינואר", "פברואר", "מרץ", "אפריל", "מאי", "יוני", "יולי",
               "אוגוסט", "ספטמבר", "אוקטובר", "נובמבר", "דצמבר")


def today_context() -> str:
    """מוסר למודל מה התאריך היום.

    בלי זה אי אפשר להכריע מה היא 'שנה נוכחית', 'שנה קודמת' או 'שלושה חודשים
    אחרונים' - וזה בדיוק מה שהכשיל את שיוך שומת המס.
    """
    t = date.today()
    return (f"\n\nהיום {t.day} ב{_HEB_MONTHS[t.month - 1]} {t.year} "
            f"(בפורמט מספרי {t.isoformat()}).\n"
            f"לכן השנה הנוכחית היא {t.year}, שנה קודמת {t.year - 1}, "
            f"ושנתיים אחורה {t.year - 2}. חשב לפי זה כל ביטוי יחסי של זמן - "
            f"שנה נוכחית, שנה קודמת, שלושה חודשים אחרונים - ואל תנחש.\n")


def people_from_docs(case_docs: list) -> list:
    """מחלץ את שמות בעלי התיק מתוך רשימת הסאב-אייטמים.

    במנוע, פריט אישי מסתיים ב"— <שם מלא>" ופריט בנקאי ב"— <שם בנק> (<בעלים>)".
    לכן לוקחים את מה שאחרי המקף האחרון, ומסננים כל מה שנראה כמו בנק או
    כמו הערה בסוגריים. מה שנשאר הוא שם אדם.
    """
    people, seen = [], set()
    for line in case_docs:
        for dash in ("—", "–", " - "):
            if dash in line:
                tail = line.rsplit(dash, 1)[1].strip()
                break
        else:
            continue
        if not tail or "(" in tail or "בנק" in tail or "טפחות" in tail:
            continue
        if tail not in seen:
            seen.add(tail)
            people.append(tail)
    return people


def _people_context(case_docs: list) -> str:
    """הקשר למעבר 1: מי בעלי התיק, ותו לא.

    במכוון אין כאן את רשימת המסמכים. כשהעברנו אותה, המודל נמשך להתאים את
    המסמך שלפניו לאחד הפריטים ברשימה במקום לקרוא מה הוא באמת - ותלוש שכר
    זוהה כדוח משכנתא רק משום שמשכנתא הופיעה ברשימה. מעבר 1 קורא את המסמך
    כמות שהוא; ההתאמה לרשימה היא תפקידו של מעבר 2 בלבד.
    """
    people = people_from_docs(case_docs)
    if not people:
        return ""
    return (
        "\n\nהתיק הזה שייך לאנשים הבאים: " + ", ".join(people) + ".\n"
        "person_name צריך להיות אחד מהם. אם השם הבולט במסמך אינו אחד מהם, "
        "כמעט תמיד מדובר בחותם, בנציג או בגורם אחר - חפש שוב את שם בעל "
        "המסמך עצמו. רק אם באמת אין התאמה, השאר ריק.\n"
        "המידע הזה נועד אך ורק לזיהוי השם. אל תיתן לו להשפיע על doc_type: "
        "סוג המסמך נקבע ממה שכתוב במסמך עצמו בלבד."
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
        "account_last3: שלוש הספרות האחרונות של מספר החשבון בלבד\n"
        "branch: מספר הסניף\n"
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
        "branch ו-account_last3 - זהירות, אלה שני מספרים שונים: במסמך בנקאי "
        "מופיעים זה לצד זה קוד בנק, מספר סניף ומספר חשבון.\n"
        "  branch = מספר הסניף. לרוב שתיים עד שלוש ספרות, ומופיע תחת הכותרת "
        "'סניף'. אם מופיע גם שם הסניף (למשל 'סניף 2 עפולה') - קח את המספר "
        "בלבד.\n"
        "  account_last3 = שלוש הספרות האחרונות של מספר החשבון. מספר החשבון "
        "הוא הארוך מביניהם, לרוב שש ספרות ומעלה, ומופיע תחת הכותרת 'חשבון' "
        "או 'מספר חשבון'.\n"
        "אל תחליף ביניהם, ואל תיקח קוד בנק או מספר אסמכתא. כל שדה שאינך מזהה "
        "בוודאות - השאר ריק.\n"
        "person_name בחשבון בנק: אם בכותרת הדוח מופיעים שני שמות (חשבון "
        "משותף), רשום את שניהם ולא רק את הראשון. אל תייחס חשבון משותף לאדם "
        "אחד.\n"
        "\nהבחנה בין שלושה מסמכי משכנתא שנראים דומים:\n"
        "  דוח התנהלות משכנתא - היסטוריית התשלומים לאורך זמן, טבלת תשלומים "
        "שבוצעו, פיגורים, ריביות בפועל.\n"
        "  דוח יתרות לסילוק - סכום אחד לתאריך יעד מסוים, מה צריך לשלם היום "
        "כדי לסלק את המשכנתא במלואה. מופיעות בו מילים כמו סילוק, פירעון מוקדם, "
        "יתרה לסילוק, עמלת היוון.\n"
        "  פירוט הלוואות - רשימת ההלוואות הפעילות בחשבון בנק רגיל, לא משכנתא.\n"
        "אם המסמך נוגע למשכנתא ולא להלוואות בחשבון עו\u05F4ש - אל תסווג אותו "
        "כפירוט הלוואות.\n"
        "\nאל תבלבל בין תלוש שכר לדוח תנועות בנק. תלוש שכר מתאר משכורת של חודש "
        "אחד: יש בו 'תלוש שכר לחודש', פירוט תשלומים, ברוטו, נטו וניכויי חובה, "
        "והוא מונפק על ידי מעסיק. דוח תנועות מתאר חשבון בנק לאורך תקופה: יש בו "
        "שורות של תנועות עם תאריך, חובה, זכות ויתרה מצטברת, והוא מונפק על ידי "
        "בנק. מסמך שמופיעים בו ברוטו ונטו הוא תלוש, גם אם מופיעים בו מספר "
        "חשבון ושם בנק - שם הבנק שם הוא רק לצורך העברת המשכורת.\n"
        "בתלוש שכר, source הוא המעסיק - לא חברת הסליקה שמדפיסה את התלוש. "
        "שמות כמו מלם שכר, חילן, מיכפל, סינריון, עוקץ ואורורה הם ספקי תוכנת "
        "שכר ומופיעים על תלושים של מעסיקים רבים. אם מופיע שם כזה לצד שם גוף "
        "אחר - המעסיק הוא הגוף האחר. בתלוש קצבה, source הוא הגוף המשלם "
        "(חברת הביטוח, קרן הפנסיה או הביטוח הלאומי).\n"
        "בתלוש שכר, שם העובד יושב בראש המסמך, ליד מספר העובד ומספר הזהות, "
        "ולצדו כתובת המגורים. תלושים ממעסיקים שונים בנויים אחרת זה מזה, אבל "
        "בכולם זה המקום. שמות שמופיעים בחתימה, בברכה, בכותרת תחתונה, בפרטי "
        "קשר של משאבי אנוש או של חשב - אינם העובד. בתלושים של גופים ציבוריים "
        "מופיעה לעתים ברכה חתומה בשם בכיר; התעלם ממנה.\n"
        "ייתכן שקיבלת כמה תמונות של אותו קובץ. page_num מתייחס לעמוד הראשון "
        "שקיבלת, לא לאחרון."
        + today_context()
        + _people_context(case_docs)
    )
    resp = _create(client, model, 3000,
                   blocks + [{"type": "text", "text": prompt}])
    _track_usage(model, resp)
    raw = _text_of(resp)
    try:
        recs = parse_kv(raw, set(FIELDS))
        if not recs:
            recs = [_extract_json(raw)]          # נפילה חזרה אם חזר JSON בכל זאת
        parsed = recs[0]
        for k in FIELDS:
            if parsed.get(k) is not None:
                result[k] = parsed[k]
        # מספר עמוד חסר היגיון (למשל 332 בלי page_total) הוא לרוב מספר שנקלט
        # בטעות מתוך המסמך - מספר חשבון או אסמכתא. עדיף בלי מאשר שגוי, כי
        # שלב הקיבוץ מסתמך עליו.
        pn, pt = result.get("page_num"), result.get("page_total")
        if isinstance(pn, int) and pn > 50 and not isinstance(pt, int):
            result["page_num"] = None
    except Exception as e:
        # חושפים את הסיבה במקום להבליע: בלי זה הקובץ יוצא "אחר / 0.0" בלי הסבר,
        # וזו בדיוק הסיטואציה שקשה לאבחן בה מה קרה.
        result["summary"] = f"כשל בפענוח התשובה: {type(e).__name__}"
        result["raw_error"] = raw[:400]
    return result


# צורות הקריאה לפי סדר עדיפות. גרסאות שונות של הספרייה ושל המודל מקבלות
# פרמטרים שונים, וסטרימליט מושכת את הגרסה שיש ברגע הפריסה.
#
# הערה חשובה: temperature אינו ברשימה בכוונה. הוא היה הדרך לקבל תשובה קבועה
# לאותו קלט, אבל השרת מחזיר עליו במפורש "deprecated for this model" - הוא
# בוטל ואי אפשר להגדיר אותו. אין מה לנסות.
# מה שכן נשאר: כיבוי החשיבה הפנימית, שחוסך טוקני פלט ומונע מצב שהחשיבה
# בולעת את כל תקרת האורך והתשובה חוזרת ריקה.
_CALL_VARIANTS = [
    {"thinking": {"type": "disabled"}},
    {},
]


def _is_param_error(e: Exception) -> bool:
    """האם השגיאה נובעת מפרמטר שלא התקבל, ולא מתקלה אמיתית.

    שני מצבים: הספרייה לא מכירה את הפרמטר (TypeError), או שהספרייה מעבירה
    אותו והשרת דוחה את הבקשה (מצב 400). כל שאר השגיאות - מפתח שגוי, מכסה
    שנגמרה, תקלת רשת - חייבות לעלות למעלה ולא להיבלע כאן.
    """
    if isinstance(e, TypeError):
        return True
    if getattr(e, "status_code", None) == 400:
        return True
    return "BadRequest" in type(e).__name__


def _create(client: Anthropic, model: str, max_tokens: int, content: list):
    """קריאה למודל, עמידה להבדלי גרסאות של הספרייה.

    הצורה שנמצאה עובדת נשמרת, כך שהניסוי קורה פעם אחת בלבד בכל הפעלה.
    """
    msgs = [{"role": "user", "content": content}]
    idx = _STATE.get("call_variant")

    if idx is not None:
        return client.messages.create(model=model, max_tokens=max_tokens,
                                      messages=msgs, **_CALL_VARIANTS[idx])

    last_err = None
    for i, extra in enumerate(_CALL_VARIANTS):
        try:
            resp = client.messages.create(model=model, max_tokens=max_tokens,
                                          messages=msgs, **extra)
            _STATE["call_variant"] = i
            _STATE["call_variant_desc"] = (
                ", ".join(extra.keys()) if extra else "ברירת מחדל")
            return resp
        except Exception as e:
            if not _is_param_error(e):
                raise             # שגיאה אמיתית (רשת, מפתח, מכסה) – לא לבלוע
            last_err = e
            # שומרים את נוסח הדחייה. בלי זה אי אפשר לדעת למה פרמטר נדחה,
            # ונשארים עם ניחושים.
            _STATE.setdefault("call_rejections", []).append(
                f"{', '.join(extra.keys()) or 'ברירת מחדל'} → {str(e)[:220]}")
            continue
    raise last_err


def _text_of(resp) -> str:
    """מחלץ את הטקסט מהתשובה. כשאין בלוק טקסט כלל, מחזיר דיווח אבחוני
    במקום מחרוזת ריקה - תשובה ריקה בלי הסבר היא הדבר הכי קשה לאבחן."""
    text = "".join(getattr(b, "text", "") for b in resp.content
                   if getattr(b, "type", "") == "text")
    if text.strip():
        return text
    kinds = [getattr(b, "type", "?") for b in resp.content] or ["(אין בלוקים)"]
    stop = getattr(resp, "stop_reason", "?")
    u = getattr(resp, "usage", None)
    out = getattr(u, "output_tokens", "?") if u else "?"
    return (f"[אבחון] לא הוחזר טקסט. סוגי בלוקים: {kinds}. "
            f"סיבת עצירה: {stop}. טוקני פלט: {out}.")


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


def safe_filename(name: str) -> str:
    for ch in '\\/:*?"<>|':
        name = name.replace(ch, "-")
    return name.strip() or "מסמך"


# סוגי מסמכים שיש להם מנפיק מובהק, ושבלעדיו הקיבוץ מתפצל בטעות
_SOURCE_HINTS = ("תלוש", "שכר", "משכורת", "עו״ש", 'עו"ש', "עובר ושב",
                 "תנועות", "חשבון", "בנק", "יתרות", "משכנת")


def _is_hard_read(name: str, data: bytes, only_edges: bool) -> bool:
    """האם הקובץ חייב קריאה ויזואלית מלאה (שכבת טקסט שבורה או סרוק)."""
    if not name.lower().endswith(".pdf"):
        return False
    try:
        d = fitz.open(stream=data, filetype="pdf")
        verdict = _text_layer_verdict(d)
        d.close()
        return verdict == "broken"
    except Exception:
        return False


def _needs_source(a: dict) -> bool:
    """האם חסר שם המנפיק במסמך שבו הוא קריטי לקיבוץ."""
    if (a.get("source") or "").strip():
        return False
    dt = (a.get("doc_type") or "")
    return any(h in dt for h in _SOURCE_HINTS)