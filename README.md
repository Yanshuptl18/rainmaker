# Take-home: build a screening graph from Indian exchange filings

**Time budget: one weekend, 8–12 hours.** If you spend much longer, stop and tell us what you cut —
knowing what to leave out is part of the job.

## The business problem

We build software for M&A advisers. Before an adviser approaches a buyer, they need to know what
that buyer has actually been doing: what it bought, from whom, for how much, and when. That
evidence sits in public filings — companies tell the exchanges every time they buy a stake, form a
subsidiary, merge, or sell a business — and in the press, which reports the same deals in different
words and sometimes with different numbers.

Today an analyst reads those filings by hand. Your job is a small version of the machine that reads
them instead.

**A word on why this is hard.** An adviser will put your output in front of a client. A number you
invent, or a deal you attribute to the wrong company, is worse than an empty field. Empty is honest;
wrong is a lost client. Design for that.

## What you get

Everything is in `data/`, already fetched for you. No scraping needed.

| | Count | What it is |
|---|---|---|
| `data/filings/FIL-*.txt` | 34 | Real announcements filed with the NSE in Aug–Sep 2026, as text pulled from the filed PDF |
| `data/pdf/FIL-*.pdf` | 34 | The original PDFs, if you prefer to parse them yourself |
| `data/news/NEWS-*.json` | 12 | Google News headlines about some of the same companies (headline, publisher, date, link — no article body) |
| `data/manifest.json` | — | One row per document: `doc_id`, company, subject, date, source URL, file paths |

Some filings describe a transaction. **Some describe nothing of the sort** — a record date, an
investor call, a board appointment. Both kinds are in there on purpose.

## What to build

A small pipeline, driven by a CLI, that does four things:

1. **Extract.** For each document, pull out the transaction it describes: who acquired what from
   whom, the stake, the consideration, the date, and the kind of deal (stake purchase, new
   subsidiary, merger or scheme, slump sale, divestment, joint venture, open offer, land or asset
   purchase). Documents with no transaction must produce nothing.
2. **Load.** Put the results into a temporal knowledge graph using **Graphiti**
   (<https://github.com/getzep/graphiti>), with a backing graph database (a `docker-compose.yml` for
   FalkorDB is included). Model the entities and relationships yourself.
3. **Ask.** Answer the six questions in `QUESTIONS.md` from the graph — not from a hardcoded table,
   and not by re-reading the files at question time. **Every answer must cite the `doc_id`s it rests
   on.**
4. **Tell us what you found.** A short `NOTES.md`: what you'd do differently with another week,
   where your extraction is weakest, what Graphiti did well and badly, and what the run cost.

Hand in a library plus that CLI, with tests. No UI.

## Rules

- **Graphiti is required.** You probably have not used it. That is the point: we want to see how you
  pick up something unfamiliar. Its docs are good; read them before you write code.
- **Every stored value must come from a document.** If the filing does not state the consideration,
  the field is empty. Never estimate, never fill a gap with a plausible number, never let the model
  "reason" a figure into existence.
- **Distinguish what was stated from what you concluded.** A filing that says "the Board approved the
  acquisition" has not said the deal completed. Your schema should be able to tell those apart.
- **No hand-written answers.** If we change a document, your answers should change.
- **Keep secrets out of git.** Use `.env` (see `.env.example`); it is already in `.gitignore`.
- **Use of AI coding tools is fine and expected.** You will be asked about any line of it.

## Getting started

```bash
uv sync                      # Python 3.11+, uv: https://docs.astral.sh/uv/
cp .env.example .env         # put the API key we sent you in here
docker compose up -d         # FalkorDB for Graphiti
uv run screening-graph --help
uv run pytest
```

The API key we send you has a hard spending cap. Running the whole corpus through a model a few
times will not come close to it — but a runaway loop will, so add a limit before you add a retry.

## What we are grading

| Weight | What we look at |
|---|---|
| 30% | **Extraction quality.** Right companies, right numbers, right direction (who bought whom), nothing invented. Decoy documents produce nothing. |
| 20% | **The graph.** Do the entity and relationship types fit the questions? Is the same company one node and not four? Are dates on the facts? |
| 20% | **The answers.** Correct, cited, and genuinely derived from the graph. |
| 20% | **Engineering.** Tests worth having, code we can follow, one command to run it, honest handling of failures. |
| 10% | **Judgement.** `NOTES.md`: where it is weak, what you would do next, what it cost. |

Things that lose marks fast: answers that don't cite documents, invented figures, a README that
claims more than the code does, tests that assert nothing, secrets committed.

**If you run short on time**, cut scope, not honesty: fewer document types handled, but say so in
`NOTES.md`. A small pipeline that works end to end beats a large one that doesn't run.

## Submitting

Push to a private GitHub repo and add the reviewer we name in the email, or send a zip (no
`.venv`, no `.env`). Include `NOTES.md`. Tell us roughly how long you spent.

## Hints, so you spend your time on the interesting part

- Graphiti's API is `async`, and every episode you add costs an LLM call (or several): expect
  seconds per document, not milliseconds. Load once, then query.
- Graphiti needs a model **and** an embedding service; by default it will reach for OpenAI for both.
  Read its configuration docs before your first run.
- Give Graphiti the **date the thing happened**, not the time you ran your script. Its whole point is
  time: facts that were true, and facts that got replaced.
- Decide early whether one company's data is one graph or all companies share one. Both are
  defensible; say why you chose yours.
- Its extraction is not magic. If you hand it raw filing text and hope, you will get a mush of
  entities. Think about what you send it and how you type it.

## About the data

These are real, public filings and headlines, collected in September 2026 for this exercise. They
are included for evaluation only. Each row in `data/manifest.json` carries the source URL so you can
check anything against the original.
