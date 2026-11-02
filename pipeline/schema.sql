-- Tables the agent queries (D1 / SQLite). Rebuilt from scratch on every import.
DROP TABLE IF EXISTS emails;
DROP TABLE IF EXISTS attachments;
DROP TABLE IF EXISTS invoices;
DROP TABLE IF EXISTS chases;
DROP TABLE IF EXISTS vendors;
DROP TABLE IF EXISTS quality;
DROP TABLE IF EXISTS search;

-- One row per email. doc_type: invoice | statement | payment_reminder | remittance | other
CREATE TABLE emails (
  id TEXT PRIMARY KEY, conversation_id TEXT, direction TEXT, sent_at TEXT,
  from_name TEXT, from_email TEXT, subject TEXT, body TEXT,
  doc_type TEXT, chase_level TEXT,            -- none | reminder | escalation | final_notice
  vendor TEXT, vendor_key TEXT, chased_invoices TEXT, amount_due REAL,
  confidence REAL, rule_type TEXT, needs_review INTEGER, review_reason TEXT, attachment_count INTEGER
);

-- One row per attachment referenced by an email (including the ones missing from the zip).
CREATE TABLE attachments (
  id TEXT PRIMARY KEY, email_id TEXT, filename TEXT, content_type TEXT, size INTEGER, is_inline INTEGER,
  read_status TEXT,   -- ok | ok_vision | missing | password | error | unsupported | needs_ocr
  read_method TEXT, read_note TEXT,
  doc_type TEXT,      -- invoice | statement | payment_reminder | remittance | other | unknown
  confidence REAL, rule_type TEXT, needs_review INTEGER, review_reason TEXT, text TEXT
);

-- One row per invoice file. is_primary = 1 for the first copy of each (vendor, invoice number).
CREATE TABLE invoices (
  attachment_id TEXT PRIMARY KEY, email_id TEXT, vendor TEXT, vendor_key TEXT,
  invoice_number TEXT, invoice_key TEXT, invoice_date TEXT, due_date TEXT, po_number TEXT,
  payment_terms TEXT, currency TEXT, subtotal REAL, tax REAL, total REAL, amount_due REAL,
  bill_to TEXT, received_at TEXT, read_method TEXT, confidence REAL,
  checks TEXT, needs_review INTEGER, review_reason TEXT, is_primary INTEGER, duplicate_count INTEGER
);

-- One row per (chasing email, invoice it chases). status: invoice in inbox | mentioned elsewhere,
-- no invoice document | not in inbox | no invoice number given
CREATE TABLE chases (
  email_id TEXT, vendor TEXT, vendor_key TEXT, level TEXT, level_rank INTEGER, sent_at TEXT, subject TEXT,
  invoice_ref TEXT, invoice_key TEXT, status TEXT, matched_attachment_id TEXT, matched_total REAL, source TEXT
);

CREATE TABLE vendors (
  vendor_key TEXT PRIMARY KEY, name TEXT, domains TEXT, emails INTEGER, invoices INTEGER, total_billed REAL,
  chase_emails INTEGER, max_chase_level TEXT, first_chase TEXT, last_chase TEXT,
  chased_invoices INTEGER, chased_found INTEGER
);

-- How well the pipeline did: coverage, self-check pass rates, agreement, cost.
CREATE TABLE quality (metric TEXT PRIMARY KEY, value TEXT, detail TEXT);

CREATE INDEX idx_emails_type ON emails(doc_type, chase_level);
CREATE INDEX idx_emails_vendor ON emails(vendor_key);
CREATE INDEX idx_att_email ON attachments(email_id);
CREATE INDEX idx_inv_key ON invoices(invoice_key);
CREATE INDEX idx_inv_vendor ON invoices(vendor_key);
CREATE INDEX idx_chases_vendor ON chases(vendor_key);
