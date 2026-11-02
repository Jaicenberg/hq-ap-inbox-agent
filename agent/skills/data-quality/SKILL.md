---
name: data-quality
description: Report how accurate the agent is, which files it could not read or could not find, and what needs human review. Use for any "how sure are you / what's missing / what failed" question.
---

# Data quality and self-assessment

Call `quality_report()` first. It returns the metrics stored by the pipeline, plus live counts.

**Explain accuracy in three layers, never as one magic number:**

1. **Coverage**: how many files were actually read: text layer, vision (scans), missing from the zip, password-protected, corrupt/unsupported. Missing ≠ unreadable: missing means the email references a file that was not delivered with the data.
2. **Self-checks** (no human labels needed): share of invoices whose number and total appear verbatim in the source PDF, arithmetic consistency, and agreement between the model's classification and an independent keyword-rule classifier.
3. **Judge accuracy**: a stronger model re-labelled a random sample blind; the metric `judge_*` gives agreement with a 95% confidence interval. Say clearly that this is model-vs-model, not human-verified.

**Review queue**: `review_queue(kind)` lists items flagged for a human, with the reason. Group reasons when summarising (e.g. "212 scans unverified, 40 total-not-found, 18 rules-vs-model disagreements").

**Known limitations to disclose when relevant**
- Scanned PDFs: only page 1 is read; totals on later pages can be missed.
- Inline images (signature logos) were not delivered; that does not affect AP documents.
- Text is truncated (emails: 3,000 chars, attachments: 4,000 chars) in the database; facts deep inside long documents can be missing.
- Classifications are probabilistic; a confidence below 0.7 is flagged.
