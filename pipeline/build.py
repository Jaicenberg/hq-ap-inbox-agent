"""Step 4: merge AI results + rule cross-check + self-checks into the tables the agent queries.

Writes <out.sqlite> (same schema as D1) and <out.sql> (import file for `wrangler d1 execute`).
Usage: python -I build.py <raw.sqlite> <work.sqlite> <out.sqlite> <out.sql>
"""
import hashlib
import json
import os
import re
import sqlite3
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from classify import ZW, clean  # noqa: E402

LEVELS = {"none": 0, "reminder": 1, "escalation": 2, "final_notice": 3}
TYPES = ("invoice", "statement", "payment_reminder", "remittance", "other")

# ---------- independent keyword rules (cross-check for the model, never the final answer) ----------
RULES = [
    ("remittance", r"remittance|payment (receipt|confirmation|advice)|thank you for (your )?(online )?payment|"
                   r"payment (was |has been )?(received|processed|scheduled)|scheduling your online payment|receipt"),
    ("payment_reminder", r"final notice|past due|overdue|payment reminder|reminder:|now due|credit hold|"
                         r"collections|disconnect|unpaid|payment request|account is due|outstanding (balance|invoices?)"),
    ("statement", r"\bstatement\b|aging|open items|outstanding invoice report"),
    ("invoice", r"invoice|\binv[\s#_-]*\d|bill is ready|new bill|sales order|\bbill\b"),
]
CHASE_RULES = [("final_notice", r"final notice|disconnect|legal action|collection agency|suspend"),
               ("escalation", r"past due|overdue|credit hold|second notice|collections"),
               ("reminder", r"reminder|now due|due soon|payment request|account is due")]


def rule_type(text):
    t = (text or "").lower()
    return next((label for label, rx in RULES if re.search(rx, t)), "other")


def rule_chase(text):
    t = (text or "").lower()
    return next((label for label, rx in CHASE_RULES if re.search(rx, t)), "none")


# ---------- normalisation ----------
SUFFIX = re.compile(r"\b(inc|llc|l\.l\.c|ltd|co|corp|corporation|company|incorporated|lp|llp|the|usa)\b\.?", re.I)


def vendor_key(name):
    if not name:
        return None
    n = name.lower()
    if " dba " in n or " d/b/a " in n or " d.b.a. " in n:
        n = re.split(r" d/?\.?b/?\.?a\.? ", n)[-1]
    n = SUFFIX.sub(" ", n)
    n = re.sub(r"[^a-z0-9]+", " ", n).strip()
    return n or None


def invoice_key(ref):
    """'INV-000123' / '#123' / 'Invoice 123' -> '123'; 'SO-164145' -> 'SO164145'."""
    if ref is None:
        return None
    r = re.sub(r"(?i)^(invoice|inv|no|number|#)[\s#.:_-]*", "", str(ref).strip())
    r = re.sub(r"[^A-Za-z0-9]", "", r).upper()
    r = re.sub(r"^0+(?=\d)", "", r)
    return r if len(r) >= 3 else None


