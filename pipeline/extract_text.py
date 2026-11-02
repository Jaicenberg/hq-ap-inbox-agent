"""Step 2: turn every attachment into text, and record exactly what happened to each file.

- PDF  -> pdftotext -layout (free, local, fast). Empty text layer => status 'needs_ocr'.
- XLSX/DOCX/CSV/TXT/ICS/JSON -> read locally.
- Images / scanned PDFs -> left for the Workers AI fallback (ocr_fallback.py).
- Referenced in the DB but not in the zip -> status 'missing'.

Usage: python -I extract_text.py <raw.sqlite> <attachments_dir> <out.sqlite>
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import zipfile
from concurrent.futures import ThreadPoolExecutor
from xml.etree import ElementTree

MAX_CHARS = 20000


def pdf_text(path):
    # pdftotext on Windows cannot open paths longer than MAX_PATH: read via a short temp copy.
    tmp = None
    if len(path) > 200:
        fd, tmp = tempfile.mkstemp(suffix=".pdf")
        os.close(fd)
        shutil.copyfile(path, tmp)
    try:
        out = subprocess.run(
            ["pdftotext", "-layout", "-enc", "UTF-8", tmp or path, "-"],
            capture_output=True, timeout=60,
        )
    except subprocess.TimeoutExpired:
        return "error", "", "pdftotext timed out"
    finally:
        if tmp:
            os.remove(tmp)
    if out.returncode != 0:
        err = out.stderr.decode("utf-8", "replace")
        if "password" in err.lower():
            return "password", "", "PDF is password-protected: cannot be read without the password"
        return "error", "", err[:300]
    text = out.stdout.decode("utf-8", "replace")
    if len(text.strip()) < 30:
        return "needs_ocr", text, "PDF has no text layer (scanned image)"
    return "ok", text, None


def office_text(path, kind):
    """Minimal XLSX/DOCX reader using only the standard library."""
    try:
        z = zipfile.ZipFile(path)
        if kind == "docx":
            xml = ElementTree.fromstring(z.read("word/document.xml"))
            return "ok", " ".join(t.text or "" for t in xml.iter() if t.tag.endswith("}t")), None
        shared = []
        if "xl/sharedStrings.xml" in z.namelist():
            xml = ElementTree.fromstring(z.read("xl/sharedStrings.xml"))
            shared = ["".join(t.text or "" for t in si.iter() if t.tag.endswith("}t"))
                      for si in xml if si.tag.endswith("}si")]
        lines = []
        for name in sorted(n for n in z.namelist() if n.startswith("xl/worksheets/sheet")):
            xml = ElementTree.fromstring(z.read(name))
            for row in (e for e in xml.iter() if e.tag.endswith("}row")):
                cells = []
                for c in (e for e in row if e.tag.endswith("}c")):
                    v = next((e.text for e in c.iter() if e.tag.endswith("}v") or e.tag.endswith("}t")), "")
                    if c.get("t") == "s" and v and v.isdigit() and int(v) < len(shared):
                        v = shared[int(v)]
                    cells.append(v or "")
                lines.append("\t".join(cells))
        return "ok", "\n".join(lines), None
    except Exception as e:  # corrupt / password-protected files
        return "error", "", f"could not open {kind}: {e}"


def extract(item):
    att, path = item
    ct, name = att["content_type"], att["filename"].lower()
    if path is None:
        return "missing", "", "referenced in the email but not present in attachments.zip"
    if ct == "application/pdf" or name.endswith(".pdf"):
        return pdf_text(path)
    if name.endswith(".xlsx"):
        return office_text(path, "xlsx")
    if name.endswith(".docx"):
        return office_text(path, "docx")
    if ct.startswith("image/") or ct == "application/octet-stream":
        return "needs_ocr", "", "image: needs vision model"
    if ct.startswith("text/") or name.endswith((".csv", ".ics", ".txt")):
        with open(path, encoding="utf-8", errors="replace") as f:
            return "ok", f.read(), None
    if name.endswith(".zip"):
        return "unsupported", "", "zip archive inside an email: not opened"
    return "unsupported", "", f"no reader for {ct}"


def main(raw_db, att_dir, out_db):
    files = {f.split("__", 1)[0]: os.path.join(att_dir, f) for f in os.listdir(att_dir)}
    src = sqlite3.connect(raw_db)
    items = []
    for mid, atts in src.execute("SELECT id, attachments FROM frontapp_messages"):
        for a in json.loads(atts or "[]"):
            a["message_id"] = mid
            items.append((a, files.get(a["id"])))

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(extract, items))

    out = sqlite3.connect(out_db)
    out.execute("DROP TABLE IF EXISTS attachment_text")
    out.execute("""CREATE TABLE attachment_text (
        id TEXT PRIMARY KEY, message_id TEXT, filename TEXT, content_type TEXT, size INTEGER,
        is_inline INTEGER, read_status TEXT, read_note TEXT, text TEXT)""")
    for (a, _), (status, text, note) in zip(items, results):
        out.execute("INSERT INTO attachment_text VALUES (?,?,?,?,?,?,?,?,?)", (
            a["id"], a["message_id"], a["filename"], a["content_type"], a["size"],
            int(bool(a["metadata"].get("is_inline"))), status, note, text[:MAX_CHARS]))
    out.commit()
    for row in out.execute("SELECT read_status, is_inline, count(*) FROM attachment_text GROUP BY 1, 2"):
        print(row)


if __name__ == "__main__":
    main(*sys.argv[1:4])
