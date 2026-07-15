# -*- coding: utf-8 -*-
"""
כלי סיווג, קיבוץ ומתן-שמות למסמכים.
העובדת מעלה את כל הקבצים (PDF / תמונות), הכלי:
  1) קורא כל קובץ עם הדגם הזול; אם לא בטוח – משדרג לדגם המדויק (מעבר 1)
  2) מקבץ מסמכים שהם אותו דבר לוגי ונותן שם לפי הכללים (מעבר 2)
  3) ממזג כל קבוצה ל-PDF אחד עם השם הסופי
  4) מסמן בנפרד מה שלא בטוח ("לבדיקה")
הכלי מזהה את שם הלקוח בעצמו מתוך המסמכים – אין צורך להקליד כלום.
ההעלאה למונדיי נשארת ידנית.
"""

import io
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

CHEAP_MODEL = "claude-haiku-4-5-20251001"   # דגם זול לקריאה
PRECISE_MODEL = "claude-sonnet-5"           # דגם מדויק לשדרוג ולקיבוץ

IMAGE_MIME = {
    "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
    "webp": "image/webp", "gif": "image/gif",
}

FIELDS = {"doc_type": "אחר", "source": None, "date_start": None, "date_end": None,
          "period_label": None, "account_last3": None, "property_address": None,
          "person_name": None, "business_name": None, "summary": "", "confidence": 0.0}

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


def _file_block(name: str, data: bytes):
    ext = name.rsplit(".", 1)[-1].lower()
    b64 = base64.standard_b64encode(data).decode("utf-8")
    if ext == "pdf":
        return {"type": "document",
                "source": {"type": "base64", "media_type": "application/pdf", "data": b64}}
    if ext in IMAGE_MIME:
        return {"type": "image",
                "source": {"type": "base64", "media_type": IMAGE_MIME[ext], "data": b64}}
    return None


def analyze_one(client: Anthropic, model: str, name: str, data: bytes) -> dict:
    """מעבר 1 – קריאת קובץ בודד. מחזיר תמיד dict עם כל השדות."""
    result = dict(FIELDS)
    block = _file_block(name, data)
    if block is None:
        result["summary"] = f"סוג קובץ לא נתמך: {name}"
        return result
    prompt = (
        "זהו מסמך שלקוח שלח למשרד ייעוץ פיננסי/ראיית חשבון. קרא אותו והחזר JSON בלבד "
        "(בלי טקסט נוסף) בשדות:\n"
        '{"doc_type": "תיאור קצר בעברית של סוג המסמך (למשל: תנועות עו״ש, נסח טאבו, תלוש שכר)", '
        '"source": "שם בנק/מוסד/מעסיק או null", '
        '"date_start": "YYYY-MM-DD או null", "date_end": "YYYY-MM-DD או null", '
        '"period_label": "חודשים/שנה, למשל 04-06/2026 או 2025, או null", '
        '"account_last3": "3 ספרות אחרונות של מספר החשבון או null", '
        '"property_address": "כתובת נכס אם רלוונטי או null", '
        '"person_name": "השם הפרטי של האדם/הלקוח שהמסמך שייך לו, או null", '
        '"business_name": "שם עסק/חברה אם רלוונטי או null", '
        '"summary": "משפט קצר בעברית", '
        '"confidence": מספר בין 0 ל-1}\n'
        "date_start/date_end = טווח התאריכים שבמסמך. אם יום בודד – שים אותו בשניהם."
    )
    resp = client.messages.create(
        model=model, max_tokens=700,
        messages=[{"role": "user", "content": [block, {"type": "text", "text": prompt}]}],
    )
    raw = "".join(b.text for b in resp.content if b.type == "text")
    try:
        parsed = json.loads(_clean_json(raw))
        for k in FIELDS:
            if k in parsed and parsed[k] is not None:
                result[k] = parsed[k]
    except Exception:
        result["summary"] = "לא ניתן לפענח את המסמך"
    return result


def group_files(client: Anthropic, model: str, per_file: list, naming_rules: str) -> list:
    """מעבר 2 – קיבוץ ומתן-שמות (טקסט בלבד). השם מזוהה מתוך המסמכים."""
    payload = [
        {"index": i, "filename": f["filename"], **{k: f["a"].get(k) for k in FIELDS}}
        for i, f in enumerate(per_file)
    ]
    prompt = (
        "קיבלת רשימת קבצים. קבץ יחד קבצים שהם אותו מסמך לוגי "
        "(אותו סוג + אותו מקור + אותו אדם + תאריכים רציפים/חופפים). לדוגמה 10 תמונות של אותם "
        "דפי עו״ש = קבוצה אחת; דוח יתרות נפרד = קבוצה אחרת.\n"
        "חוק ברזל: לעולם אל תשים באותה קבוצה מסמכים של שני אנשים שונים (person_name שונה). "
        "בכל קבוצה השתמש בשם האדם של אותה קבוצה בלבד – אל תיקח שם מקבוצה אחרת.\n"
        "אם המסמך ברור וזוהה היטב – תן confidence גבוה (0.8-1). "
        "הורד confidence רק אם באמת לא ברור מה המסמך או של מי הוא.\n\n"
        f"{naming_rules}\n\n"
        "החזר JSON בלבד במבנה:\n"
        '{"groups": [{"indices": [0,1], "doc_type": "...", "final_name": "שם לפי הכללים", '
        '"confidence": מספר בין 0 ל-1, "note": "הערה קצרה אם יש ספק"}]}\n'
        "כל קובץ חייב להופיע בקבוצה אחת בדיוק. סדר את ה-indices בכל קבוצה לפי התאריך.\n\n"
        "הקבצים:\n" + json.dumps(payload, ensure_ascii=False, indent=2)
    )
    resp = client.messages.create(
        model=model, max_tokens=1500,
        messages=[{"role": "user", "content": [{"type": "text", "text": prompt}]}],
    )
    raw = "".join(b.text for b in resp.content if b.type == "text")
    try:
        return json.loads(_clean_json(raw)).get("groups", [])
    except Exception:
        return [{"indices": [i], "doc_type": f["a"].get("doc_type", "אחר"),
                 "final_name": f["filename"].rsplit(".", 1)[0],
                 "confidence": 0.0, "note": "לא ניתן לקבץ אוטומטית"} for i, f in enumerate(per_file)]


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

    only_edges = st.checkbox("ב-PDF לקרוא רק עמוד ראשון + אחרון (חוסך)", value=True)
    threshold = st.slider("סף ביטחון (מתחתיו: שדרוג לסונט, ואם עדיין נמוך – 'לבדיקה')",
                          0.0, 1.0, 0.7, 0.05)
    st.divider()
    naming_rules = st.text_area("כללי מתן-שמות (ניתן לעריכה)", DEFAULT_NAMING_RULES, height=300)

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


