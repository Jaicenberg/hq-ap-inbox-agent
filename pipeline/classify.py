"""Step 3: classify every email and attachment with Workers AI, and pull invoice fields.

- Emails: 8 per call (short excerpts) -> type, vendor, chase level, invoices being chased.
- Attachments with text: 1 per call, deduplicated by text hash (forwards repeat the same PDF).
- Scanned PDFs / images: page 1 rendered to JPEG -> same prompt, but read by the vision model.

Results are cached in <work.sqlite> so the step can be stopped and resumed.
Usage: python -I classify.py <raw.sqlite> <work.sqlite> <attachments_dir> [emails|attachments|scans|all] [limit]
"""
import base64
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ai  # noqa: E402

MODEL = os.environ.get("BATCH_MODEL", "@cf/google/gemma-4-26b-a4b-it")
WORKERS = int(os.environ.get("BATCH_CONCURRENCY", "16"))
# "i/n": only process documents whose hash falls in shard i of n (lets two models share the work).
SHARD = tuple(int(x) for x in os.environ.get("SHARD", "0/1").split("/"))

TYPES = """Document types (pick exactly one):
- invoice: a bill requesting payment for goods/services (incl. "your bill is ready", sales invoice, invoice+manifest).
- statement: an account statement / aging / open-items report listing several invoices and a balance.
- payment_reminder: the vendor asks to be paid for something ALREADY billed (due soon, now due, overdue, past due, credit hold, final notice, collections).
- remittance: proof that a payment was made or scheduled (remittance advice, payment receipt, "thank you for your payment", ACH/payment confirmation).
- other: anything else (delivery/shipping notice without a bill, purchase order, COA, marketing, internal chat, account admin, credit memo).
Boilerplate like "friendly reminder to have payment ready for any overdue orders" inside a delivery email does NOT make it a payment_reminder."""

EMAIL_PROMPT = f"""You classify emails in the accounts-payable inbox of Eaze (a cannabis delivery company; Eaze is the CUSTOMER who pays vendors).
{TYPES}
For payment_reminder also set chase_level:
- reminder: polite reminder, payment due soon / now due, first follow-up.
- escalation: past due / overdue follow-ups, repeated chasing, credit hold, deliveries or service at risk, collections team.
- final_notice: explicitly "final notice", threat of disconnection, suspension, legal action, or referral to a collections agency.
Otherwise chase_level is "none".
vendor = the company that issued the bill / is chasing (NOT Eaze, NOT a platform like Front or Gmail). For forwarded emails use the original sender's company.
chased_invoices = invoice/document numbers the email says are unpaid (exact strings as written), else [].
confidence = your probability (0-1) that `type` is correct.
Return JSON: {{"results":[{{"id":..., "type":..., "chase_level":..., "vendor":..., "chased_invoices":[...], "amount_due":number|null, "confidence":number, "reason":"<12 words"}}]}} with one entry per email, same ids."""

DOC_PROMPT = f"""You read one attachment from the accounts-payable inbox of Eaze (Eaze is the CUSTOMER who pays vendors).
{TYPES}
Extract fields exactly as printed (do not invent; use null when absent). Dates as YYYY-MM-DD. Amounts as plain numbers.
- vendor: company that issued the document (the seller / payee, NOT Eaze or its stores).
- invoice_number, invoice_date, due_date, po_number, payment_terms, currency (ISO code), subtotal, tax, total, amount_due.
- bill_to: the customer / store billed.
- listed_invoices: for statements and reminders, the invoice numbers listed as open/unpaid (max 30), else [].
- confidence: your probability (0-1) that `type` is correct and the fields are right.
Return JSON: {{"type":..., "vendor":..., "invoice_number":..., "invoice_date":..., "due_date":..., "po_number":..., "payment_terms":..., "currency":..., "subtotal":..., "tax":..., "total":..., "amount_due":..., "bill_to":..., "listed_invoices":[...], "confidence":..., "reason":"<12 words"}}"""

