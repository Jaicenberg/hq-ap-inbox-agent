# AP Inbox Agent

A chat agent over one month of the Eaze accounts-payable inbox (5,705 emails, 24 Aug – 30 Sep 2026, ~9,900 attachment references).
It tells what every email and attachment is, pulls the key fields of every invoice, finds the vendors who chase payment and how far they escalated, checks whether the invoices they chase are actually in the inbox, and says plainly what it could not read or is unsure about.

**Live:** https://hq-ap-inbox-agent.a-mencias99.workers.dev (access code shared separately: the inbox holds real vendor data).
Built entirely on Cloudflare: Workers, Agents SDK (Durable Objects + WebSockets), Workers AI, D1, AI Gateway.

---

## How it works

```
  OFFLINE, run once (pipeline/, Python, stdlib only)                     ONLINE (src/, Cloudflare Worker)
  ─────────────────────────────────────────────────                      ──────────────────────────────────
  ap_inbox_candidate.sql ─ load_sql.py ─► raw.sqlite                     Browser chat UI (React)
  attachments.zip ──────── extract_text.py ─► text + read status              │  WebSocket (realtime, streaming)
        │  pdftotext (free, local) for 7,424 files                            ▼
        │  scans/images flagged for vision                               ChatAgent  = AIChatAgent Durable Object
        ▼                                                                 · conversation persisted in its SQLite
  classify.py  ── Workers AI (via AI Gateway) ──►  type + fields          · GLM-5.3 (Workers AI) with 9 tools
        │  emails: 8 per call · docs: 1 per unique text · scans: vision   · instructions = agent/SOUL.md + AGENTS.md
  judge.py     ── stronger model re-labels a blind sample ──► accuracy    · skills loaded on demand (agent/skills)
  build.py     ── rules cross-check, self-checks, dedupe, vendor merge,         │  read-only SQL / FTS5
                  chase matching ──► out.sql ──wrangler d1 execute──►    D1 database  `ap-inbox`
```

The heavy lifting (reading 8,600 files, ~10k model calls) happens once, offline. The chat agent then answers from a clean, verified table set instead of re-reading PDFs per question: answers take seconds and cost cents, and every number can be traced to a row and a file.

### How it reads the files (`pipeline/extract_text.py`, `classify.py`)
| file | how | result |
|---|---|---|
| PDF with a text layer | `pdftotext -layout` locally | `read_status = ok` (7,424 files) |
| Scanned PDF / image (no text layer) | page 1 rendered to JPEG → vision model (Gemma 4 / Llama 4 Scout) transcribes and extracts | `ok_vision`, always flagged "verify" (1,092 files) |
| XLSX / DOCX / CSV / ICS | standard-library readers | `ok` |
| Password-protected PDF | cannot open | `password`, reported (8) |
| Referenced by an email, absent from the zip | — | `missing`, reported (82 business files + 1,267 inline logos) |
| Corrupt / zip-in-zip / vision failed | — | `error` / `unsupported` / `needs_ocr`, reported (30) |

Nothing is dropped silently: **every** attachment referenced in the database gets a row with `read_status`, `read_method` and a human-readable `read_note`.

### What it extracts
- Every email → `invoice | statement | payment_reminder | remittance | other`, vendor, chase level (`reminder` → `escalation` → `final_notice`), the invoice numbers it chases, amount due.
- Every attachment → the same types; for invoices: vendor, invoice number, invoice/due date, PO, terms, currency, subtotal, tax, total, amount due, bill-to.
- Duplicates (the same invoice forwarded 2–5×) are grouped; `is_primary` marks the first copy.
- Vendor name variants are merged ("Leafly" / "Leafly Holdings, Inc") by name prefix and shared sender domain.
- Chasing: each invoice a vendor chases is matched (normalised number) against invoices in the inbox → `invoice in inbox` / `mentioned elsewhere, no invoice document` / `not in inbox` / `no invoice number given`.

### How it checks itself
1. **Grounding**: is the extracted invoice number / total literally present in the PDF text? (catches hallucinated fields)
2. **Consistency**: subtotal + tax = total; dates valid; due ≥ issue date; required fields present.
3. **Second opinion**: an independent keyword-rule classifier runs on every item; disagreements with the model go to the review queue.
4. **Confidence**: model confidence < 0.7 is flagged.
5. **Judge**: a stronger model (GLM-5.3, reasoning on) re-labels a random sample blind; agreement is reported with a 95% confidence interval. (Model-vs-model, not human-labelled; the agent says so.)
6. **Coverage**: counts of files read by text layer, by vision, missing, password-protected, unreadable.

All of it lands in the `quality` table; the agent's `quality_report` and `review_queue` tools expose it, and the `data-quality` skill tells the agent how to explain it.

---

## Models and why

| job | model (Workers AI) | why |
|---|---|---|
| Chat agent (tool calling) | `@cf/zai-org/glm-5.3`, reasoning `low` | Strong agentic/tool-use model; benchmarked fastest of the frontier candidates on this account (1.7 s per tool step vs 8.5 s Kimi K2.6); 1M context. |
| Chat fallback | `deepseek-v4.1-flash` | If GLM-5.3 is rate-limited or down, the same step is retried here (AI SDK middleware), so the user never gets a silent failure. |
| Batch: emails | `deepseek-v4.1-flash` + `gpt-oss-120b` | Fast, good at strict JSON; split across two models because each model has its own capacity/rate limit. |
| Batch: documents | `gemma-4-26b-a4b-it` + `glm-4.7-flash` | Cheap ($0.06–0.10 / M input) and accurate on invoices; thinking **disabled** (`enable_thinking: false`) → 9× faster, ~30× fewer output tokens. |
| Batch: scans | `gemma-4-26b-a4b-it` + `llama-4-scout` (vision) | Same prompt, reads the rendered page image; outputs matched on spot checks. |
| Judge | `@cf/zai-org/glm-5.3` (reasoning on) | Stronger than the batch models; blind re-labelling of a sample. |

