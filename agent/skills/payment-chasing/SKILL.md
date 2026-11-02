---
name: payment-chasing
description: Which vendors chase payment, how far they escalated (reminder, escalation, final notice) and whether the invoices they chase are actually in the inbox.
---

# Payment chasing

**Levels** (stored per email in `emails.chase_level`, per chased invoice in `chases.level`):

| level | rank | means |
|---|---|---|
| `reminder` | 1 | polite reminder, "due soon", "now due", first follow-up |
| `escalation` | 2 | past due / overdue follow-ups, repeated chasing, credit hold, deliveries at risk, collections team |
| `final_notice` | 3 | explicit final notice, disconnection / suspension / legal / collections-agency threat |

"How far a vendor went" = the **highest** level reached (`vendors.max_chase_level`), plus the date of first and last chase.

**Steps**
1. `vendor_chasing()` with no vendor → ranked list of chasers. With a vendor → its timeline and per-invoice status.
2. For "are the chased invoices in the inbox?" report the four `chases.status` buckets with counts, then list the invoices **not in inbox** (those are the actionable ones: AP may never have received them).
3. `mentioned elsewhere, no invoice document` means the number appears in some email or statement, but no invoice PDF for it was found: say so explicitly; it is not the same as "received".
4. A chasing email without invoice numbers (`no invoice number given`) still counts as chasing; mention the amount if `emails.amount_due` is set.
5. Statements attached to chasing emails contribute their listed open invoices (`chases.source` starts with `attachment:`).

**Caveats to state when relevant**
- Matching uses normalised invoice numbers (`invoice_key`: no punctuation, no leading zeros, no "INV" prefix). Different numbering between a statement and the invoice PDF (e.g. SO vs INV numbers) can produce false "not in inbox": say "not found under that number".
- Utility "final notice" emails (e.g. FPL disconnection notices) are real final notices, but the bill itself is often not attached.
- Chase classification comes from the model; `emails.rule_type` is an independent keyword check. If they disagree the row is in `review_queue`.
