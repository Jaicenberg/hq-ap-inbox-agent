import { tool } from "ai";
import { z } from "zod";
import paymentChasing from "../agent/skills/payment-chasing/SKILL.md?raw";
import invoiceLookup from "../agent/skills/invoice-lookup/SKILL.md?raw";
import dataQuality from "../agent/skills/data-quality/SKILL.md?raw";

// Skills are loaded on demand: the system prompt only carries name + description.
export const SKILLS: Record<string, string> = {
  "payment-chasing": paymentChasing,
  "invoice-lookup": invoiceLookup,
  "data-quality": dataQuality
};

export function skillIndex(): string {
  return Object.entries(SKILLS)
    .map(([name, md]) => `- ${name}: ${md.match(/description:\s*(.+)/)?.[1] ?? ""}`)
    .join("\n");
}

// Same normalisation as pipeline/build.py: "INV-000123" -> "123", "SO-164145" -> "SO164145".
export function invoiceKey(ref: string): string | null {
  let r = ref.trim().replace(/^(invoice|inv|no|number|#)[\s#.:_-]*/i, "");
  r = r.replace(/[^A-Za-z0-9]/g, "").toUpperCase().replace(/^0+(?=\d)/, "");
  return r.length >= 3 ? r : null;
}

const MAX_ROWS = 200;
const WRITE_SQL =
  /\b(insert|update|delete|drop|alter|create|replace|attach|detach|pragma|vacuum|reindex|truncate)\b/i;

function trimRow(row: Record<string, unknown>, maxText = 600) {
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(row)) {
    out[k] = typeof v === "string" && v.length > maxText ? `${v.slice(0, maxText)}…` : v;
  }
  return out;
}

async function all(db: D1Database, sql: string, ...params: unknown[]) {
  const res = await db.prepare(sql).bind(...params).all();
  return res.results as Record<string, unknown>[];
}

export function buildTools(db: D1Database) {
  return {
    load_skill: tool({
      description: "Read the full instructions of a skill before doing that kind of task.",
      inputSchema: z.object({ name: z.enum(Object.keys(SKILLS) as [string, ...string[]]) }),
      execute: async ({ name }) => SKILLS[name]
    }),

    inbox_overview: tool({
      description:
        "Counts of emails and attachments by type, unique invoices and total billed, chase levels, top vendors. Start here for broad questions.",
      inputSchema: z.object({}),
      execute: async () => {
        const [range, emailTypes, attTypes, readStatus, inv, chase, topVendors, topChasers] =
          await Promise.all([
            all(db, "SELECT min(sent_at) first, max(sent_at) last, count(*) emails FROM emails"),
            all(db, "SELECT doc_type, count(*) n FROM emails GROUP BY 1 ORDER BY 2 DESC"),
            all(db, "SELECT doc_type, count(*) n FROM attachments WHERE is_inline = 0 GROUP BY 1 ORDER BY 2 DESC"),
            all(db, "SELECT read_status, count(*) n FROM attachments WHERE is_inline = 0 GROUP BY 1 ORDER BY 2 DESC"),
            all(db, `SELECT count(*) invoice_files, sum(is_primary) unique_invoices,
                     round(sum(CASE WHEN is_primary = 1 THEN total END), 2) total_billed_usd,
                     sum(CASE WHEN is_primary = 1 AND needs_review = 1 THEN 1 ELSE 0 END) unique_needing_review
                     FROM invoices`),
            all(db, "SELECT chase_level, count(*) emails FROM emails WHERE doc_type = 'payment_reminder' GROUP BY 1"),
            all(db, "SELECT name, invoices, total_billed FROM vendors ORDER BY invoices DESC LIMIT 10"),
            all(db, `SELECT name, chase_emails, max_chase_level FROM vendors WHERE chase_emails > 0
                     ORDER BY CASE max_chase_level WHEN 'final_notice' THEN 3 WHEN 'escalation' THEN 2 ELSE 1 END DESC,
                     chase_emails DESC LIMIT 10`)
          ]);
        return { range: range[0], emailTypes, attachmentTypes: attTypes, readStatus, invoices: inv[0], chaseLevels: chase, topVendors, topChasers };
      }
    }),

    vendor_chasing: tool({
      description:
        "Payment chasing. Without vendor: every vendor that chased, highest level reached, how many chased invoices are in the inbox. With vendor: its chase timeline and the status of each chased invoice.",
      inputSchema: z.object({
        vendor: z.string().optional().describe("Part of the vendor name, e.g. 'connected' or 'nabis'")
      }),
      execute: async ({ vendor }) => {
        if (!vendor) {
          return all(
            db,
            `SELECT v.name vendor, v.max_chase_level, v.chase_emails, v.first_chase, v.last_chase,
                    count(DISTINCT c.invoice_key) invoices_chased,
                    count(DISTINCT CASE WHEN c.status = 'invoice in inbox' THEN c.invoice_key END) in_inbox,
                    count(DISTINCT CASE WHEN c.status = 'mentioned elsewhere, no invoice document' THEN c.invoice_key END) mentioned_only,
                    count(DISTINCT CASE WHEN c.status = 'not in inbox' THEN c.invoice_key END) not_in_inbox
             FROM vendors v LEFT JOIN chases c ON c.vendor_key = v.vendor_key
             WHERE v.chase_emails > 0 GROUP BY v.vendor_key
             ORDER BY CASE v.max_chase_level WHEN 'final_notice' THEN 3 WHEN 'escalation' THEN 2 ELSE 1 END DESC,
                      v.chase_emails DESC`
          );
        }
        const like = `%${vendor.toLowerCase()}%`;
        const vendors = await all(
          db,
          "SELECT * FROM vendors WHERE lower(name) LIKE ?1 OR vendor_key LIKE ?1 OR domains LIKE ?1",
          like
        );
        if (!vendors.length) return { error: `No vendor matching '${vendor}'. Try a shorter part of the name.` };
        const keys = vendors.map((v) => v.vendor_key as string);
        const ph = keys.map((_, i) => `?${i + 1}`).join(",");
        const timeline = await all(
          db,
          `SELECT e.id email_id, e.sent_at, e.chase_level, e.subject, e.from_email, e.amount_due, e.chased_invoices, e.needs_review, e.review_reason
           FROM emails e WHERE e.doc_type = 'payment_reminder' AND e.vendor_key IN (${ph}) ORDER BY e.sent_at`,
          ...keys
        );
        const invoices = await all(
          db,
          `SELECT invoice_ref, status, max(level) highest_level, count(*) times_chased, min(sent_at) first_chased,
                  matched_total, group_concat(DISTINCT source) sources
           FROM chases WHERE vendor_key IN (${ph}) GROUP BY coalesce(invoice_key, email_id) ORDER BY status, first_chased`,
          ...keys
        );
        return { vendors, timeline: timeline.map((r) => trimRow(r, 300)), chasedInvoices: invoices };
      }
    }),

    get_invoice: tool({
      description:
        "Look up an invoice by its number (any formatting), PO number, or attachment id. Returns every copy with its self-checks.",
      inputSchema: z.object({ ref: z.string() }),
      execute: async ({ ref }) => {
        const key = invoiceKey(ref);
        const rows = await all(
          db,
          `SELECT i.*, e.subject email_subject, e.from_email, a.filename
           FROM invoices i JOIN emails e ON e.id = i.email_id JOIN attachments a ON a.id = i.attachment_id
           WHERE i.invoice_key = ?1 OR i.attachment_id = ?2 OR replace(i.po_number, '-', '') LIKE ?3
           ORDER BY i.received_at LIMIT 20`,
          key ?? "",
          ref,
          `%${ref.replace(/[^A-Za-z0-9]/g, "")}%`
        );
        if (rows.length) return rows.map((r) => ({ ...r, checks: JSON.parse(String(r.checks ?? "{}")) }));
        const chased = await all(db, "SELECT vendor, level, sent_at, status FROM chases WHERE invoice_key = ?1", key ?? "");
        return {
          found: false,
          note: "No invoice document with this number. It may only be mentioned in emails or statements: try search_inbox.",
          chasedBy: chased
        };
      }
    }),

    get_email: tool({
      description: "Full detail of one email: classification, body, every attachment with how it was read, and invoices extracted from it.",
      inputSchema: z.object({ id: z.string() }),
      execute: async ({ id }) => {
        const [email] = await all(db, "SELECT * FROM emails WHERE id = ?1", id);
        if (!email) return { error: "No email with that id" };
        const attachments = await all(
          db,
          `SELECT id, filename, content_type, is_inline, read_status, read_method, read_note, doc_type, confidence,
                  needs_review, review_reason, substr(text, 1, 1200) text_excerpt
           FROM attachments WHERE email_id = ?1 AND (is_inline = 0 OR read_status != 'missing')`,
          id
        );
        const invoices = await all(db, "SELECT * FROM invoices WHERE email_id = ?1", id);
        return { email, attachments, invoices };
      }
    }),

    search_inbox: tool({
      description:
        "Full-text search over email subjects/bodies and attachment text. Use for names, numbers or phrases not covered by structured fields.",
      inputSchema: z.object({
        query: z.string().describe("Words or numbers to find (all must match)"),
        kind: z.enum(["email", "attachment", "any"]).default("any"),
        limit: z.number().int().min(1).max(50).default(15)
      }),
      execute: async ({ query, kind, limit }) => {
        const terms = query.match(/[\p{L}\p{N}][\p{L}\p{N}._-]*/gu) ?? [];
        if (!terms.length) return { error: "Empty query" };
        const match = terms.map((t) => `"${t.replace(/"/g, "")}"`).join(" ");
        const rows = await all(
          db,
          `SELECT s.kind, s.ref_id, s.title, snippet(search, 3, '[', ']', '…', 18) snippet,
                  coalesce(e.sent_at, pe.sent_at) sent_at, coalesce(e.doc_type, a.doc_type) doc_type,
                  a.email_id, a.read_status
           FROM search s
           LEFT JOIN emails e ON s.kind = 'email' AND e.id = s.ref_id
           LEFT JOIN attachments a ON s.kind = 'attachment' AND a.id = s.ref_id
           LEFT JOIN emails pe ON pe.id = a.email_id
           WHERE search MATCH ?1 AND (?2 = 'any' OR s.kind = ?2)
           ORDER BY rank LIMIT ?3`,
          match,
          kind,
          limit
        );
        return { query: match, results: rows };
      }
    }),

    quality_report: tool({
      description:
        "How accurate the agent is and what it could not read: coverage, missing/unreadable files, self-check pass rates, rule/model agreement, judge accuracy, cost.",
      inputSchema: z.object({}),
      execute: async () => {
        const [metrics, unreadable, reviewReasons] = await Promise.all([
          all(db, "SELECT * FROM quality"),
          all(
            db,
            `SELECT read_status, count(*) n, group_concat(filename, ' | ') examples FROM
               (SELECT read_status, filename FROM attachments
                WHERE is_inline = 0 AND read_status NOT IN ('ok', 'ok_vision'))
             GROUP BY read_status`
          ),
          all(
            db,
            `SELECT 'invoice' kind, review_reason, count(*) n FROM invoices WHERE needs_review = 1 AND is_primary = 1 GROUP BY 2
             UNION ALL SELECT 'email', review_reason, count(*) FROM emails WHERE needs_review = 1 GROUP BY 2
             ORDER BY n DESC LIMIT 25`
          )
        ]);
        return { metrics, unreadableFiles: unreadable.map((r) => trimRow(r, 500)), reviewReasons };
      }
    }),

    review_queue: tool({
      description: "Items flagged for human review (low confidence, failed self-check, unreadable file, rules vs model disagreement).",
      inputSchema: z.object({
        kind: z.enum(["invoices", "emails", "attachments"]),
        limit: z.number().int().min(1).max(50).default(20)
      }),
      execute: async ({ kind, limit }) => {
        const sql = {
          invoices: `SELECT attachment_id, vendor, invoice_number, total, read_method, confidence, review_reason
                     FROM invoices WHERE needs_review = 1 AND is_primary = 1 ORDER BY confidence LIMIT ?1`,
          emails: `SELECT id, sent_at, subject, doc_type, rule_type, confidence, review_reason
                   FROM emails WHERE needs_review = 1 ORDER BY confidence LIMIT ?1`,
          attachments: `SELECT id, email_id, filename, read_status, doc_type, review_reason
                        FROM attachments WHERE needs_review = 1 AND is_inline = 0 LIMIT ?1`
        }[kind];
        return all(db, sql, limit);
      }
    }),

    draft_reply: tool({
      description:
        "Draft (never send) a reply to a vendor email, e.g. a payment reminder: confirms which chased invoices AP has on file and asks for copies of the ones it cannot find. Get the email_id from vendor_chasing, search_inbox or get_email first.",
      inputSchema: z.object({
        email_id: z.string().describe("id of the vendor email to reply to"),
        note: z
          .string()
          .optional()
          .describe("Anything the user wants added, in their words (e.g. 'payment is scheduled for Friday')")
      }),
      execute: async ({ email_id, note }) => {
        const [email] = await all(
          db,
          "SELECT id, sent_at, from_name, from_email, subject, vendor, doc_type, chase_level, amount_due FROM emails WHERE id = ?1",
          email_id
        );
        if (!email) return { error: "No email with that id" };
        const chased = await all(
          db,
          `SELECT invoice_ref, status, matched_total FROM chases
           WHERE email_id = ?1 AND invoice_ref IS NOT NULL ORDER BY status, invoice_ref`,
          email_id
        );
        const onFile = chased.filter((c) => c.status === "invoice in inbox");
        const notFound = chased.filter((c) => c.status !== "invoice in inbox");
        const money = (v: unknown) =>
          typeof v === "number" ? ` ($${v.toLocaleString("en-US", { minimumFractionDigits: 2 })})` : "";
        // Forwards come from eaze.com: the vendor's own address is not in the data.
        const to = String(email.from_email).endsWith("@eaze.com") ? null : email.from_email;
        const lines = [
          `Hi ${email.from_name || email.vendor || "team"},`,
          "",
          `Thank you for your email "${email.subject}".`
        ];
        if (onFile.length) {
          lines.push("", "We have these invoices on file and they are with our AP team for processing:");
          onFile.forEach((c) => lines.push(`- ${c.invoice_ref}${money(c.matched_total)}`));
        }
        if (notFound.length) {
          lines.push(
            "",
            "We could not find these invoices in our records. Could you please send copies to ap@eaze.com?"
          );
          notFound.forEach((c) => lines.push(`- ${c.invoice_ref}`));
        }
        if (!chased.length) {
          lines.push(
            "",
            "Could you please send the invoice numbers and copies of the invoices this relates to, so we can match them to our records?"
          );
        }
        if (note) lines.push("", note);
        lines.push(
          "",
          "For payment status questions, please contact APInquiries@Eaze.com.",
          "",
          "Best regards,",
          "Eaze Accounts Payable"
        );
        return {
          status: "DRAFT ONLY: nothing was sent",
          to: to ?? "(vendor address not in the data: this email was forwarded internally)",
          subject: `Re: ${String(email.subject).replace(/^((re|fwd?):\s*)+/i, "")}`,
          body: lines.join("\n"),
          basedOn: { email_sent_at: email.sent_at, chase_level: email.chase_level, invoices_on_file: onFile.length, invoices_not_found: notFound.length },
          reviewBeforeSending:
            "Invoice status comes from matching invoice numbers in this inbox; payment dates are not in the data."
        };
      }
    }),

    run_sql: tool({
      description:
        "Run a read-only SQLite SELECT (or WITH) against the inbox tables (schema in your instructions). Max 200 rows. Use when no other tool fits.",
      inputSchema: z.object({ sql: z.string() }),
      execute: async ({ sql }) => {
        const q = sql.trim().replace(/;\s*$/, "");
        if (!/^(select|with)\b/i.test(q) || WRITE_SQL.test(q) || q.includes(";")) {
          return { error: "Only a single read-only SELECT/WITH statement is allowed." };
        }
        try {
          const rows = await all(db, q);
          return { rowCount: rows.length, truncated: rows.length > MAX_ROWS, rows: rows.slice(0, MAX_ROWS).map((r) => trimRow(r)) };
        } catch (e) {
          return { error: `SQL error: ${(e as Error).message}. Check column names against the schema.` };
        }
      }
    })
  };
}
