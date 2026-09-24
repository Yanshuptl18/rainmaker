# NOTES.md

## What This Project Does

This project finds M&A and other important corporate transactions from Indian exchange filings and news headlines.

It processes 46 documents:

- 34 NSE exchange filings
- 12 news headlines

The system extracts important transaction information such as:

- Acquirer
- Target company
- Stake percentage
- Deal value
- Transaction status
- Announcement date
- Completion date

The extracted information is stored in FalkorDB.

The final six screening questions (Q1-Q6) are answered directly from the graph. The original files are not read again while answering questions.

---

## Pipeline

The project has three main stages.

| Stage | Command | What it does |
|---|---|---|
| Extract | `uv run screening-graph extract` | Uses LLMs to identify transaction documents and extract structured transaction data |
| Load | `uv run screening-graph load` | Loads the extracted data into FalkorDB |
| Ask | `uv run screening-graph ask --all` | Runs all six screening questions using the graph |

The complete flow is:

```text
Documents
    ↓
LLM Classification
    ↓
Transaction Extraction
    ↓
Structured JSON
    ↓
FalkorDB
    ↓
Q1-Q6 Screening Questions
```

---

## Document Classification

The system processes all 46 documents.

The documents are divided into:

| Category                          | Count |
| --------------------------------- | ----: |
| Documents containing transactions |    19 |
| Non-transaction documents         |    27 |
| Total documents                   |    46 |

Some documents contain more than one transaction.

For example, FIL-020 contains two separate transactions, so the final graph contains more transaction records than the number of transaction documents.

Non-transaction documents are not simply deleted.

They are stored as `DocumentOutcome` records with a reason explaining why no transaction was found.

Examples include:

* Promoter share transmission
* Dividend record dates
* Investor meetings
* Board/management changes
* Capital reorganizations
* Other corporate events that are not M&A transactions

---

## LLM Pipeline

The extraction uses two stages.

### Stage 1 - Classification

`gpt-4o-mini` checks each document and determines whether it contains a relevant transaction.

This prevents expensive extraction calls on documents that are clearly not transactions.

### Stage 2 - Extraction

`gpt-4o` extracts the transaction details from documents classified as relevant.

The extraction follows a strict schema and does not guess missing information.

If a value is not present in the source, it is kept empty instead of being invented.

---

## Transaction Deduplication

The system also handles duplicate representations of the same transaction inside a document.

For example, a filing may contain both:

* A board approval section
* A press release describing the same deal

If both describe the same commercial transaction, the system attempts to represent it as one transaction instead of creating duplicate transactions.

The deduplication uses information such as:

* Acquirer
* Target
* Stake percentage
* Consideration
* Document ID
* Transaction details

The system does not merge transactions only because the companies are the same. Different transactions between the same companies are kept separate.

---

## Graph Structure

The structured data is stored in FalkorDB.

Important nodes include:

```text
Company
Transaction
Document
DocumentOutcome
```

Transactions are connected to the relevant companies and source documents.

Company names are normalized so that variations such as:

```text
Manipal Health Enterprises Limited
Manipal Health Enterprises Private Limited
```

can be treated as the same company where appropriate.

---

## Screening Questions

The system answers six questions directly from the FalkorDB graph.

### Q1 - Ranked Transaction Values

Ranks transactions using comparable INR values.

Different units such as:

* Crore
* Lakh
* Million
* Absolute INR values

are converted into comparable values.

The system also avoids treating things such as project development value as purchase consideration.

---

### Q2 - Kinder Women's Hospital

Finds the acquisition involving Kinder Women's Hospital.

The answer combines information from the relevant filing and news document and identifies the actual acquisition consideration.

It also avoids confusing the acquisition price with the target company's revenue.

---

### Q3 - Multi-Document Transactions

Finds transactions that appear across multiple documents.

For example, the same transaction can appear in:

```text
Exchange filing
       +
News report
```

The graph connects these records through the normalized company identities and document information.

---

### Q4 - Approved or Agreed Transactions

Finds transactions that have been:

* Board approved
* Agreement signed

but are not yet marked as completed.

Completed transactions are excluded from this question.

---

### Q5 - Non-Transaction Documents

Finds documents where no transaction was identified.

The system reports the reason stored in the graph.

This makes it possible to distinguish between different corporate events instead of simply saying:

```text
No transaction found
```

---

### Q6 - Transactions Before the Cut-Off Date

Checks transaction announcement dates against:

```text
2026-08-25
```

The results are returned chronologically.

Transactions without an available date are handled separately rather than having a date invented.

---

## Important Design Decisions

### 1. Empty is better than guessing

If the source does not provide a value, the system does not invent one.

For example:

```text
consideration = null
```

is better than guessing a value.

### 2. Source information is preserved

The extracted transaction keeps the original document ID so that the result can be traced back to its source.

### 3. Deterministic graph queries

Q1-Q6 use direct FalkorDB queries.

This makes operations such as:

* Sorting transaction values
* Filtering by dates
* Finding relationships
* Finding non-transaction documents

deterministic.

### 4. Graphiti

Graphiti is used as the semantic/episode layer backed by FalkorDB.

The structured `screening_data` graph is used for the deterministic screening questions.

This hybrid design gives both:

* Graphiti-based semantic storage
* Deterministic structured graph queries

---

## Known Limitations

### LLM extraction

LLM extraction can still be difficult when filings contain complicated tables or multi-stage deals.

Strict schema validation and extraction checks are used to reduce these errors.

If an LLM request fails, the system records the failure instead of creating fake transaction data.

### Company matching

Company deduplication uses deterministic name normalization.

It does not use an external company registry such as CIN or LEI.

Therefore, some unusual company-name variations may still require additional handling.

### News data

Most news documents contain only headlines.

Therefore, the system can only extract information that is actually present in those headlines.

It does not assume additional details that are not stated.

---

## FalkorDB / Graphiti

During development, Graphiti's background index creation could sometimes finish after the main operation had already started shutting down the database connection.

This could produce errors such as:

```text
Connection closed by server
```

The implementation now waits for the relevant background tasks before closing the driver.

The structured FalkorDB graph is kept separate from the Graphiti semantic layer so that deterministic Q1-Q6 queries are not dependent on background semantic indexing.

---

## Testing

The project includes automated tests for the corpus and extraction logic.

Run:

```bash
uv run pytest
```

Expected result:

```text
37 passed
```

Code quality can be checked with:

```bash
uv run ruff check
```

Expected result:

```text
All checks passed
```

---

## Running the Project

Start FalkorDB:

```bash
docker compose up -d
```

Install/sync dependencies:

```bash
uv sync
```

Run tests:

```bash
uv run pytest
```

Run linting:

```bash
uv run ruff check
```

Check the corpus:

```bash
uv run screening-graph corpus
```

Extract transactions:

```bash
uv run screening-graph extract
```

Load the results:

```bash
uv run screening-graph load
```

Run all screening questions:

```bash
uv run screening-graph ask --all
```

---

## Example End-to-End Flow

```text
34 NSE filings + 12 news documents
                ↓
        Document Classification
                ↓
       Transaction Extraction
                ↓
        Structured JSON Data
                ↓
             FalkorDB
                ↓
       Deterministic Graph Queries
                ↓
              Q1-Q6
```

The main goal is to turn unstructured Indian exchange disclosures into structured, traceable transaction data that can be queried reliably.
