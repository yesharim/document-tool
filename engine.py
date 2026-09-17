"""מנוע קיבוץ, שיוך ומתן-שמות — בקוד, בלי מודל.

למה בקוד ולא במודל:
    כל עוד ההחלטות האלה נעשו בהנחיה למודל, הן היו תלויות בשיקול דעת שלו בין
    כללים שמתחרים זה בזה. אותם קבצים בדיוק יצאו פעם נכון ופעם לא, וכל כלל
    חדש שנוסף שבר כלל קודם. כאן אין שיקול דעת: יש כללים, והם רצים.

מה המודל עדיין עושה:
    קורא קובץ בודד ומדווח מה כתוב בו - סוג, אדם, מנפיק, תאריכים. בזה הוא
    מצוין, וזה לא משתנה.

הכללים שנקבעו עם הלקוח:
    - תלושים של אותו אדם ומאותו מעסיק -> קובץ אחד, גם על פני כמה חודשים
    - תלושים ממעסיקים שונים -> קבצים נפרדים
    - אבל כולם משויכים לאותו סאב-אייטם של תלושי שכר
    - מה שאין לו יעד ברור -> בדיקה ידנית עם שם נכון, בלי ניחוש
"""

import re
import unicodedata
from datetime import date

# ------------------------------------------------------------------ נרמול

_QUOTES = str.maketrans({"\u05F4": '"', "\u05F3": "'", "״": '"', "׳": "'"})
_NOISE = ("בע\"מ", "בעמ", "לישראל", "בערבון מוגבל", "חברה לביטוח",
          "אגודה שיתופית", "אגש\"ח", "בע''מ")