GENERIC_DOMAINS = {"gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "icloud.com", "aol.com", "eaze.com"}


def canonical_vendors(emails, invoices):
    """Map vendor keys that are the same company onto one key.
    1. Name prefix: 'leafly' absorbs 'leafly holdings' (shorter key must be >= 4 chars).
    2. Sender domain: keys whose inbound emails come from the same vendor domain merge, unless the
       domain is a platform used by many vendors (QuickBooks, Bill.com...), detected by key count."""
    count = Counter(r["vendor_key"] for r in emails + invoices if r["vendor_key"])
    parent = {k: k for k in count}

    def find(k):
        while parent[k] != k:
            k = parent[k]
        return k

    def union(a, b):  # keep the most frequent spelling as the root
        ra, rb = find(a), find(b)
        if ra != rb:
            keep, drop = (ra, rb) if count[ra] >= count[rb] else (rb, ra)
            parent[drop] = keep

    keys = sorted(count, key=len)
    for i, short in enumerate(keys):
        if len(short) < 4:
            continue
        for long_ in keys[i + 1:]:
            if long_.startswith(short + " "):
                union(short, long_)
    by_domain = defaultdict(Counter)
    for e in emails:
        dom = e["from_email"].split("@")[-1] if "@" in e["from_email"] else None
        if e["vendor_key"] and e["direction"] == "inbound" and dom and dom not in GENERIC_DOMAINS:
            by_domain[dom][e["vendor_key"]] += 1
    for dom, ks in by_domain.items():
        main = [k for k, n in ks.items() if n >= 2]
        if 2 <= len(main) <= 3:  # more than 3 distinct vendors on one domain = a billing platform
            for k in main[1:]:
                union(main[0], k)
    return {k: find(k) for k in count}


def num(v):
    if v is None or v == "":
        return None
    try:
        return float(str(v).replace(",", "").replace("$", "").strip())
    except ValueError:
        return None


def date_ok(d):
    return bool(d) and bool(re.fullmatch(r"20(2[4-7])-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])", str(d)))


def grounded_number(ref, text):
    """Does the extracted invoice number literally appear in the source document?"""
    if not ref or not text:
        return False
    flat = re.sub(r"[^A-Za-z0-9]", "", text).upper()
    k = re.sub(r"[^A-Za-z0-9]", "", str(ref)).upper()
    return len(k) >= 3 and k in flat


def grounded_amount(v, text):
    if v is None or not text:
        return False
    flat = text.replace(",", "").replace("$", "")
    return any(s in flat for s in {f"{v:.2f}", f"{v:,.2f}".replace(",", ""), str(int(v)) if v == int(v) else "@@"})


# ---------- invoice self-checks ----------
def invoice_checks(f, text, source):
    checks, problems = {}, []
    total = num(f.get("total")) if num(f.get("total")) is not None else num(f.get("amount_due"))
    checks["has_number"] = bool(f.get("invoice_number"))
    checks["has_vendor"] = bool(f.get("vendor"))
    checks["has_total"] = total is not None
    checks["number_in_source"] = grounded_number(f.get("invoice_number"), text)
    checks["total_in_source"] = grounded_amount(total, text)
    checks["dates_valid"] = all(date_ok(f.get(k)) for k in ("invoice_date", "due_date") if f.get(k))
    sub, tax = num(f.get("subtotal")), num(f.get("tax"))
    if sub is not None and tax is not None and total is not None:
        checks["arithmetic"] = abs(sub + tax - total) <= max(0.02, 0.005 * total)
    if f.get("invoice_date") and f.get("due_date") and checks["dates_valid"]:
        checks["due_after_issue"] = f["due_date"] >= f["invoice_date"]
    labels = {"has_number": "no invoice number", "has_vendor": "no vendor", "has_total": "no total",
              "number_in_source": "invoice number not found verbatim in the document",
              "total_in_source": "total not found verbatim in the document",
              "dates_valid": "invalid date", "arithmetic": "subtotal + tax != total",
              "due_after_issue": "due date before invoice date"}
    for k, ok in checks.items():
        if not ok and not (source == "vision" and k in ("number_in_source", "total_in_source")):
            problems.append(labels[k])
    if source == "vision":
        problems.append("read from a scanned image (page 1 only): verify against the PDF")
    return checks, problems


def main(raw_db, work_db, out_db, out_sql):
    raw, work = sqlite3.connect(raw_db), sqlite3.connect(work_db, timeout=120)
    if os.path.exists(out_db):
        os.remove(out_db)
    out = sqlite3.connect(out_db)
    out.executescript(open(os.path.join(os.path.dirname(__file__), "schema.sql"), encoding="utf-8").read())

    llm_email = {i: (json.loads(r) if r else None, e) for i, r, e in
                 work.execute("SELECT id, result, error FROM llm_email")}
    llm_doc = {k: (json.loads(r) if r else None, e, s) for k, r, e, s in
               work.execute("SELECT key, result, error, source FROM llm_doc")}
    cost_batch = sum(c or 0 for (c,) in work.execute("SELECT cost FROM llm_email UNION ALL SELECT cost FROM llm_doc"))

    # ---------------- emails ----------------
    emails = {}
    for (mid, conv, ev, fname, fhandle, subj, at, body, atts) in raw.execute(
            """SELECT id, conversation_id, event_type, recipient_from_name, recipient_from_handle, subject,
                      message_created_at, body_text, attachments FROM frontapp_messages"""):
        res, err = llm_email.get(mid, (None, "not classified (batch did not reach this email)"))
        names = " ".join(a["filename"] for a in json.loads(atts or "[]"))
        rtype = rule_type(f"{subj} {names} {clean(body, 1500)}")
        problems = []
        if res is None:
            problems.append(f"AI could not classify: {err}")
            dtype, level, vendor, conf = rtype, rule_chase(f"{subj} {clean(body, 1500)}"), None, 0.0
            refs, amount = [], None
        else:
            dtype = res.get("type") if res.get("type") in TYPES else "other"
            level = res.get("chase_level") if res.get("chase_level") in LEVELS else "none"
            if dtype != "payment_reminder":
                level = "none"
            vendor = res.get("vendor")
            if vendor and vendor.strip().lower() in ("eaze", "null", "none", "unknown", "n/a"):
                vendor = None
            conf = num(res.get("confidence")) or 0.0
            refs = [str(r) for r in (res.get("chased_invoices") or []) if r][:40]
            amount = num(res.get("amount_due"))
            if conf < 0.7:
                problems.append(f"low model confidence ({conf:.2f})")
            if rtype != dtype and {rtype, dtype} != {"invoice", "other"}:
                problems.append(f"keyword rules say '{rtype}', AI says '{dtype}'")
        emails[mid] = dict(id=mid, conversation_id=conv, direction=ev.lower(), sent_at=at, from_name=fname,
                           from_email=(fhandle or "").lower(), subject=ZW.sub("", subj or ""),
                           body=clean(body, 3000), doc_type=dtype, chase_level=level, vendor=vendor,
                           vendor_key=vendor_key(vendor), chased_invoices=json.dumps(refs), amount_due=amount,
                           confidence=conf, rule_type=rtype, needs_review=int(bool(problems)),
                           review_reason="; ".join(problems) or None,
                           attachment_count=sum(1 for a in json.loads(atts or "[]") if not a["metadata"].get("is_inline")))

    # ---------------- attachments + invoices ----------------
    attachments, invoices, listed = [], [], defaultdict(list)
    for (fid, mid, name, ct, size, inline, status, note, text) in work.execute(
            "SELECT id, message_id, filename, content_type, size, is_inline, read_status, read_note, text FROM attachment_text"):
        key = hashlib.sha1(text.encode()).hexdigest() if status == "ok" else "scan:" + fid
        res, err, source = llm_doc.get(key, (None, None, None))
        method = {"ok": "pdftotext/local", "needs_ocr": "vision model (page 1)"}.get(status, "none")
        problems, dtype, conf = [], None, None
        rtype = rule_type(f"{name} {text[:1500]}")
        if inline:
            dtype = "other"  # signature logos / embedded images
        elif status in ("missing", "password", "error", "unsupported"):
            problems.append(f"cannot read file: {note}")
            dtype = "unknown"
        elif res is None:
            problems.append(f"AI could not classify: {err or 'not processed'}")
            dtype = rtype if status == "ok" else "unknown"
        else:
            dtype = res.get("type") if res.get("type") in TYPES else "other"
            conf = num(res.get("confidence")) or 0.0
            if source == "vision" and res.get("page_text"):
                text = res["page_text"]
                status = "ok_vision"
            if conf < 0.7:
                problems.append(f"low model confidence ({conf:.2f})")
            if status == "ok" and rtype != dtype and {rtype, dtype} != {"invoice", "other"}:
                problems.append(f"keyword rules say '{rtype}', AI says '{dtype}'")
            for ref in (res.get("listed_invoices") or [])[:40]:
                listed[mid].append((str(ref), fid))
            if dtype == "invoice":
                checks, inv_problems = invoice_checks(res, text, source)
                total = num(res.get("total")) if num(res.get("total")) is not None else num(res.get("amount_due"))
                invoices.append(dict(
                    attachment_id=fid, email_id=mid, vendor=res.get("vendor"), vendor_key=vendor_key(res.get("vendor")),
                    invoice_number=res.get("invoice_number"), invoice_key=invoice_key(res.get("invoice_number")),
                    invoice_date=res.get("invoice_date") if date_ok(res.get("invoice_date")) else None,
                    due_date=res.get("due_date") if date_ok(res.get("due_date")) else None,
                    po_number=res.get("po_number"), payment_terms=res.get("payment_terms"),
                    currency=res.get("currency") or "USD", subtotal=num(res.get("subtotal")), tax=num(res.get("tax")),
                    total=total, amount_due=num(res.get("amount_due")), bill_to=res.get("bill_to"),
                    received_at=emails[mid]["sent_at"], read_method=method, confidence=conf,
                    checks=json.dumps(checks), needs_review=int(bool(inv_problems) or conf < 0.7),
                    review_reason="; ".join(inv_problems) or None, is_primary=1, duplicate_count=0))
        attachments.append(dict(id=fid, email_id=mid, filename=name, content_type=ct, size=size, is_inline=inline,
                                read_status=status, read_method=method, read_note=note, doc_type=dtype,
                                confidence=conf, rule_type=rtype, needs_review=int(bool(problems)),
                                review_reason="; ".join(problems) or None, text=(text or "")[:4000]))

    # One vendor, many spellings ("Leafly" / "Leafly Holdings, Inc"): merge keys before counting anything.
    canon = canonical_vendors(list(emails.values()), invoices)
    for row in list(emails.values()) + invoices:
        row["vendor_key"] = canon.get(row["vendor_key"], row["vendor_key"])

    # The same invoice arrives many times (forwards, re-sends): keep the first as primary.
    groups = defaultdict(list)
    for inv in invoices:
        k = (inv["vendor_key"], inv["invoice_key"]) if inv["invoice_key"] else ("file", inv["attachment_id"])
        groups[k].append(inv)
    for g in groups.values():
        g.sort(key=lambda i: i["received_at"])
        for dup in g[1:]:
            dup["is_primary"] = 0
        g[0]["duplicate_count"] = len(g) - 1

    # ---------------- chases: what vendors chase, and is it in the inbox? ----------------
    by_key = defaultdict(list)
    for inv in invoices:
        if inv["invoice_key"] and inv["is_primary"]:
            by_key[inv["invoice_key"]].append(inv)
    all_text = {a["id"]: (a["email_id"], re.sub(r"[^A-Za-z0-9]", "", a["text"]).upper()) for a in attachments if a["text"]}
    subjects = {e["id"]: re.sub(r"[^A-Za-z0-9]", "", e["subject"] + e["body"]).upper() for e in emails.values()}
    chases = []
    for e in emails.values():
        # Only real payment reminders count as chasing (a plain statement is not a chase).
        if e["doc_type"] != "payment_reminder":
            continue
        refs = [(r, "email") for r in json.loads(e["chased_invoices"])] + [(r, "attachment:" + f) for r, f in listed.get(e["id"], [])]
        seen = set()
        if not refs:
            refs = [(None, "email")]
        for ref, src in refs:
            k = invoice_key(ref)
            if ref and (k in seen or k is None):
                continue
            seen.add(k)
            match = None
            if k:
                cands = by_key.get(k, [])
                match = next((c for c in cands if c["vendor_key"] == e["vendor_key"]), cands[0] if cands else None)
            mentioned = False
            if k and not match:
                # "Elsewhere" = outside this chasing email and its own attachments (its statement lists it anyway).
                mentioned = (any(k in t for mid, t in all_text.values() if mid != e["id"])
                             or any(k in s for i, s in subjects.items() if i != e["id"]))
            status = ("no invoice number given" if not k else "invoice in inbox" if match else
                      "mentioned elsewhere, no invoice document" if mentioned else "not in inbox")
            chases.append(dict(email_id=e["id"], vendor=e["vendor"], vendor_key=e["vendor_key"], level=e["chase_level"],
                               level_rank=LEVELS[e["chase_level"]], sent_at=e["sent_at"], subject=e["subject"],
                               invoice_ref=ref, invoice_key=k, status=status,
                               matched_attachment_id=match["attachment_id"] if match else None,
                               matched_total=match["total"] if match else None, source=src))

    # ---------------- vendors ----------------
    vend = defaultdict(lambda: dict(names=Counter(), domains=Counter(), emails=0, invoices=0, billed=0.0,
                                    chase_emails=set(), max_level=0, first=None, last=None, refs=set(), found=set()))
    for e in emails.values():
        if e["vendor_key"]:
            v = vend[e["vendor_key"]]
            v["names"][e["vendor"]] += 1
            v["emails"] += 1
            if e["direction"] == "inbound" and "@" in e["from_email"]:
                v["domains"][e["from_email"].split("@")[1]] += 1
    for inv in invoices:
        if inv["vendor_key"] and inv["is_primary"]:
            v = vend[inv["vendor_key"]]
            v["names"][inv["vendor"]] += 1
            v["invoices"] += 1
            v["billed"] += inv["total"] or 0
    for c in chases:
        if c["vendor_key"]:
            v = vend[c["vendor_key"]]
            v["chase_emails"].add(c["email_id"])
            v["max_level"] = max(v["max_level"], c["level_rank"])
            v["first"] = min(filter(None, [v["first"], c["sent_at"]]))
            v["last"] = max(filter(None, [v["last"], c["sent_at"]]))
            if c["invoice_key"]:
                v["refs"].add(c["invoice_key"])
                if c["status"] == "invoice in inbox":
                    v["found"].add(c["invoice_key"])
    inv_level = {r: l for l, r in LEVELS.items()}
    vendors = [dict(vendor_key=k, name=v["names"].most_common(1)[0][0], domains=",".join(d for d, _ in v["domains"].most_common(3)),
                    emails=v["emails"], invoices=v["invoices"], total_billed=round(v["billed"], 2),
                    chase_emails=len(v["chase_emails"]), max_chase_level=inv_level[v["max_level"]],
                    first_chase=v["first"], last_chase=v["last"], chased_invoices=len(v["refs"]),
                    chased_found=len(v["found"])) for k, v in vend.items()]

    # ---------------- quality / accuracy report ----------------
    q = {}
    n_att = [a for a in attachments if not a["is_inline"]]
    st = Counter(a["read_status"] for a in n_att)
    q["files_referenced"] = (len(n_att), "non-inline attachments referenced by emails")
    q["files_read_text_layer"] = (st["ok"], "read locally with pdftotext / office readers")
    q["files_read_vision"] = (st["ok_vision"], "scanned PDFs/images read by the vision model (page 1)")
    q["files_missing"] = (st["missing"], "referenced in an email but not in attachments.zip")
    q["files_password_protected"] = (st["password"], "PDF needs a password")
    q["files_unreadable"] = (st["error"] + st["unsupported"] + st["needs_ocr"], "corrupt, unsupported, or vision step failed")
    q["inline_images_missing"] = (sum(1 for a in attachments if a["is_inline"] and a["read_status"] == "missing"),
                                  "signature logos etc. (not business documents)")
    em = list(emails.values())
    q["emails_classified_by_ai"] = (sum(1 for e in em if llm_email.get(e["id"], (None,))[0]), f"of {len(em)} emails")
    agree = [e for e in em if llm_email.get(e["id"], (None,))[0]]
    q["email_rule_agreement"] = (f"{sum(1 for e in agree if e['rule_type'] == e['doc_type']) / max(1, len(agree)):.1%}",
                                 "AI type == independent keyword-rule type (disagreements go to review)")
    att_ai = [a for a in n_att if a["confidence"] is not None and a["read_status"] == "ok"]
    q["attachment_rule_agreement"] = (f"{sum(1 for a in att_ai if a['rule_type'] == a['doc_type']) / max(1, len(att_ai)):.1%}",
                                      "AI type == keyword-rule type, text-layer documents")
    prim = [i for i in invoices if i["is_primary"]]
    q["invoices_unique"] = (len(prim), f"{len(invoices)} invoice files, {len(invoices) - len(prim)} are repeats")
    for k, label in [("number_in_source", "invoice number found verbatim in the document"),
                     ("total_in_source", "total found verbatim in the document"),
                     ("arithmetic", "subtotal + tax = total (where all three exist)")]:
        vals = [json.loads(i["checks"]).get(k) for i in prim if i["read_method"].startswith("pdftotext")]
        vals = [v for v in vals if v is not None]
        q[f"check_{k}"] = (f"{sum(vals) / max(1, len(vals)):.1%}", f"{label}; n={len(vals)} text-layer invoices")
    q["invoices_needing_review"] = (sum(i["needs_review"] for i in prim), "failed at least one self-check")
    q["emails_needing_review"] = (sum(e["needs_review"] for e in em), "low confidence or rules/AI disagree")
    if work.execute("SELECT 1 FROM sqlite_master WHERE name='judge_metrics'").fetchone():
        from judge import wilson
        for m, k, n in work.execute("SELECT metric, k, n FROM judge_metrics"):
            p, lo, hi = wilson(k, n)
            q[m] = (f"{p:.1%}", f"{k}/{n} agree with a stronger model (GLM-5.3, reasoning on) labelling blind; 95% CI {lo:.0%}-{hi:.0%}")
    q["batch_ai_cost_usd"] = (f"{cost_batch:.2f}", "Workers AI, one-off processing of the whole inbox")
    out.executemany("INSERT INTO quality VALUES (?,?,?)", [(k, str(v), d) for k, (v, d) in q.items()])

    def insert(table, rows):
        if rows:
            cols = list(rows[0])
            out.executemany(f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                            [tuple(r[c] for c in cols) for r in rows])

    insert("emails", list(emails.values()))
    insert("attachments", attachments)
    insert("invoices", invoices)
    insert("chases", chases)
    insert("vendors", vendors)
    out.commit()
    for k, (v, d) in q.items():
        print(f"{k:32} {v:>10}  {d}")

    # D1 import file: plain INSERTs (no BEGIN/COMMIT: D1 rejects explicit transactions).
    with open(out_sql, "w", encoding="utf-8") as f:
        f.write(open(os.path.join(os.path.dirname(__file__), "schema.sql"), encoding="utf-8").read() + "\n")
        for line in out.iterdump():
            if line.startswith("INSERT INTO"):
                f.write(line + "\n")
    print("wrote", out_sql)


if __name__ == "__main__":
    main(*sys.argv[1:5])
