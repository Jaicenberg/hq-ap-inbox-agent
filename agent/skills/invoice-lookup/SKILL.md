---
name: invoice-lookup
description: Find invoices and their key fields (number, vendor, dates, PO, totals), handle duplicates, and explain how trustworthy each extracted field is.
---

# Invoice lookup

**Finding an invoice**
- By number → `get_invoice(ref)` (normalises "INV-00123", "#123", "123").
- By vendor / period / amount → `run_sql` on `invoices` with `is_primary = 1`.
- By PO → `invoices.po_number LIKE '%154346%'`.
- If nothing matches, try `search_inbox` (the number may only appear in an email body or statement) and say where it was found.

**Fields**: `vendor`, `invoice_number`, `invoice_date`, `due_date`, `po_number`, `payment_terms`, `currency`, `subtotal`, `tax`, `total`, `amount_due`, `bill_to` (which Eaze store/entity was billed), `received_at` (when the email arrived).

**Duplicates**: the same invoice often arrives 2–5 times (vendor sends, AP forwards). `is_primary = 1` marks the first copy; `duplicate_count` tells how many repeats. Totals must use primary rows only.

**Trust: the `checks` JSON on every invoice**
| check | meaning |
|---|---|
| `number_in_source` | the extracted invoice number appears verbatim in the PDF text |
| `total_in_source` | the extracted total appears verbatim in the PDF text |
| `arithmetic` | subtotal + tax = total |
| `dates_valid`, `due_after_issue` | dates parse and make sense |
| `has_number`, `has_vendor`, `has_total` | required fields present |

When presenting an invoice with `needs_review = 1`, show the `review_reason`. Invoices read from scans (`read_method` = vision) cannot be verified against a text layer: say "read from a scan, unverified".

**Example SQL**
```sql
-- biggest unique invoices in September
SELECT vendor, invoice_number, invoice_date, total, currency, needs_review
FROM invoices WHERE is_primary = 1 AND invoice_date LIKE '2026-09%'
ORDER BY total DESC LIMIT 10;
```
