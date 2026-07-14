# -*- coding: utf-8 -*-
"""
כלי סיווג, קיבוץ ומתן-שמות למסמכים.
העובדת מעלה את כל הקבצים (PDF / תמונות), הכלי:
  1) קורא כל קובץ בנפרד (מעבר 1)
  2) מקבץ מסמכים שהם אותו דבר לוגי ונותן שם לפי הכללים (מעבר 2)
  3) ממזג כל קבוצה ל-PDF אחד עם השם הסופי
  4) מסמן בנפרד מה שלא בטוח ("לבדיקה")
ההעלאה למונדיי נשארת ידנית.
"""

import io
import os
import json
import base64
import zipfile

import fitz  # PyMuPDF
import streamlit as st
from anthropic import Anthropic

# ------------------------------------------------------------------ הגדרות בסיס
st.set_page_config(page_title="מיון וקיבוץ מסמכים", page_icon="🗂️", layout="wide")

# תמיכה בעברית / כיווניות RTL
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

IMAGE_MIME = {
    "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
    "webp": "image/webp", "gif": "image/gif",
}

# כללי מתן-שמות ברירת מחדל (ניתן לערוך במסך). מבוסס על טבלת "איך לקרוא לדוח".
DEFAULT_NAMING_RULES = """בנה שם קובץ (בלי סיומת) לפי סוג המסמך, בסדר: מילת-סוג + מזהים.
כלל: הוסף רכיב רק אם הוא מופיע במסמך או ברור. רכיב בסוגריים מרובעים [ ] הוסף
רק אם יש יותר מאחד מאותו סוג בין הקבצים שהועלו (כמה חשבונות/נכסים/מקומות עבודה).

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
    """מנקה גדרות ```json``` אם המודל הוסיף אותן."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.lstrip().lower().startswith("json"):
            text = text.lstrip()[4:]
    return text.strip().strip("`").strip()


def _file_block(name: str, data: bytes):
    """בונה בלוק תוכן ל-API לפי סוג הקובץ."""
    ext = name.rsplit(".", 1)[-1].lower()
    b64 = base64.standard_b64encode(data).decode("utf-8")
    if ext == "pdf":
        return {"type": "document",
                "source": {"type": "base64", "media_type": "application/pdf", "data": b64}}
    if ext in IMAGE_MIME:
        return {"type": "image",
                "source": {"type": "base64", "media_type": IMAGE_MIME[ext], "data": b64}}
    return None  # סוג לא נתמך


def analyze_one(client: Anthropic, model: str, name: str, data: bytes) -> dict:
    """מעבר 1 – קריאת קובץ בודד."""
    block = _file_block(name, data)
    if block is None:
        return {"doc_type": "אחר", "source": None, "date_start": None, "date_end": None,
                "period_label": None, "summary": f"סוג קובץ לא נתמך: {name}", "confidence": 0.0}

    prompt = (
        "זהו דף בודד מתוך מסמך שלקוח שלח למשרד ראיית חשבון/ייעוץ. "
        "קרא אותו והחזר JSON בלבד (בלי טקסט נוסף) בשדות הבאים:\n"
        '{"doc_type": "תלושי שכר|דפי חשבון|דוח יתרות|נסח טאבו|אישור קצבה|אחר", '
        '"source": "שם הבנק/המוסד או null", '
        '"date_start": "YYYY-MM-DD או null", "date_end": "YYYY-MM-DD או null", '
        '"period_label": "לדוגמה 04/2026 עבור תלוש, או null", '
        '"summary": "משפט קצר בעברית שמתאר מה רואים בדף", '
        '"confidence": מספר בין 0 ל-1}\n'
        "date_start/date_end = טווח התאריכים שמופיע בדף הזה בלבד. אם זה דף אחד מיום מסוים, שים אותו בשניהם."
    )
    resp = client.messages.create(
        model=model, max_tokens=700,
        messages=[{"role": "user", "content": [block, {"type": "text", "text": prompt}]}],
    )
    raw = "".join(b.text for b in resp.content if b.type == "text")
    try:
        return json.loads(_clean_json(raw))
    except Exception:
        return {"doc_type": "אחר", "source": None, "date_start": None, "date_end": None,
                "period_label": None, "summary": "לא ניתן לפענח את הדף", "confidence": 0.0}


