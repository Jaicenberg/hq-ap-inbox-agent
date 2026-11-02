"""Step 3b: estimate accuracy. A stronger model re-labels a random sample BLIND (it never sees
the batch model's answer); we report agreement with a 95% Wilson interval.

This is model-vs-model agreement, not human ground truth: it bounds errors the cheap model makes
that a stronger model would not. Results go to the `judge` table and into the quality report.
Usage: python -I judge.py <raw.sqlite> <work.sqlite> [n_per_kind=80]
"""
import hashlib
import json
import math
import os
import random
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ai  # noqa: E402
from build import invoice_key, num  # noqa: E402
from classify import DOC_PROMPT, EMAIL_PROMPT, clean, doc_excerpt  # noqa: E402

JUDGE = os.environ.get("JUDGE_MODEL", "@cf/zai-org/glm-5.3")


def wilson(k, n, z=1.96):
    if n == 0:
        return 0, 0, 0
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return p, max(0, c - h), min(1, c + h)


def call(msgs):
    # Reasoning stays ON for the judge: accuracy matters more than cost for 160 calls.
    out, usage = ai.chat(JUDGE, msgs, max_tokens=6000, thinking=True)
    return ai.parse_json(out), ai.cost(JUDGE, usage)


def main(raw_db, work_db, n="80"):
    n = int(n)
    raw, db = sqlite3.connect(raw_db), sqlite3.connect(work_db, check_same_thread=False, timeout=120)
    db.execute("CREATE TABLE IF NOT EXISTS judge (kind TEXT, ref TEXT PRIMARY KEY, batch TEXT, judge TEXT, cost REAL)")
    rnd = random.Random(42)

    emails = {r[0]: r for r in raw.execute(
        """SELECT id, event_type, recipient_from_name, recipient_from_handle, subject, message_created_at,
                  body_text, attachments FROM frontapp_messages""")}
    done_e = [(i, json.loads(r)) for i, r in db.execute("SELECT id, result FROM llm_email WHERE error IS NULL")]
    sample_e = rnd.sample(done_e, min(n, len(done_e)))

    texts = {hashlib.sha1(t.encode()).hexdigest(): (f, t) for f, t in db.execute(
        "SELECT filename, text FROM attachment_text WHERE read_status='ok' AND is_inline=0")}
    done_d = [(k, json.loads(r)) for k, r in db.execute(
        "SELECT key, result FROM llm_doc WHERE error IS NULL AND source='text'") if k in texts]
    sample_d = rnd.sample(done_d, min(n, len(done_d)))

    def judge_email(item):
        mid, batch = item
        r = emails[mid]
        names = [a["filename"] for a in json.loads(r[7] or "[]") if not a["metadata"].get("is_inline")]
        it = {"id": mid, "direction": r[1], "from": f"{r[2]} <{r[3]}>", "date": r[5][:10],
              "subject": clean(r[4], 200), "attachments": names[:10], "body": clean(r[6], 1400)}
        res, c = call([{"role": "system", "content": EMAIL_PROMPT},
                       {"role": "user", "content": json.dumps([it], ensure_ascii=False)}])
        return "email", mid, batch, res["results"][0], c

    def judge_doc(item):
        key, batch = item
        name, text = texts[key]
        res, c = call([{"role": "system", "content": DOC_PROMPT},
                       {"role": "user", "content": f"Filename: {name}\n---\n{doc_excerpt(text)}"}])
        return "doc", key, batch, res, c

    jobs = [(judge_email, x) for x in sample_e] + [(judge_doc, x) for x in sample_d]
    with ThreadPoolExecutor(8) as pool:
        for fut in [pool.submit(f, x) for f, x in jobs]:
            try:
                kind, ref, batch, res, c = fut.result()
                db.execute("INSERT OR REPLACE INTO judge VALUES (?,?,?,?,?)",
                           (kind, ref, json.dumps(batch), json.dumps(res), c))
                db.commit()
            except Exception as e:
                print("judge call failed:", str(e)[:200])

    rows = [(k, json.loads(b), json.loads(j)) for k, b, j in db.execute("SELECT kind, batch, judge FROM judge")]
    metrics = {}
    for kind in ("email", "doc"):
        rs = [(b, j) for k, b, j in rows if k == kind]
        metrics[f"judge_{kind}_type"] = sum(b.get("type") == j.get("type") for b, j in rs), len(rs)
    pr = [(b, j) for k, b, j in rows if k == "email" and j.get("type") == "payment_reminder"]
    metrics["judge_email_chase_level"] = sum(b.get("chase_level") == j.get("chase_level") for b, j in pr), len(pr)
    inv = [(b, j) for k, b, j in rows if k == "doc" and b.get("type") == "invoice" and j.get("type") == "invoice"]
    metrics["judge_invoice_number"] = sum(invoice_key(b.get("invoice_number")) == invoice_key(j.get("invoice_number")) for b, j in inv), len(inv)
    metrics["judge_invoice_total"] = sum(
        num(b.get("total")) is not None and num(j.get("total")) is not None and abs(num(b["total"]) - num(j["total"])) < 0.01
        for b, j in inv), len(inv)
    metrics["judge_invoice_date"] = sum(b.get("invoice_date") == j.get("invoice_date") for b, j in inv), len(inv)
    db.execute("CREATE TABLE IF NOT EXISTS judge_metrics (metric TEXT PRIMARY KEY, k INTEGER, n INTEGER)")
    for m, (k, total) in metrics.items():
        db.execute("INSERT OR REPLACE INTO judge_metrics VALUES (?,?,?)", (m, k, total))
        p, lo, hi = wilson(k, total)
        print(f"{m:28} {k}/{total} = {p:.1%}  (95% CI {lo:.1%}-{hi:.1%})")
    db.commit()
    print("judge cost $%.3f" % (db.execute("SELECT sum(cost) FROM judge").fetchone()[0] or 0))


if __name__ == "__main__":
    main(*sys.argv[1:])
