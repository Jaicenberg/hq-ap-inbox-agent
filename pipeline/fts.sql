-- Full-text search over emails and attachment text (D1 supports FTS5). Appended after the data import.
CREATE VIRTUAL TABLE search USING fts5(kind UNINDEXED, ref_id UNINDEXED, title, content, tokenize = 'porter unicode61');
INSERT INTO search SELECT 'email', id, coalesce(subject, '') || ' | ' || coalesce(from_name, '') || ' ' || coalesce(from_email, ''), coalesce(body, '') FROM emails;
INSERT INTO search SELECT 'attachment', id, filename, coalesce(text, '') FROM attachments WHERE is_inline = 0;
