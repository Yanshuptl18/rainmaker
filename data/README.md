# The corpus

Real, public documents, collected on 23 September 2026 for this exercise.

| Folder | Count | Source |
|---|---|---|
| `filings/` | 34 | Announcements filed with the National Stock Exchange of India, Aug–Sep 2026. Text extracted from the filed PDF with `pdfplumber`. |
| `pdf/` | 34 | The original filed PDFs. |
| `news/` | 12 | Google News search results (headline, publisher, date, link). Headlines only — no article bodies. |

`manifest.json` has one row per document:

- `doc_id` — `FIL-001` … `FIL-034`, `NEWS-001` … `NEWS-012`. Use these in your answers.
- `kind` — `filing` or `news`
- `symbol`, `company_name`, `subject`, `announced_at`, `isin` — as the exchange published them
- `source_url` — the original file, so any figure can be checked
- `text_file`, `pdf_file` — paths in this repo

Notes that matter:

- **Not every filing describes a transaction.** Several are record dates, investor calls or board
  appointments. That is deliberate.
- **The text is what the PDF gave us.** Headers, addresses and table columns are mangled in places,
  exactly as they are in production.
- **The news items are headlines only.** They are enough to notice that a filing and an article
  describe the same deal, and sometimes disagree.
- The filing text is truncated to the first few pages of long PDFs. The PDF is always the fuller record.
