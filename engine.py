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

ENGINE_BUILD = "engine-2026-09-17-v38"

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


def _kw_hit(kw: str, hay: str) -> bool:
    """התאמת מילת מפתח לפי גבולות מילה ולא כתת-מחרוזת.

    בלי זה 'שומה' נמצא בתוך 'רשומה', ונסח טאבו שסיכומו מזכיר זכויות רשומות
    מסווג כשומת מס. תת-מחרוזת היא מקור שקט לסיווגים שגויים.
    """
    k = norm(kw)
    if not k:
        return False
    # גבול שמאלי מחמיר: מונע את הבאג שבו 'שומה' נמצא בתוך 'רשומה'.
    # גבול ימני מקל: מרשה עד שתי אותיות נוספות, כדי ש'תלוש' יתפוס גם
    # 'תלושים' ו'משכנת' גם 'משכנתא' - הטיות רגילות בעברית.
    return re.search(r"(?<![\w֐-׿])" + re.escape(k) +
                     r"(?![\w֐-׿]{3,})", hay) is not None


def classify(doc_type: str, summary: str = "") -> str:
    """ממפה תיאור חופשי מהמודל לקטגוריה סגורה. 'other' אם אין התאמה.

    doc_type מנצח תמיד: הוא מה שהמודל קבע שהמסמך הוא. הסיכום נבדק רק אם
    doc_type לא הכריע, כי הוא טקסט חופשי ועלול להזכיר מונחים שאינם הנושא.
    """
    for hay in (norm(doc_type), norm(summary)):
        if not hay:
            continue
        for cid, spec in CATEGORIES:
            if any(_kw_hit(k, hay) for k in spec["kw"]):
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
        # בקטגוריות שמכילות כמה מסמכים שונים במהותם - אישור זכויות, שטר
        # משכנתא, אישור ביצוע פעולה - אסור לאחד רק כי הקטגוריה משותפת.
        if cid in ("rights_confirm", "tabu", "appraisal", "benefits"):
            parts.append(norm(a.get("doc_type")))
        if cid in ("bank_statement", "bank_account_confirm", "bank_loans",
                   "bank_balances"):
            parts.append(str(a.get("account_last3") or ""))
    else:
        if cid == "bank_statement":
            parts.append(str(a.get("account_last3") or ""))
    return tuple(parts)


def _page_series(enriched: list) -> list:
    """מאתר סדרות עמודים: כמה קבצים שהם עמודים של אותו מסמך.

    למה זה קריטי: לקוחות שולחים דוח עו״ש כצילומים, עמוד-עמוד. רק העמוד
    הראשון נושא כותרת עם שם הלקוח, הבנק ומספר החשבון - וכל השאר טבלת
    תנועות בלבד. הקיבוץ הרגיל מזהה מסמך לפי בדיוק השדות האלה, ולכן פירק
    מסמך אחד לארבעה.

    המספור המודפס ('דף 3 מתוך 5') הוא הסימן החזק ביותר שיש, והוא נקרא
    אמין. כאן משתמשים בו.

    תנאי בטיחות: המספרים חייבים להיות ייחודיים. אם הועלו שני מסמכים שונים
    שבשניהם יש 'עמוד 1 מתוך 5', המספרים יתנגשו - ואז לא מאחדים, אלא
    נופלים חזרה לקיבוץ הרגיל. עדיף לא לאחד מאשר לאחד שני מסמכים זרים.
    """
    by_total = {}
    for e in enriched:
        pn, pt = e["a"].get("page_num"), e["a"].get("page_total")
        if isinstance(pn, int) and isinstance(pt, int) and pt > 1:
            by_total.setdefault(pt, []).append(e)

    series = []
    for total, members in by_total.items():
        if len(members) < 2:
            continue
        nums = [m["a"]["page_num"] for m in members]
        if len(set(nums)) != len(nums):
            continue                      # מספרים חוזרים - כנראה שני מסמכים
        if any(n < 1 or n > total for n in nums):
            continue
        # זהות תואמת: מי שיש לו שם חייב להסכים עם האחרים שיש להם שם
        peoples = [m["a"]["_people"] for m in members if m["a"]["_people"]]
        if peoples and not set.intersection(*(set(p) for p in peoples)):
            continue
        # במכוון אין כאן בדיקת מנפיק: עמוד אמצעי אינו נושא לוגו, והמודל
        # נוטה להשלים שם בנק מהניחוש. פסילה לפיו הייתה הורסת בדיוק את
        # הסדרות שהמנגנון הזה נועד לאחד. ההגנה היא מספור העמודים -
        # מספרים חוזרים כבר נפסלו למעלה.
        members.sort(key=lambda m: m["a"]["page_num"])
        series.append(members)
    return series