All calls go through **AI Gateway** (`ap-agent`): per-call logs, latency, token and cost analytics, and it avoids the Cloudflare REST API's per-user rate limit during the batch.

## How accurate it is

| measure | result |
|---|---|
| Invoice number found verbatim in its PDF (text-layer invoices) | 97.2% (n=3,100) |
| Invoice total found verbatim in its PDF | 97.9% (n=3,100) |
| Subtotal + tax = total (where all three printed) | 91.8% (n=1,267) |
| Judge: email type (n=79) | 89.9% (95% CI 81–95%) |
| Judge: attachment type (n=75) | 98.7% (95% CI 93–100%) |
| Judge: invoice number / total / date (n=55) | 98.2% / 100% / 96.4% |
| Judge: chase level (n=6, too small to conclude) | 6/6 |

Most email disagreements sit on a genuine boundary: delivery/shipping notices that *carry* an invoice ("invoice" vs "other"). Every item that fails a check is in the review queue with its reason, and the agent mentions it when it uses that data.

## What it costs

| item | cost | notes |
|---|---|---|
| One-off processing of the whole inbox | **$2.72** | ~11k Workers AI calls (emails batched 8 per call, documents deduplicated by content hash: 6,818 text files → 4,277 unique) |
| Accuracy judge (160 blind samples, reasoning on) | $1.02 | optional, re-run when prompts/models change |
| Per chat question | ~$0.02–0.06 | GLM-5.3, 2–5 tool steps of ~2–11k input tokens each (measured in AI Gateway) |
| Workers, Durable Objects, D1, AI Gateway | ~$0 at this scale | inside the Workers Paid plan ($5/month) allowances |
| Steady state for this inbox | ≈ $4/month + chat usage | a month of new mail ≈ one batch run |

Design choices that keep it cheap: free local text extraction first (vision only for the 13% of PDFs with no text layer), thinking disabled for extraction, deduplication before any model call, and answering chat questions from pre-extracted tables instead of re-reading documents.

---

## Repository

```
agent/                 the agent's brain (imported verbatim into the system prompt)
  SOUL.md              persona: evidence first, honest about limits
  AGENTS.md            operating instructions + full database schema
  skills/*/SKILL.md    payment-chasing, invoice-lookup, data-quality (loaded on demand via load_skill)
src/
  server.ts            ChatAgent (AIChatAgent Durable Object), model + gateway wiring, access-code gate
  tools.ts             the 9 tools: inbox_overview, vendor_chasing, get_invoice, get_email, search_inbox,
                       quality_report, review_queue, run_sql (read-only), load_skill
  app.tsx              chat UI (Cloudflare agents-starter, trimmed)
pipeline/              offline processing (Python 3.11, standard library only)
  load_sql.py          Postgres COPY dump → SQLite (no Postgres needed)
  extract_text.py      every attachment → text + read status
  classify.py          Workers AI classification / extraction (resumable, sharded across models)
  run_batch.sh         runs classify.py over 5 model shards in parallel
  judge.py             blind accuracy sample with a stronger model
  build.py             rules cross-check, self-checks, dedupe, vendor merge, chase matching → D1 SQL
  schema.sql, fts.sql  D1 schema + full-text index
.github/workflows/deploy.yml   push to main → typecheck → build → wrangler deploy
```

## Run it

**Prerequisites:** Node 22, Python 3.11, poppler (`pdftotext`, `pdftoppm`), a Cloudflare account with Workers Paid, an API token (Workers AI, D1, AI Gateway, Workers Scripts).

```bash
# 0. secrets (never committed)
echo "CLOUDFLARE_API_TOKEN=..." > .env
echo "AI_GATEWAY_ID=ap-agent"   >> .env
echo "ACCESS_CODE=choose-one"   > .dev.vars

# 1. offline pipeline (D = a working folder outside the repo)
python -I pipeline/load_sql.py ap_inbox_candidate.sql $D/raw.sqlite
unzip attachments.zip -d $D/attachments
python -I pipeline/extract_text.py $D/raw.sqlite $D/attachments $D/work.sqlite
pipeline/run_batch.sh $D/raw.sqlite $D/work.sqlite $D/attachments      # resumable; re-run to retry failures
python -I pipeline/judge.py $D/raw.sqlite $D/work.sqlite 80
python -I pipeline/build.py $D/raw.sqlite $D/work.sqlite $D/out.sqlite $D/out.sql
cat pipeline/fts.sql >> $D/out.sql

# 2. load D1 and run
npx wrangler d1 create ap-inbox                  # once; put the id in wrangler.jsonc
npx wrangler d1 execute ap-inbox --remote --file $D/out.sql
npm install
npm run dev                                      # http://localhost:5173/?code=<ACCESS_CODE>

# 3. deploy (CI does this on every push to main)
npx wrangler secret put ACCESS_CODE
npm run deploy
```

## Limits (stated, not hidden)
- Scanned documents: only page 1 is read; flagged for verification.
- Accuracy is measured by self-checks and a model judge, not a human-labelled set.
- The database keeps the first 3,000 chars of each email and 4,000 of each attachment; very long documents can be cut.
- Vendor merging is heuristic; the agent mentions which spelling it matched.
- The chat is read-only by design: it cannot pay, reply or edit.