def norm(s) -> str:
    """מנרמל טקסט להשוואה: בלי גרשיים, בלי רווחים כפולים, בלי ניקוד."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", str(s)).translate(_QUOTES)
    s = re.sub(r"[\u0591-\u05C7]", "", s)          # ניקוד וטעמים
    s = re.sub(r"[^\w\s\"'/.-]", " ", s)
    return re.sub(r"\s+", " ", s).strip().lower()


def norm_org(s) -> str:
    """נרמול שם מוסד: מסיר צורות התאגדות ותוספות שאינן מזהות.

    בלי זה 'קווים תחבורה ציבורית בע\"מ' ו'קווים תחבורה ציבורית אגש\"ח' נראים
    כשני מעסיקים, והתלושים מתפצלים.
    """
    s = norm(s)
    for w in (norm(x) for x in _NOISE):
        s = s.replace(w, " ")
    return re.sub(r"\s+", " ", s).strip()


# ספקי תוכנת שכר. מופיעים על תלוש אבל אינם המעסיק.
PAYROLL_VENDORS = {"מלם שכר", "מלם", "חילן", "חילן טק", "מיכפל", "סינריון",
                   "עוקץ מערכות", "עוקץ", "אורורה", "הרגון", "ניסן"}


# תיאורי מוצר, לא שם המשלם. מופיעים על תלושי קצבה ואינם מזהים גוף.
GENERIC_SOURCES = {"קרן פנסיה", "קרן הפנסיה", "פנסיה מקיפה", "קרן פנסיה מקיפה",
                   "קרן הפנסיה מקיפה", "פנסיה", "קצבה", "קרן השתלמות",
                   "ביטוח מנהלים", "תלוש שכר", "שכר"}


def _fuzzy(a: str, b: str) -> float:
    from difflib import SequenceMatcher
    return SequenceMatcher(None, a, b).ratio()


def is_payroll_vendor(s) -> bool:
    """ספק תוכנת שכר, כולל שגיאות כתיב קלות שהמודל מייצר ('מלם שכה')."""
    n = norm_org(s)
    if not n:
        return False
    for v in PAYROLL_VENDORS:
        nv = norm_org(v)
        if n == nv or nv in n or _fuzzy(n, nv) >= 0.8:
            return True
    return False


def is_generic_source(s) -> bool:
    n = norm_org(s)
    if not n:
        return False
    return any(_fuzzy(n, norm_org(g)) >= 0.8 or norm_org(g) == n
               for g in GENERIC_SOURCES)


def norm_person(s) -> str:
    """נרמול שם אדם, כולל סידור מילים.

    'זאודה אנדרגה' ו'אנדרגה זאודה' הם אותו אדם. בלי מיון המילים הם נראים
    כשני אנשים והמסמכים שלהם מתפצלים.
    """
    s = norm(s)
    if not s:
        return ""
    return " ".join(sorted(w for w in s.split() if len(w) > 1))


def people_in(s) -> frozenset:
    """מחזיר את קבוצת האנשים שבשדה. חשבון משותף מכיל שניים."""
    if not s:
        return frozenset()
    parts = re.split(r"[,;]|\s+ו\s+|\s+and\s+", str(s))
    out = {norm_person(p) for p in parts if norm_person(p)}
    return frozenset(out)


# ------------------------------------------------------------------ קטגוריות

# כל קטגוריה: מילות זיהוי, האם היא סדרה תקופתית, ותבנית השם.
# הסדר חשוב - הקטגוריה הראשונה שמתאימה מנצחת, ולכן הספציפיות קודמות.
CATEGORIES = [
    ("mortgage_payoff", dict(
        kw=["יתרה לסילוק", "יתרות לסילוק", "סילוק משכנת", "פירעון מוקדם",
            "פרעון מוקדם", "יתרת סילוק"],
        periodic=False, template="יתרה לסילוק {source} {address}")),
    ("mortgage_history", dict(
        kw=["התנהלות משכנת", "דוח משכנת", "פירוט משכנת", "תנועות משכנת"],
        periodic=False, template="התנהלות משכנתה {source} {address}")),
    ("payslip", dict(
        kw=["תלוש שכר", "תלוש משכורת", "תלוש קצבה", "תלוש פנסיה", "תלוש",
            "פירוט תשלומים וניכויים", "פנסיה מוקדמת"],
        periodic=True, template="תלושי שכר {months} {person} {source}")),
    ("bank_statement", dict(
        kw=["תנועות עו\"ש", "תנועות עוש", "עובר ושב", "תנועות אחרונות",
            "דוח תנועות", "פעולות אחרונות", "תנועות חשבון"],
        periodic=True, template="תנועות עוש {dates} {source} {person} {acct}")),
    ("bank_account_confirm", dict(
        kw=["אישור ניהול חשבון", "אישור על ניהול חשבון", "ניהול חשבון"],
        periodic=False, template="אישור ניהול חשבון {source} {person} {acct}")),
    ("bank_loans", dict(
        kw=["פירוט הלוואות", "יתרות הלוואה", "ריכוז הלוואות"],
        periodic=False, template="פירוט הלוואות {source} {person} {acct}")),
    ("bank_balances", dict(
        kw=["ריכוז יתרות", "שערוך יתרות", "פירוט תיק לקוח"],
        periodic=False, template="ריכוז יתרות {source} {person} {acct}")),
    ("cpa_income", dict(
        kw=["אישור רו\"ח", "אישור רואה חשבון", "רואה חשבון", "רו\"ח",
            "הכנסות והוצאות", "דוח הכנסות"],
        periodic=False, template="אישור רוח {year} {business}")),
    ("tax_assessment", dict(
        kw=["שומת מס", "שומה", "הודעה על שומת"],
        periodic=False, template="שומת מס {year}")),
    ("vat_cert", dict(
        kw=["עוסק מורשה", "עוסק פטור", "תעודת עוסק"],
        periodic=False, template="עוסק מורשה {person}")),
    ("appraisal", dict(
        kw=["שמאות", "חוות דעת שמאי", "שומת מקרקעין", "הערכת שווי"],
        periodic=False, template="שמאות {address}")),
    ("rights_confirm", dict(
        kw=["אישור זכויות", "אישור רישום", "שטר משכנת", "אישור ביצוע פעולה",
            "אישור ביצוע פעולת", "חברה משכנת", "עמידר", "עמיגור", "אישור בעלות"],
        periodic=False, template="אישור זכויות {source} {address}")),
    ("tabu", dict(
        kw=["נסח טאבו", "נסח רישום", "פנקס בתים משותפים", "נסח"],
        periodic=False, template="נסח טאבו {address}")),
    ("benefits", dict(
        kw=["קצבאות", "קצבה", "ביטוח לאומי", "נכות", "זכאות לקצב"],
        periodic=False, template="אישור על תשלומי קצבאות {person}")),
    ("credit_report", dict(
        kw=["נתוני אשראי", "דנא"],
        periodic=False, template="דנא {person}")),
    ("id_card", dict(
        kw=["תעודת זהות", "ספח", "ת.ז"],
        periodic=False, template="ת.ז {person}")),
]

CAT_BY_ID = dict(CATEGORIES)


def classify(doc_type: str, summary: str = "") -> str:
    """ממפה תיאור חופשי מהמודל לקטגוריה סגורה. 'other' אם אין התאמה."""
    hay = norm(doc_type) + " " + norm(summary)
    for cid, spec in CATEGORIES:
        for k in spec["kw"]:
            if norm(k) in hay:
                return cid
    return "other"


# ------------------------------------------------------------------ קיבוץ

def _months(a: dict) -> set:
    """קבוצת החודשים שהמסמך מכסה, כמספרים YYYYMM."""
    out = set()
    for key in ("date_start", "date_end"):
        m = re.search(r"(\d{4})[-/.](\d{1,2})", str(a.get(key) or ""))
        if m:
            out.add(int(m.group(1)) * 100 + int(m.group(2)))
    lab = str(a.get("period_label") or "")
    for mm, yy in re.findall(r"(\d{1,2})[./-](\d{4})", lab):
        out.add(int(yy) * 100 + int(mm))
    return out


def _year(a: dict) -> str:
    """שנת המסמך, לקטגוריות שנתיות."""
    for key in ("period_label", "date_end", "date_start"):
        m = re.search(r"(20\d{2})", str(a.get(key) or ""))
        if m:
            return m.group(1)
    return ""


_ORG_STOP = {"בית", "חולים", "חברה", "משרד", "בנק", "סניף", "קבוצת",
             "שירותי", "מרכז", "רשות", "עיריית", "מועצה", "תחבורה",
             "ציבורית", "ל", "של", "ה"}


def org_tokens(s) -> set:
    return {w for w in norm_org(s).split() if len(w) > 2 and w not in _ORG_STOP}


def sources_compatible(a, b) -> bool:
    """האם שני שמות מנפיק הם אותו גוף.

    שם מוסד נכתב אחרת בין מסמך למסמך: 'הפניקס' מול 'הפניקס חברה לביטוח',
    'קווים בע"מ' מול 'קווים אגש"ח'. השוואת מחרוזות מדויקת מפצלת מסמך אחד
    לכמה, ולכן משווים לפי מילים מזהות משותפות.
    """
    na, nb = norm_org(a), norm_org(b)
    if not na or not nb:
        return True                 # אחד מהם לא זוהה - לא פוסלים
    if na == nb or na in nb or nb in na:
        return True
    ta, tb = org_tokens(a), org_tokens(b)
    if not ta or not tb:
        return True
    return bool(ta & tb)


def _source_of(a: dict) -> str:
    """המנפיק לצורך קיבוץ.

    חברת סליקת שכר אינה מנפיק, וגם לא תיאור מוצר כמו 'קרן פנסיה מקיפה' -
    שניהם מופיעים על תלושים במקום שם המשלם, ואם נתייחס אליהם כמנפיק,
    תלושים של אותו אדם יתפצלו לכמה קבצים.
    """
    s = a.get("source")
    return "" if (is_payroll_vendor(s) or is_generic_source(s)) else (s or "")


def split_by_source(members: list) -> list:
    """מפצל קבוצה לתתי-קבוצות לפי מנפיק, בסבלנות לניסוח שונה.

    חבר מצטרף לתת-קבוצה קיימת אם המנפיק שלו תואם את זה של אחד מחבריה.
    """
    clusters = []
    for m in members:
        src = _source_of(m["a"])
        for c in clusters:
            if any(sources_compatible(src, _source_of(x["a"])) for x in c):
                c.append(m)
                break
        else:
            clusters.append([m])
    return clusters


def group_key(a: dict) -> tuple:
    """המפתח שקובע אילו קבצים הם אותו מסמך לוגי.

    הכלל שנקבע: אותו אדם + אותו סוג + אותו מנפיק = מסמך אחד.
    בקטגוריה תקופתית (תלושים, תנועות) החודש אינו חלק מהמפתח, ולכן חודשים
    עוקבים מתאחדים. בקטגוריה שנתית השנה כן חלק מהמפתח, ולכן 2025 ו-2026
    נשארים נפרדים.
    """
    cid = a["_cat"]
    spec = CAT_BY_ID.get(cid, {})
    people = a["_people"]
    parts = [cid, "|".join(sorted(people))]
    if not spec.get("periodic", False):
        parts.append(_year(a))
        if cid in ("bank_statement", "bank_account_confirm", "bank_loans",
                   "bank_balances"):
            parts.append(str(a.get("account_last3") or ""))
    else:
        if cid == "bank_statement":
            parts.append(str(a.get("account_last3") or ""))
    return tuple(parts)


def build_groups(per_file: list) -> list:
    """מקבץ קבצים למסמכים לוגיים. פונקציה טהורה - אין בה קריאה למודל."""
    enriched = []
    for i, f in enumerate(per_file):
        a = dict(f.get("a") or {})
        a["_cat"] = classify(a.get("doc_type"), a.get("summary"))
        a["_people"] = people_in(a.get("person_name"))
        enriched.append({"index": i, "filename": f.get("filename", ""),
                         "a": a, "file_pages": f.get("file_pages", 1)})

    buckets = {}
    for e in enriched:
        # קובץ בלי קטגוריה מזוהה נשאר תמיד בודד: אין כלל שמתיר לאחד אותו,
        # ולכן הוא ילך לבדיקה ידנית עם השם שחולץ ממנו.
        key = (("other", e["index"]) if e["a"]["_cat"] == "other"
               else group_key(e["a"]))
        buckets.setdefault(key, []).append(e)

    groups = []
    for key, members in buckets.items():
        for cluster in split_by_source(members):
            cluster.sort(key=lambda m: (min(_months(m["a"]) or {0}), m["index"]))
            groups.append({"key": key, "cat": cluster[0]["a"]["_cat"],
                           "members": cluster})
    groups.sort(key=lambda g: (g["cat"], g["members"][0]["index"]))
    return groups


# ------------------------------------------------------------------ מתן שמות

_BANK_SHORT = [("הבינלאומי", "הבינלאומי"), ("מזרחי", "מזרחי טפחות"),
               ("טפחות", "מזרחי טפחות"), ("דיסקונט", "דיסקונט"),
               ("הפועלים", "הפועלים"), ("לאומי", "לאומי"), ("יהב", "יהב"),
               ("מרכנתיל", "מרכנתיל"), ("ירושלים", "ירושלים"),
               ("איגוד", "איגוד"), ("אוצר החייל", "אוצר החייל"),
               ("מסד", "מסד"), ("פאגי", "פאגי"), ("דואר", "בנק הדואר")]


def short_bank(s) -> str:
    n = norm_org(s)
    for needle, nice in _BANK_SHORT:
        if norm(needle) in n:
            return nice
    return (s or "").strip()


def _fmt_months(groups_months: set) -> str:
    """טווח חודשים לשם קובץ: 04-07. חודש בודד -> 07."""
    if not groups_months:
        return ""
    lo, hi = min(groups_months), max(groups_months)
    a, b = f"{lo % 100:02d}", f"{hi % 100:02d}"
    return a if a == b else f"{a}-{b}"


def _fmt_dates(members: list) -> str:
    """טווח תאריכים לדוח תנועות: 01.06-20.08."""
    dates = []
    for m in members:
        for key in ("date_start", "date_end"):
            g = re.search(r"(\d{4})-(\d{2})-(\d{2})", str(m["a"].get(key) or ""))
            if g:
                dates.append(g.groups())
    if not dates:
        return ""
    lo, hi = min(dates), max(dates)
    if lo == hi:
        return ""          # טווח של יום אחד בדוח תקופתי - כמעט תמיד שגוי
    return f"{lo[2]}.{lo[1]}-{hi[2]}.{hi[1]}"


def _people_label(members: list) -> str:
    """שמות בעלי המסמך, בסדר שבו הופיעו במקור."""
    seen, out = set(), []
    for m in members:
        raw = str(m["a"].get("person_name") or "")
        for p in re.split(r"[,;]|\s+ו\s+", raw):
            p = p.strip()
            if p and norm_person(p) not in seen:
                seen.add(norm_person(p))
                out.append(p)
    return " ו".join(out)


def _acct_label(members: list) -> str:
    br = next((str(m["a"].get("branch")).strip() for m in members
               if m["a"].get("branch")), "")
    ac = next((str(m["a"].get("account_last3")).strip() for m in members
               if m["a"].get("account_last3")), "")
    if br and ac:
        return f"[סניף {br} · {ac}]"
    if br:
        return f"[סניף {br}]"
    if ac:
        return f"[{ac}]"
    return ""


def build_name(group: dict) -> str:
    """מרכיב את שם הקובץ מהתבנית של הקטגוריה. הדבקת מחרוזות, לא שיקול דעת."""
    members = group["members"]
    cat = group["cat"]
    spec = CAT_BY_ID.get(cat)
    first = members[0]["a"]

    if not spec:
        base = (first.get("doc_type") or "מסמך").strip()
        who = _people_label(members)
        when = _year(first)
        return " ".join(x for x in (base, who, when) if x)

    months = set()
    for m in members:
        months |= _months(m["a"])

    # לשם הקובץ בוחרים את המנפיק האמיתי הראשון שנמצא בקבוצה, ולא את זה של
    # הקובץ הראשון: הוא עלול להיות חברת סליקה או תיאור מוצר.
    src = next((m["a"].get("source") for m in members
                if _source_of(m["a"]).strip()), "")
    if cat in ("bank_statement", "bank_account_confirm", "bank_loans",
               "bank_balances", "mortgage_payoff", "mortgage_history"):
        src = short_bank(src)

    vals = {
        "months": _fmt_months(months),
        "dates": _fmt_dates(members),
        "person": _people_label(members),
        "source": (src or "").strip(),
        "acct": _acct_label(members),
        "year": _year(first),
        "business": (first.get("business_name") or "").strip(),
        "address": (first.get("property_address") or "").strip(),
    }
    name = spec["template"].format(**vals)
    return re.sub(r"\s+", " ", name).strip()


# ------------------------------------------------------------------ שיוך

def parse_subitem(text: str) -> dict:
    """מפרק שם סאב-אייטם למרכיביו: קטגוריה, בנק, אנשים, ורמז שנה."""
    raw = text.strip()
    body, owners = raw, ""
    m = re.search(r"\(([^)]*)\)\s*$", body)
    if m:
        owners, body = m.group(1), body[:m.start()].strip()

    tail = ""
    for dash in ("—", "–", " - "):
        if dash in body:
            head, tail = body.rsplit(dash, 1)
            body, tail = head.strip(), tail.strip()
            break

    bank = tail if ("בנק" in tail or short_bank(tail) != tail.strip()) else ""
    person = "" if bank else tail

    people = people_in(owners) | people_in(person)
    if norm(owners) == norm("משותף"):
        people = people_in(person)

    year_hint = ""
    if "שנה נוכחית" in raw:
        year_hint = "current"
    elif "שנה קודמת" in raw:
        year_hint = "prev"
    elif "שנתיים אחורה" in raw:
        year_hint = "prev2"

    return {"raw": raw, "cat": classify(body), "bank": bank,
            "people": people, "year_hint": year_hint,
            "joint": norm(owners) == norm("משותף")}


def _year_matches(hint: str, year: str) -> bool:
    if not hint or not year:
        return True
    cur = date.today().year
    want = {"current": cur, "prev": cur - 1, "prev2": cur - 2}.get(hint)
    return str(want) == str(year)


def assign(groups: list, case_docs: list) -> None:
    """משייך כל קבוצה לסאב-אייטם. כותב target ו-target_reason לכל קבוצה.

    ניקוד מפורש ולא שיקול דעת: קטגוריה חייבת להתאים, ואחריה בנק, אנשים
    ושנה מוסיפים ודאות. בלי התאמת קטגוריה אין שיוך בכלל.
    """
    parsed = [parse_subitem(d) for d in case_docs]

    for g in groups:
        first = g["members"][0]["a"]
        gsrc = short_bank(first.get("source") or "")
        gpeople = set()
        for m in g["members"]:
            gpeople |= m["a"]["_people"]
        gyear = _year(first)

        cands = []
        for p in parsed:
            if p["cat"] != g["cat"] or p["cat"] == "other":
                continue
            # התאמת קטגוריה לבדה היא כבר שיוך תקף; כל אימות נוסף מחזק.
            score, notes = 0.7, []

            if p["bank"]:
                if norm_org(short_bank(p["bank"])) == norm_org(gsrc) and gsrc:
                    score += 0.1
                    notes.append("בנק תואם")
                elif gsrc:
                    continue          # בנק אחר - פסול, לא רק פחות מתאים
            if p["people"]:
                if gpeople & p["people"]:
                    score += 0.1
                    notes.append("שם תואם")
                elif gpeople and not p["joint"]:
                    continue          # אדם אחר - פסול
            if p["year_hint"]:
                if _year_matches(p["year_hint"], gyear):
                    score += 0.1
                    notes.append("שנה תואמת")
                else:
                    continue
            cands.append((score, p, ", ".join(notes)))

        if not cands:
            g["target"], g["target_conf"], g["target_reason"] = None, 0.0, ""
            continue

        cands.sort(key=lambda c: -c[0])
        score, best, why = cands[0]

        # שני מועמדים באותו ניקוד = הרשימה עצמה אינה מבחינה ביניהם. מורידים
        # ודאות כדי שהקובץ יעבור לבדיקה ידנית במקום להיות משויך בהגרלה.
        if len(cands) > 1 and abs(cands[1][0] - score) < 1e-9:
            score -= 0.25
            why = (why + ", יש יותר מפריט מתאים").strip(", ")

        g["target"] = best["raw"]
        g["target_reason"] = why
        g["target_conf"] = round(min(score, 0.99), 2)