def _balances_chain(a: dict, b: dict) -> bool:
    """האם יתרת הסגירה של עמוד אחד שווה ליתרת הפתיחה של הבא."""
    e, s = a.get("balance_end"), b.get("balance_start")
    if e is None or s is None:
        return False
    try:
        return abs(float(e) - float(s)) < 0.01
    except (TypeError, ValueError):
        return False


def _absorb_orphans(series: list, enriched: list, taken: set) -> None:
    """מצרף לסדרה קובץ שלא נקרא לו מספור, כשברור לאיזו סדרה הוא שייך.

    לקוח מצלם עמוד-עמוד, ולעתים המספור בעמוד אחד מטושטש. אם בסדרה חסר
    בדיוק מספר אחד, ונשאר בדיוק קובץ יתום אחד מאותה קטגוריה - הוא העמוד
    החסר. אם יש יותר מאפשרות אחת, לא מנחשים.
    """
    orphans = [e for e in enriched
               if e["index"] not in taken
               and not isinstance(e["a"].get("page_num"), int)]
    for members in series:
        total = members[0]["a"]["page_total"]
        have = {m["a"]["page_num"] for m in members}
        missing = [n for n in range(1, total + 1) if n not in have]
        if len(missing) != 1:
            continue
        cat = _series_category(members)
        fits = [o for o in orphans
                if o["index"] not in taken
                and o["a"]["_cat"] in (cat, "other")]
        if len(fits) != 1:
            continue                     # יותר מאפשרות אחת - לא מנחשים
        o = fits[0]
        o["a"]["page_num"] = missing[0]
        o["a"]["page_total"] = total
        members.append(o)
        taken.add(o["index"])
        members.sort(key=lambda m: m["a"]["page_num"])


def _series_category(members: list) -> str:
    """קטגוריית הסדרה: לפי העמוד הראשון, שהוא היחיד שנושא כותרת מלאה.

    עמוד אמצעי או אחרון מסווג לעתים אחרת - עמוד אחרון של דוח תנועות נראה
    כאישור בנקאי - ולכן העמוד הראשון קובע.
    """
    for m in members:
        if m["a"]["_cat"] != "other":
            return m["a"]["_cat"]
    return "other"


def _adopt_series_identity(members: list) -> None:
    """בסדרת עמודים, הזהות נלקחת מהעמוד הראשון שיש לו אותה.

    עמוד אמצעי של דוח תנועות אינו נושא לוגו או כותרת, והמודל נוטה להשלים
    שם בנק מהניחוש. כאן מוחקים את מה שהומצא ומאמצים את מה שנקרא מהעמוד
    שבאמת מכיל את הכותרת.
    """
    for field in ("person_name", "source", "branch", "account_last3"):
        val = next((m["a"].get(field) for m in members if m["a"].get(field)), None)
        if val is None:
            continue
        for m in members:
            m["a"][field] = val
    people = next((m["a"]["_people"] for m in members if m["a"]["_people"]),
                  frozenset())
    for m in members:
        m["a"]["_people"] = people


def build_groups(per_file: list) -> list:
    """מקבץ קבצים למסמכים לוגיים. פונקציה טהורה - אין בה קריאה למודל."""
    enriched = []
    for i, f in enumerate(per_file):
        a = dict(f.get("a") or {})
        a["_cat"] = classify(a.get("doc_type"), a.get("summary"))
        a["_people"] = people_in(a.get("person_name"))
        enriched.append({"index": i, "filename": f.get("filename", ""),
                         "a": a, "file_pages": f.get("file_pages", 1)})

    # קודם סדרות עמודים. מה שנקלט בהן יוצא מהמסלול הרגיל.
    groups, taken = [], set()
    series = _page_series(enriched)
    for members in series:
        for m in members:
            taken.add(m["index"])
    _absorb_orphans(series, enriched, taken)
    for members in series:
        cat = _series_category(members)
        _adopt_series_identity(members)
        for m in members:
            m["a"]["_cat"] = cat          # כל עמודי הסדרה מקבלים קטגוריה אחת
        groups.append({"key": ("series", cat, members[0]["index"]),
                       "cat": cat, "members": members})

    buckets = {}
    for e in enriched:
        if e["index"] in taken:
            continue
        # קובץ בלי קטגוריה מזוהה נשאר תמיד בודד: אין כלל שמתיר לאחד אותו,
        # ולכן הוא ילך לבדיקה ידנית עם השם שחולץ ממנו.
        key = (("other", e["index"]) if e["a"]["_cat"] == "other"
               else group_key(e["a"]))
        buckets.setdefault(key, []).append(e)

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