def group_files(client: Anthropic, model: str, per_file: list, naming_rules: str,
                client_name: str = "", extra_ctx: str = "") -> list:
    """מעבר 2 – קיבוץ ומתן-שמות. מקבל את סיכומי מעבר 1 (טקסט בלבד)."""
    payload = [
        {"index": i, "filename": f["filename"], "doc_type": f["a"]["doc_type"],
         "source": f["a"]["source"], "date_start": f["a"]["date_start"],
         "date_end": f["a"]["date_end"], "period_label": f["a"]["period_label"],
         "summary": f["a"]["summary"], "confidence": f["a"]["confidence"]}
        for i, f in enumerate(per_file)
    ]
    ctx = ""
    if client_name:
        ctx += f'שם פרטי הלקוח (השתמש בו במקום לנחש): "{client_name}".\n'
    if extra_ctx:
        ctx += f"הקשר נוסף מהעובדת: {extra_ctx}\n"
    prompt = (
        "קיבלת רשימת קבצים שלקוח שלח. קבץ יחד קבצים שהם אותו מסמך לוגי "
        "(אותו סוג + אותו מקור + תאריכים רציפים/חופפים). לדוגמה 10 תמונות של אותם "
        "דפי עו״ש = קבוצה אחת; דוח יתרות נפרד = קבוצה אחרת.\n\n"
        f"{ctx}\n"
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
        # נפילה: כל קובץ לבד
        return [{"indices": [i], "doc_type": f["a"]["doc_type"],
                 "final_name": f["filename"].rsplit(".", 1)[0],
                 "confidence": 0.0, "note": "לא ניתן לקבץ אוטומטית"} for i, f in enumerate(per_file)]


def merge_to_pdf(files_bytes: list, names: list) -> bytes:
    """ממזג רשימת קבצים (PDF/תמונות) ל-PDF אחד."""
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
st.caption("העלי את כל הקבצים של לקוח. הכלי יקבץ אותם, ייתן שם לפי הכללים, וימזג כל קבוצה ל-PDF אחד להורדה.")

with st.sidebar:
    st.header("הגדרות")
    api_key = st.secrets.get("ANTHROPIC_API_KEY", "") if hasattr(st, "secrets") else ""
    if not api_key:
        api_key = st.text_input("Anthropic API Key", type="password")
    model = st.selectbox("מודל", ["claude-sonnet-5", "claude-opus-4-8"], index=0)
    threshold = st.slider("סף ביטחון (מתחתיו → 'לבדיקה')", 0.0, 1.0, 0.7, 0.05)
    st.divider()
    naming_rules = st.text_area("כללי מתן-שמות (ניתן לעריכה)", DEFAULT_NAMING_RULES, height=320)

uploaded = st.file_uploader(
    "גררי לכאן את כל הקבצים של הלקוח",
    type=["pdf", "jpg", "jpeg", "png", "webp", "gif"],
    accept_multiple_files=True,
)

if st.button("עבד קבצים", type="primary", disabled=not (uploaded and api_key)):
    client = Anthropic(api_key=api_key)
    files = [{"filename": f.name, "bytes": f.getvalue()} for f in uploaded]

    # מעבר 1
    per_file = []
    prog = st.progress(0.0, text="קורא קבצים...")
    for i, f in enumerate(files):
        a = analyze_one(client, model, f["filename"], f["bytes"])
        per_file.append({"filename": f["filename"], "bytes": f["bytes"], "a": a})
        prog.progress((i + 1) / len(files), text=f"נקרא: {f['filename']}")
    prog.progress(1.0, text="מקבץ ונותן שמות...")

    # מעבר 2
    groups = group_files(client, model, per_file, naming_rules)
    prog.empty()

    # בניית קבצים ממוזגים
    zip_buf = io.BytesIO()
    ok_groups, review_groups = [], []
    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for g in groups:
            idxs = g.get("indices", [])
            members = [per_file[i] for i in idxs if 0 <= i < len(per_file)]
            if not members:
                continue
            pdf = merge_to_pdf([m["bytes"] for m in members], [m["filename"] for m in members])
            fname = safe_filename(g.get("final_name", "מסמך")) + ".pdf"
            conf = float(g.get("confidence", 0) or 0)
            folder = "לבדיקה/" if conf < threshold else ""
            zf.writestr(folder + fname, pdf)
            entry = {"name": fname, "pdf": pdf, "members": members, "g": g, "conf": conf}
            (review_groups if conf < threshold else ok_groups).append(entry)

    st.success(f"עובדו {len(files)} קבצים → {len(ok_groups) + len(review_groups)} מסמכים.")
    st.download_button("⬇️ הורד הכל (ZIP)", zip_buf.getvalue(),
                       file_name="מסמכים_ממוינים.zip", mime="application/zip")

    def show(entry):
        cols = st.columns([3, 1])
        with cols[0]:
            st.markdown(f"**{entry['name']}**")
            src = ", ".join(m["filename"] for m in entry["members"])
            st.caption(f"מ-{len(entry['members'])} קבצים: {src}")
            if entry["g"].get("note"):
                st.caption(f"הערה: {entry['g']['note']}")
        with cols[1]:
            st.download_button("הורד", entry["pdf"], file_name=entry["name"],
                               mime="application/pdf", key=entry["name"])

    if ok_groups:
        st.subheader("✅ מוכן להעלאה")
        for e in ok_groups:
            show(e)
    if review_groups:
        st.subheader("⚠️ לבדיקה ידנית (ביטחון נמוך)")
        for e in review_groups:
            show(e)