def analyze_cached(client, model, name, data, only_edges):
    """קורא קובץ, אבל אם כבר נקרא בעבר – מחזיר מהזיכרון בלי לשלם שוב."""
    rb = reading_bytes(name, data, only_edges)
    key = hashlib.sha256(rb).hexdigest() + "|" + model
    if key in st.session_state["cache"]:
        return st.session_state["cache"][key], True
    a = analyze_one(client, model, name, rb)
    st.session_state["cache"][key] = a
    return a, False


def sort_key(m):
    """מיון בתוך קבוצה: לפי תאריך, ואם אין – לפי שם הקובץ."""
    d = m["a"].get("date_start") or m["a"].get("date_end") or ""
    return (d == "", d, m["filename"])


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
            a, hit = analyze_cached(client, CHEAP_MODEL, f["filename"], f["bytes"], only_edges)
            used = "זול"
            if float(a.get("confidence") or 0) < threshold:
                a, hit2 = analyze_cached(client, PRECISE_MODEL, f["filename"], f["bytes"], only_edges)
                used, hit = "שודרג לסונט", hit and hit2
        else:
            a, hit = analyze_cached(client, PRECISE_MODEL, f["filename"], f["bytes"], only_edges)
            used = "סונט"
        from_cache += 1 if hit else 0
        per_file.append({"filename": f["filename"], "bytes": f["bytes"], "a": a, "used": used})
        prog.progress((i + 1) / len(files), text=f"נקרא: {f['filename']}")
    prog.progress(1.0, text="מקבץ ונותן שמות...")

    groups = group_files(client, PRECISE_MODEL, per_file, naming_rules)
    prog.empty()

    # בניית קבצים ממוזגים
    zip_buf = io.BytesIO()
    ok_groups, review_groups = [], []
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

            folder = "לבדיקה/" if conf < threshold else ""
            zf.writestr(folder + fname, pdf)
            entry = {"name": fname, "pdf": pdf, "conf": conf, "note": g.get("note", ""),
                     "sources": [m["filename"] for m in members], "key": f"g{gi}"}
            (review_groups if conf < threshold else ok_groups).append(entry)

    st.session_state["results"] = {
        "zip": zip_buf.getvalue(), "ok": ok_groups, "review": review_groups,
        "table": [{"קובץ": m["filename"], "זוהה כ": m["a"].get("doc_type"),
                   "שם שזוהה": m["a"].get("person_name"),
                   "ביטחון": round(float(m["a"].get("confidence") or 0), 2),
                   "נקרא ב": m["used"]} for m in per_file],
        "n_files": len(files), "from_cache": from_cache,
        "upgraded": sum(1 for m in per_file if m["used"] == "שודרג לסונט"),
        "economical": economical,
    }

# --------------------------------------------------- הצגת תוצאות (נשמרות גם אחרי הורדה)
R = st.session_state.get("results")
if R:
    st.success(f"עובדו {R['n_files']} קבצים → {len(R['ok']) + len(R['review'])} מסמכים.")
    if R["from_cache"]:
        st.caption(f"({R['from_cache']} קבצים נלקחו מהזיכרון – לא שולם עליהם שוב)")
    st.download_button("⬇️ הורד הכל (ZIP)", R["zip"], file_name="מסמכים_ממוינים.zip",
                       mime="application/zip", key="zip_all")

    if R["economical"]:
        st.info(f"קריאה: {R['n_files'] - R['upgraded']} קבצים הסתדרו עם הדגם הזול, "
                f"{R['upgraded']} שודרגו לסונט.")
    st.dataframe(R["table"], use_container_width=True, hide_index=True)

    def show(entry):
        cols = st.columns([3, 1])
        with cols[0]:
            st.markdown(f"**{entry['name']}**")
            st.caption(f"מ-{len(entry['sources'])} קבצים: {', '.join(entry['sources'])}")
            if entry["note"]:
                st.caption(f"הערה: {entry['note']}")
        with cols[1]:
            st.download_button("הורד", entry["pdf"], file_name=entry["name"],
                               mime="application/pdf", key=entry["key"])

    if R["ok"]:
        st.subheader("✅ מוכן להעלאה")
        for e in R["ok"]:
            show(e)
    if R["review"]:
        st.subheader("⚠️ לבדיקה ידנית (ביטחון נמוך)")
        for e in R["review"]:
            show(e)