# פער קצר בין סוף עמוד לתחילת הבא הוא המשך טבעי, לא חוסר. דוחות רבים
# מסתיימים ביום אחד ומתחילים למחרת.
GAP_TOLERANCE_DAYS = 3


def _days_between(a: str, b: str) -> int:
    try:
        return abs((date.fromisoformat(b) - date.fromisoformat(a)).days)
    except Exception:
        return 0


def find_gaps(group: dict) -> list:
    """מאתר חוסרים במסמך תקופתי. מחזיר רשימת תיאורים בעברית.

    שלוש בדיקות, מהחזקה לחלשה:
      1. מספור עמודים - 'דף 3 מתוך 4' שלא הגיע
      2. רצף יתרות - יתרת הסגירה של עמוד אינה יתרת הפתיחה של הבא
      3. רצף תאריכים - פער בין סוף עמוד לתחילת הבא

    הבחנה חשובה: פער בתאריכים לבדו אינו בהכרח חוסר. ייתכן שלא היו תנועות
    באותה תקופה. אבל אם גם היתרות אינן מתחברות - חסר דף.
    """
    members = group["members"]
    if len(members) < 1 or group["cat"] not in ("bank_statement",
                                                "mortgage_history"):
        return []

    gaps = []
    total = next((m["a"].get("page_total") for m in members
                  if isinstance(m["a"].get("page_total"), int)), None)
    have = {m["a"].get("page_num") for m in members
            if isinstance(m["a"].get("page_num"), int)}
    if total and have:
        missing = [n for n in range(1, total + 1) if n not in have]
        if missing:
            gaps.append("חסרים עמודים " +
                        ", ".join(str(n) for n in missing) + f" מתוך {total}")

    ordered = sorted(members, key=lambda m: (
        m["a"].get("page_num") if isinstance(m["a"].get("page_num"), int) else 99,
        str(m["a"].get("date_start") or "")))
    for a, b in zip(ordered, ordered[1:]):
        da, db = a["a"], b["a"]
        end, start = str(da.get("date_end") or ""), str(db.get("date_start") or "")
        if not (len(end) == 10 and len(start) == 10) or start <= end:
            continue
        if _days_between(end, start) <= GAP_TOLERANCE_DAYS:
            continue                # המשך טבעי בין עמודים, לא חוסר
        if _balances_chain(da, db):
            continue                # היתרות מתחברות - פשוט לא היו תנועות
        # מדווחים על היתרות רק כששתיהן באמת נקראו. עמוד אמצעי מצולם לא
        # תמיד מוסר אותן, ואי-קריאה אינה ראיה לחוסר.
        both = da.get("balance_end") is not None and db.get("balance_start") is not None
        gaps.append(f"פער בתאריכים בין {end} ל-{start}" +
                    (" והיתרות אינן מתחברות" if both else ""))
    return gaps


def group_confidence(group: dict) -> float:
    """ביטחון הזיהוי של הקבוצה כולה.

    לא המינימום: קריאה חלשה אחת מתוך חמש גררה קבוצה נכונה לגמרי לבדיקה
    ידנית. כשכמה קבצים נקראו בנפרד והגיעו לאותו סיווג, ההסכמה ביניהם
    מחזקת ולא מחלישה. לכן חציון, שעמיד לחריג בודד.
    """
    members = group["members"]
    vals = sorted(float(m["a"].get("confidence") or 0) for m in members)
    if not vals:
        return 0.0

    # סדרת עמודים שלמה: הביטחון נקבע לפי העמוד החזק ביותר, לרוב עמוד
    # הכותרת. עמוד אמצעי של דוח תנועות אינו נושא מידע מזהה ולכן מדווח
    # ביטחון נמוך - אבל המספור הרצוף מוכיח שהוא חלק מאותו מסמך, וזו
    # ראיה חזקה יותר מהביטחון של כל עמוד בנפרד.
    nums = [m["a"].get("page_num") for m in members]
    total = next((m["a"].get("page_total") for m in members
                  if isinstance(m["a"].get("page_total"), int)), None)
    if (total and len(members) == total
            and all(isinstance(n, int) for n in nums)
            and sorted(nums) == list(range(1, total + 1))):
        return max(vals)

    n = len(vals)
    return vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2


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