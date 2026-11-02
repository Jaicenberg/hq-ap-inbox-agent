# AGENTS: operating instructions

## The data

One month of the Eaze AP inbox: **5,705 emails, 24 Aug – 30 Sep 2026**, and the 9,903 attachments they reference.
It was processed once, offline, by the pipeline in `pipeline/` (see README), and stored in D1:

| table | one row per | key columns |
|---|---|---|
| `emails` | email | `sent_at`, `from_email`, `subject`, `body` (first 3,000 chars), `doc_type`, `chase_level`, `vendor`, `chased_invoices` (JSON), `amount_due`, `confidence`, `rule_type`, `needs_review`, `review_reason` |
| `attachments` | referenced file | `email_id`, `filename`, `read_status`, `read_method`, `read_note`, `doc_type`, `confidence`, `needs_review`, `review_reason`, `text` (first 4,000 chars) |
| `invoices` | invoice file | `vendor`, `invoice_number`, `invoice_key` (normalised), `invoice_date`, `due_date`, `po_number`, `payment_terms`, `currency`, `subtotal`, `tax`, `total`, `amount_due`, `bill_to`, `received_at`, `read_method`, `confidence`, `checks` (JSON), `needs_review`, `review_reason`, `is_primary`, `duplicate_count` |
| `chases` | (chasing email, invoice it chases) | `vendor`, `level` (`reminder` < `escalation` < `final_notice`), `level_rank` 1–3, `sent_at`, `invoice_ref`, `status`, `matched_attachment_id`, `matched_total` |
| `vendors` | vendor | `name`, `domains`, `emails`, `invoices`, `total_billed`, `chase_emails`, `max_chase_level`, `chased_invoices`, `chased_found` |
| `quality` | metric | coverage, self-check pass rates, rule/model agreement, judge accuracy, batch cost |
| `search` | FTS5 index | `kind` (email/attachment), `ref_id`, `title`, `content` |

Value conventions:
- `doc_type` is one of `invoice`, `statement`, `payment_reminder`, `remittance`, `other` (attachments can also be `unknown` when unreadable).
- `read_status`: `ok` (text layer), `ok_vision` (scanned page read by a vision model, page 1 only), `missing` (referenced but not in the zip), `password`, `error`, `unsupported`, `needs_ocr` (vision step failed).
- **Invoices repeat** (forwards, re-sends). Count and sum with `is_primary = 1` unless the user asks about copies.
- `chases.status`: `invoice in inbox` | `mentioned elsewhere, no invoice document` | `not in inbox` | `no invoice number given`.
- Dates are ISO strings; `sent_at` is UTC.

## How to answer

1. Pick the narrowest tool: `inbox_overview`, `vendor_chasing`, `get_invoice`, `get_email`, `search_inbox`, `quality_report`, `review_queue`. Use `run_sql` for anything else (read-only `SELECT`, max 200 rows).
2. Before a task covered by a skill, call `load_skill` to read it. Skills: see the list below.
3. Vendor names vary ("NABIFIVE, LLC dba Nabis" vs "Nabis"): match with `LIKE '%term%'` on `vendor`/`vendor_key`, and say which spelling you matched.
4. If a result looks surprising (zero rows, huge totals), check it with a second query before answering.
5. Always end answers that rely on extracted data with the relevant caveat when it applies: rows with `needs_review = 1`, unreadable/missing files, or vision-read scans.
6. Never print raw SQL to the user unless they ask; never invent column names: the schema above is complete.
