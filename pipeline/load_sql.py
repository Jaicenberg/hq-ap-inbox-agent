"""Step 1: load the Postgres dump (COPY ... FROM stdin blocks) into a local SQLite file.

No Postgres needed: we parse the COPY text format directly.
Usage: python -I load_sql.py <ap_inbox_candidate.sql> <out.sqlite>
"""
import re
import sqlite3
import sys

COPY_RE = re.compile(r"^COPY frontapp\.(\w+) \((.*)\) FROM stdin;$")
ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "v": "\v", "\\": "\\"}


def unescape(field):
    if field == "\\N":
        return None
    if "\\" not in field:
        return field
    out, i = [], 0
    while i < len(field):
        c = field[i]
        if c == "\\" and i + 1 < len(field):
            out.append(ESCAPES.get(field[i + 1], field[i + 1]))
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def main(sql_path, db_path):
    db = sqlite3.connect(db_path)
    table, cols, rows = None, None, []
    with open(sql_path, encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if table is None:
                m = COPY_RE.match(line)
                if m:
                    table, cols = m.group(1), [c.strip() for c in m.group(2).split(",")]
                    db.execute(f"DROP TABLE IF EXISTS {table}")
                    db.execute(f"CREATE TABLE {table} ({', '.join(cols)})")
                    rows = []
                continue
            if line == "\\.":
                db.executemany(
                    f"INSERT INTO {table} VALUES ({', '.join('?' * len(cols))})", rows
                )
                print(f"{table}: {len(rows)} rows")
                table = None
                continue
            rows.append([unescape(v) for v in line.split("\t")])
    db.commit()


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