ZW = re.compile(r"[​-‏  ͏­﻿]+")


def clean(text, n):
    text = ZW.sub("", text or "")
    return re.sub(r"\s+", " ", text).strip()[:n]


def doc_excerpt(text, head=3500, tail=1200):
    """Invoices put the number/dates at the top and totals at the bottom: keep both ends."""
    text = ZW.sub("", text or "")
    text = re.sub(r"[ \t]{3,}", "  ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text if len(text) <= head + tail else text[:head] + "\n[...]\n" + text[-tail:]


def setup(db):
    db.executescript("""
    CREATE TABLE IF NOT EXISTS llm_email (id TEXT PRIMARY KEY, result TEXT, model TEXT, cost REAL, error TEXT);
    CREATE TABLE IF NOT EXISTS llm_doc (key TEXT PRIMARY KEY, result TEXT, model TEXT, cost REAL, error TEXT, source TEXT);
    """)


lock = threading.Lock()


def save(db, table, row):
    with lock:
        db.execute(f"INSERT OR REPLACE INTO {table} VALUES ({','.join('?' * len(row))})", row)
        db.commit()


def run_emails(raw, db, limit):
    done = {r[0] for r in db.execute("SELECT id FROM llm_email WHERE error IS NULL")}
    rows = raw.execute("""SELECT id, event_type, recipient_from_name, recipient_from_handle, subject,
                          message_created_at, body_text, attachments FROM frontapp_messages
                          ORDER BY message_created_at""").fetchall()
    todo = [r for r in rows if r[0] not in done
            and int(hashlib.sha1(r[0].encode()).hexdigest()[:8], 16) % SHARD[1] == SHARD[0]][:limit]
    batches = [todo[i:i + 8] for i in range(0, len(todo), 8)]
    print(f"emails: {len(todo)} to classify in {len(batches)} calls")

    def work(batch):
        items = []
        for mid, ev, fname, fhandle, subj, at, body, atts in batch:
            names = [a["filename"] for a in json.loads(atts or "[]") if not a["metadata"].get("is_inline")]
            items.append({"id": mid, "direction": ev, "from": f"{fname} <{fhandle}>", "date": at[:10],
                          "subject": clean(subj, 200), "attachments": names[:10], "body": clean(body, 1400)})
        msgs = [{"role": "system", "content": EMAIL_PROMPT},
                {"role": "user", "content": json.dumps(items, ensure_ascii=False)}]
        try:
            text, usage = ai.chat(MODEL, msgs, max_tokens=2500)
            results = {r["id"]: r for r in ai.parse_json(text)["results"]}
            c = ai.cost(MODEL, usage) / len(batch)
            for it in items:
                r = results.get(it["id"])
                save(db, "llm_email", (it["id"], json.dumps(r) if r else None, MODEL, c,
                                       None if r else "model returned no result for this email"))
        except Exception as e:
            for it in items:
                save(db, "llm_email", (it["id"], None, MODEL, 0, str(e)[:300]))

    with ThreadPoolExecutor(WORKERS) as pool:
        for i, _ in enumerate(as_completed([pool.submit(work, b) for b in batches])):
            if i % 50 == 0:
                print(f"  emails {i}/{len(batches)} calls")


def run_docs(db, limit):
    done = {r[0] for r in db.execute("SELECT key FROM llm_doc WHERE error IS NULL")}
    todo, seen = [], set()
    for fid, name, text in db.execute(
            "SELECT id, filename, text FROM attachment_text WHERE read_status='ok' AND is_inline=0"):
        key = hashlib.sha1(text.encode()).hexdigest()
        if key not in done and key not in seen and int(key[:8], 16) % SHARD[1] == SHARD[0]:
            seen.add(key)
            todo.append((key, name, text))
    todo = todo[:limit]
    print(f"attachments: {len(todo)} unique texts to classify")

    def work(item):
        key, name, text = item
        msgs = [{"role": "system", "content": DOC_PROMPT},
                {"role": "user", "content": f"Filename: {name}\n---\n{doc_excerpt(text)}"}]
        try:
            out, usage = ai.chat(MODEL, msgs, max_tokens=1200)
            save(db, "llm_doc", (key, json.dumps(ai.parse_json(out)), MODEL, ai.cost(MODEL, usage), None, "text"))
        except Exception as e:
            save(db, "llm_doc", (key, None, MODEL, 0, str(e)[:300], "text"))

    with ThreadPoolExecutor(WORKERS) as pool:
        for i, _ in enumerate(as_completed([pool.submit(work, t) for t in todo])):
            if i % 200 == 0:
                print(f"  docs {i}/{len(todo)}")


def render_page(path):
    """Render page 1 of a PDF (or pass an image through) as base64 JPEG."""
    tmpdir = tempfile.mkdtemp()
    try:
        src = os.path.join(tmpdir, "in.pdf")
        shutil.copyfile(path, src)
        with open(src, "rb") as f:
            head = f.read(8)
        if not head.startswith(b"%PDF"):
            return base64.b64encode(open(src, "rb").read()).decode(), "image/png" if head.startswith(b"\x89PNG") else "image/jpeg"
        subprocess.run(["pdftoppm", "-jpeg", "-r", "110", "-f", "1", "-l", "1", "-scale-to", "1600", src,
                        os.path.join(tmpdir, "p")], check=True, capture_output=True, timeout=60)
        jpg = next(f for f in os.listdir(tmpdir) if f.endswith(".jpg"))
        return base64.b64encode(open(os.path.join(tmpdir, jpg), "rb").read()).decode(), "image/jpeg"
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def run_scans(db, att_dir, limit):
    done = {r[0] for r in db.execute("SELECT key FROM llm_doc WHERE error IS NULL")}
    files = {f.split("__", 1)[0]: os.path.join(att_dir, f) for f in os.listdir(att_dir)}
    todo = [(fid, name) for fid, name in db.execute(
        "SELECT id, filename FROM attachment_text WHERE read_status='needs_ocr' AND is_inline=0")
        if "scan:" + fid not in done][:limit]
    print(f"scans: {len(todo)} page images to read with the vision model")

    def work(item):
        fid, name = item
        try:
            b64, mime = render_page(files[fid])
            msgs = [{"role": "system", "content": DOC_PROMPT + '\nAlso add "page_text": a plain transcription of the page (max 800 chars).'},
                    {"role": "user", "content": [
                        {"type": "text", "text": f"Filename: {name}. This is page 1 of a scanned document."},
                        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}]}]
            out, usage = ai.chat(MODEL, msgs, max_tokens=3000)
            save(db, "llm_doc", ("scan:" + fid, json.dumps(ai.parse_json(out)), MODEL, ai.cost(MODEL, usage), None, "vision"))
        except Exception as e:
            save(db, "llm_doc", ("scan:" + fid, None, MODEL, 0, str(e)[:300], "vision"))

    with ThreadPoolExecutor(max(4, WORKERS // 2)) as pool:
        for i, _ in enumerate(as_completed([pool.submit(work, t) for t in todo])):
            if i % 100 == 0:
                print(f"  scans {i}/{len(todo)}")


def main(raw_db, work_db, att_dir, what="all", limit="1000000"):
    raw = sqlite3.connect(raw_db)
    db = sqlite3.connect(work_db, check_same_thread=False, timeout=120)
    setup(db)
    limit = int(limit)
    if what in ("emails", "all"):
        run_emails(raw, db, limit)
    if what in ("attachments", "all"):
        run_docs(db, limit)
    if what in ("scans", "all"):
        run_scans(db, att_dir, limit)
    for t in ("llm_email", "llm_doc"):
        ok, err, c = db.execute(f"SELECT sum(error IS NULL), sum(error IS NOT NULL), sum(cost) FROM {t}").fetchone()
        print(f"{t}: ok={ok} errors={err} cost=${(c or 0):.4f}")


if __name__ == "__main__":
    main(*sys.argv[1:])
