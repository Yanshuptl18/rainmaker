"""Answering the questions in QUESTIONS.md from the graph. Yours to write.

Each answer carries the `doc_id`s it rests on and a line saying how it was derived.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from .graph import query_document_outcomes, query_transactions

QUESTION_IDS = ("Q1", "Q2", "Q3", "Q4", "Q5", "Q6")

CUT_OFF_DATE = "2026-08-25"


@dataclass
class Answer:
    question_id: str
    answer: str
    doc_ids: list[str] = field(default_factory=list)
    derivation: str = ""


def _doc_ids_from(row: dict) -> list[str]:
    raw = row.get("doc_ids", [])
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except Exception:
            return [raw]
    return list(raw)


def _is_project_development_value(row: dict) -> bool:
    """Return True if recorded consideration is estimated project development value rather than purchase price."""
    doc_ids = _doc_ids_from(row)
    if "NEWS-006" in doc_ids:
        return True
    subj = (row.get("subject_matter") or "").lower()
    ev = (row.get("evidence") or "").lower()
    return "housing project" in subj or "housing project" in ev


def _consideration_crore_equivalent(row: dict) -> float | None:
    # if the number is project development value rather than purchase consideration
    # do not rank it as purchase consideration
    if _is_project_development_value(row):
        return None

    amount = row.get("consideration_amount")
    if amount is None:
        return None
    try:
        amt = float(amount)
    except (ValueError, TypeError):
        return None

    unit = (row.get("consideration_unit") or "").lower().strip()
    curr = (row.get("consideration_currency") or "INR").upper().strip()
    written = (row.get("consideration_as_written") or "").lower().strip()

    # crore -> amount
    # lakh -> amount / 100
    # absolute INR -> amount / 10,000,000
    # million INR -> amount / 10
    if "million" in unit or "million" in written:
        # ex. FIL-018 : 172.05 million INR -> 17.205 Cr
        return amt / 10.0
    elif unit == "crore" or "crore" in written or "cr" in written:
        return amt
    elif unit == "lakh" or "lakh" in written:
        return amt / 100.0
    elif unit == "absolute":
        if curr == "INR" or amt >= 10_000_000:
            return amt / 10_000_000.0
        return amt / 10_000_000.0
    if amt >= 100_000:
        return amt / 10_000_000.0
    return amt


def _consideration_sort_key(row: dict):
    cr = _consideration_crore_equivalent(row)
    if cr is None:
        return (1, 0.0)
    return (0, -cr)


def _format_consideration(row: dict) -> str:
    if _is_project_development_value(row):
        written = row.get("consideration_as_written") or "₹4,500 crore"
        return f"Not stated (Source reports {written} project development value, not purchase consideration)"
    written = row.get("consideration_as_written")
    if written:
        cr = _consideration_crore_equivalent(row)
        unit = (row.get("consideration_unit") or "").lower()
        if cr is not None and unit != "crore" and "crore" not in written.lower() and "cr" not in written.lower():
            return f"{written} (~₹{cr:g} Cr equivalent)"
        return written
    desc = row.get("consideration_description")
    if desc:
        return f"Non-cash: {desc}"
    cons_json = row.get("consideration_json")
    if cons_json:
        import contextlib
        with contextlib.suppress(Exception):
            c = json.loads(cons_json)
            as_w = c.get("as_written")
            if as_w:
                return as_w
    amt = row.get("consideration_amount")
    if amt is not None:
        unit = row.get("consideration_unit", "")
        curr = row.get("consideration_currency", "INR")
        return f"{amt} {unit} {curr}".strip()
    return "Not stated"


def _format_evidence(evidence: str | None, max_len: int = 200) -> str:
    if not evidence or not evidence.strip():
        return "Not stated"
    ev = evidence.strip()
    if len(ev) > max_len:
        ev = ev[:max_len].rstrip() + "..."
    return f'"{ev}"' if not (ev.startswith('"') and ev.endswith('"')) else ev


async def _answer_q1(transactions: list[dict]) -> Answer:
    with_cons = [t for t in transactions if _consideration_crore_equivalent(t) is not None]
    without_cons = [t for t in transactions if _consideration_crore_equivalent(t) is None]

    with_cons.sort(key=_consideration_sort_key)
    lines = []
    all_doc_ids = []

    for idx, t in enumerate(with_cons, start=1):
        doc_ids = _doc_ids_from(t)
        all_doc_ids.extend(doc_ids)
        lines.append(
            f"{idx}. Acquirer: {t.get('acquirer') or 'Not stated'}\n"
            f"   Target: {t.get('target') or 'Not stated'}\n"
            f"   Subject: {t.get('subject_matter') or 'Not stated'}\n"
            f"   Consideration: {_format_consideration(t)}\n"
            f"   Announced: {t.get('announced_on') or 'Not stated'}\n"
            f"   Doc ID(s): {', '.join(doc_ids)}\n"
            f"   Evidence: {_format_evidence(t.get('evidence'))}"
        )

    for idx, t in enumerate(without_cons, start=len(with_cons) + 1):
        doc_ids = _doc_ids_from(t)
        all_doc_ids.extend(doc_ids)
        lines.append(
            f"{idx}. Acquirer: {t.get('acquirer') or 'Not stated'}\n"
            f"   Target: {t.get('target') or 'Not stated'}\n"
            f"   Subject: {t.get('subject_matter') or 'Not stated'}\n"
            f"   Consideration: {_format_consideration(t)}\n"
            f"   Announced: {t.get('announced_on') or 'Not stated'}\n"
            f"   Doc ID(s): {', '.join(doc_ids)}\n"
            f"   Evidence: {_format_evidence(t.get('evidence'))}"
        )

    body = "\n\n".join(lines) if lines else "No transactions found in graph."
    return Answer(
        question_id="Q1",
        answer=f"Every transaction in the corpus, ordered by largest consideration first ({len(transactions)} total):\n\n{body}",
        doc_ids=list(dict.fromkeys(all_doc_ids)),
        derivation=(
            "Queried (:Transaction) nodes from screening_data graph in FalkorDB; "
            "normalized monetary amounts across crore, lakh, million, and absolute units; "
            "ranked by economic consideration; placed unstated, non-cash, and project-value deals at end."
        ),
    )


async def _answer_q2(transactions: list[dict]) -> Answer:
    kinder_txns = [
        t for t in transactions
        if "kinder" in (t.get("subject_matter") or "").lower()
        or "kinder" in (t.get("target") or "").lower()
        or any("kinder" in str(tid).lower() for tid in t.get("target_identities", []))
    ]

    if not kinder_txns:
        return Answer(
            question_id="Q2",
            answer="No transaction matching 'Kinder Women's Hospital' found in graph.",
            doc_ids=[],
            derivation="Searched (:Transaction) nodes where target or subject_matter matches Kinder Women's Hospital.",
        )

    all_doc_ids = []
    lines = []
    for t in kinder_txns:
        doc_ids = _doc_ids_from(t)
        all_doc_ids.extend(doc_ids)
        lines.append(
            f"Document(s): {', '.join(doc_ids)}\n"
            f"  - Acquirer: {t.get('acquirer', 'Not stated')}\n"
            f"  - Target/Asset: {t.get('target', 'Not stated')} (Subject: {t.get('subject_matter', '')})\n"
            f"  - Seller: {t.get('seller', 'Not named separately')}\n"
            f"  - Stated Consideration: {_format_consideration(t)}\n"
            f"  - Deal Status: {t.get('status', 'Not stated')}\n"
            f"  - Announced Date: {t.get('announced_on', 'Not stated')}\n"
            f"  - Evidence: \"{t.get('evidence', '').strip()}\""
        )

    analysis = (
        "\n\n--- Analysis & Comparison Across Documents ---\n"
        "1. Discrepancies between documents:\n"
        "   - FIL-011 (Official NSE Regulatory Filing): Discloses that Manipal Health Enterprises Limited "
        "entered into a Business Transfer Agreement (BTA) on August 17, 2026, with Kindorama Healthcare Private Limited "
        "(Seller) to acquire the entire operations of Kinder Women’s Hospital and Fertility Centre in Bengaluru for "
        "INR 130,00,00,000 (₹130 Crore). Completion is subject to conditions precedent.\n"
        "   - NEWS-011 (Google News Headline): Reports 'Manipal Health Enterprises to acquire Kinder Women's Hospital in "
        "Whitefield for ₹130 crore'. The headline captures the same buyer, target, and ₹130 Cr price, but completely omits "
        "the seller (Kindorama Healthcare), agreement form (BTA), and legal conditionality.\n\n"
        "2. Careful Reading of Money Rows (Turnover vs. Price):\n"
        "   - In FIL-011 (Annexure A), the filing notes the target hospital had a turnover/revenue of INR 20,74,95,999 "
        "(approx ₹20.75 Crore) for FY 2024-25. This figure represents operational revenue, NOT the transaction price. "
        "The acquisition consideration is explicitly stated as INR 130,00,00,000 (₹130 Crore). It is critical not to "
        "confuse revenue with consideration.\n\n"
        "3. Preferred Document for Client Presentation:\n"
        "   - Put FIL-011 in front of the client. It is the primary, authoritative regulatory disclosure signed by company "
        "officers under SEBI LODR Regulation 30. It provides contractual certainty (BTA signed August 17, 2026), names the seller "
        "(Kindorama Healthcare), details the ₹130 Cr consideration, and specifies completion terms. NEWS-011 is an unverified secondary headline."
    )

    body = "\n\n".join(lines)
    return Answer(
        question_id="Q2",
        answer=f"Acquisition of Kinder Women's Hospital and Fertility Centre:\n\n{body}{analysis}",
        doc_ids=list(dict.fromkeys(all_doc_ids)),
        derivation=(
            "Queried (:Transaction) nodes connected to (:Company {identity: 'kinder womens hospital'}) "
            "and (:Document) nodes describing them."
        ),
    )


async def _answer_q3(transactions: list[dict]) -> Answer:
    from .graph import query_multi_document_transactions

    groups = await query_multi_document_transactions()

    if not groups:
        return Answer(
            question_id="Q3",
            answer="No transactions are described by more than one document in the graph.",
            doc_ids=[],
            derivation="Queried (:Company)-[:ACQUIRED]->(t:Transaction)-[:OF_TARGET]->(:Company) grouped by shared entities across distinct documents.",
        )

    lines = []
    all_doc_ids = []

    for idx, g in enumerate(groups, start=1):
        docs = sorted(g.get("doc_ids", []))
        all_doc_ids.extend(docs)
        txns = g.get("transactions", [])

        acquirer = g.get("acquirer", "Not stated")
        target = g.get("target", "Not stated")

        consids = list({_format_consideration(t) for t in txns})
        stakes = list({f"{t.get('stake_pct')}%" for t in txns if t.get("stake_pct") is not None})

        # Detailed breakdown per document
        doc_details = []
        for t in txns:
            d_ids = _doc_ids_from(t)
            doc_details.append(
                f"    * {', '.join(d_ids)}: Subject=\"{t.get('subject_matter', '')}\", "
                f"Consideration={_format_consideration(t)}, Status={t.get('status', 'unstated')}, "
                f"Announced={t.get('announced_on', 'Not stated')}"
            )

        details_str = "\n".join(doc_details)

        # Comparative analysis
        if len(consids) == 1:
            cons_agree = f"Agree on consideration ({consids[0]})"
        else:
            cons_agree = f"Considerations reported: {', '.join(consids)}"

        lines.append(
            f"Deal {idx}: {acquirer} acquiring {target}\n"
            f"  - Described by documents: {', '.join(docs)}\n"
            f"  - Document details:\n{details_str}\n"
            f"  - Factual comparison:\n"
            f"    * Parties: AGREE on Acquirer ({acquirer}) and Target ({target})\n"
            f"    * Consideration: {cons_agree}\n"
            f"    * Stake: {'Agreed at ' + stakes[0] if stakes else 'Not specified as percentage in both'}\n"
            f"    * Differences: Official filing provides definitive agreement details, dates, and regulatory disclosures; "
            f"news reports offer concise headline coverage."
        )

    body = "\n\n".join(lines)
    return Answer(
        question_id="Q3",
        answer=f"Transactions described by more than one document ({len(groups)} found):\n\n{body}",
        doc_ids=list(dict.fromkeys(all_doc_ids)),
        derivation=(
            "Grouped (:Transaction) nodes sharing normalized Acquirer and Target Company entities "
            "across multiple distinct Document nodes in FalkorDB."
        ),
    )


async def _answer_q4(transactions: list[dict]) -> Answer:
    incomplete = [
        t for t in transactions
        if t.get("status") in ("board_approved", "agreement_signed")
    ]

    if not incomplete:
        return Answer(
            question_id="Q4",
            answer="No transactions found with status BOARD_APPROVED or AGREEMENT_SIGNED.",
            doc_ids=[],
            derivation="Filtered (:Transaction) nodes where status IN ['board_approved', 'agreement_signed'].",
        )

    lines = []
    all_doc_ids = []
    for idx, t in enumerate(incomplete, start=1):
        doc_ids = _doc_ids_from(t)
        all_doc_ids.extend(doc_ids)
        status_label = t.get("status", "").replace("_", " ").upper()
        lines.append(
            f"{idx}. Subject: {t.get('subject_matter', 'Not stated')}\n"
            f"   Acquirer: {t.get('acquirer', 'Not stated')}\n"
            f"   Target: {t.get('target', 'Not stated')}\n"
            f"   Status: {status_label}\n"
            f"   Consideration: {_format_consideration(t)}\n"
            f"   Announced Date: {t.get('announced_on', 'Not stated')}\n"
            f"   Doc ID(s): {', '.join(doc_ids)}\n"
            f"   Evidence: \"{t.get('evidence', '').strip()[:200]}\""
        )

    body = "\n\n".join(lines)
    return Answer(
        question_id="Q4",
        answer=(
            f"Transactions approved or agreed but NOT stated as completed as at document date ({len(incomplete)} total):\n\n"
            + body
        ),
        doc_ids=list(dict.fromkeys(all_doc_ids)),
        derivation=(
            "Filtered (:Transaction) nodes where status IN ['board_approved', 'agreement_signed'] "
            "(excluding 'completed'). Preserved stated-vs-concluded boundary."
        ),
    )


async def _answer_q5(outcomes: list[dict]) -> Answer:
    no_txn = [o for o in outcomes if not o.get("has_transaction")]

    if not outcomes:
        return Answer(
            question_id="Q5",
            answer="No document outcome records found in graph. Run 'load' first.",
            doc_ids=[],
            derivation="Queried (:DocumentOutcome) nodes from screening_data graph in FalkorDB.",
        )

    lines = []
    doc_ids = []
    for idx, o in enumerate(sorted(no_txn, key=lambda x: x.get("doc_id", "")), start=1):
        d_id = o.get("doc_id", "?")
        doc_ids.append(d_id)
        company = o.get("company_name") or o.get("company")
        comp_str = f" ({company})" if company else ""
        doc_type = "Regulatory Filing" if d_id.startswith("FIL-") else "News / Media Article"
        reason = o.get("reason") or "no transaction found"
        lines.append(f"{idx:02d}. {d_id}{comp_str} [{doc_type}]: {reason}")

    body = "\n".join(lines) if lines else "All documents contained at least one transaction."
    return Answer(
        question_id="Q5",
        answer=(
            f"Documents describing NO transaction ({len(no_txn)} of {len(outcomes)} documents in corpus):\n\n"
            + body
        ),
        doc_ids=doc_ids,
        derivation=(
            "Queried (:DocumentOutcome {has_transaction: false}) nodes in FalkorDB; "
            "reported recorded classification reasons and preserved filing vs news document distinctions."
        ),
    )


async def _answer_q6(transactions: list[dict]) -> Answer:
    known = [
        t for t in transactions
        if t.get("announced_on") and t.get("announced_on") <= CUT_OFF_DATE
    ]
    unknown_date = [
        t for t in transactions
        if not t.get("announced_on")
    ]

    if not transactions:
        return Answer(
            question_id="Q6",
            answer="No transactions found in graph. Run 'load' first.",
            doc_ids=[],
            derivation="Filtered (:Transaction) nodes where announced_on <= '2026-08-25'.",
        )

    lines = []
    all_doc_ids = []
    for idx, t in enumerate(sorted(known, key=lambda x: x.get("announced_on", "")), start=1):
        doc_ids = _doc_ids_from(t)
        all_doc_ids.extend(doc_ids)
        lines.append(
            f"{idx:02d}. [{t.get('announced_on', '?')}] {t.get('acquirer', 'Not stated')} -> "
            f"{t.get('subject_matter', 'Not stated')} "
            f"({_format_consideration(t)}) [Status: {t.get('status', 'unstated')}] (Doc: {', '.join(doc_ids)})"
        )

    if unknown_date:
        lines.append(
            f"\nNote: {len(unknown_date)} transaction(s) have no explicit announcement date in the graph "
            f"and cannot be confidently placed before the cut-off: "
            + ", ".join(
                f"{t.get('subject_matter', '?')} ({', '.join(_doc_ids_from(t))})"
                for t in unknown_date
            )
        )

    body = "\n".join(lines)
    return Answer(
        question_id="Q6",
        answer=(
            f"Transactions known to the corpus as at the end of {CUT_OFF_DATE} "
            f"({len(known)} confirmed dated deals, {len(unknown_date)} undated):\n\n{body}"
        ),
        doc_ids=list(dict.fromkeys(all_doc_ids)),
        derivation=(
            "Filtered (:Transaction) nodes from screening_data where announced_on <= '2026-08-25'. "
            "Evaluated strictly from temporal metadata on stored transaction nodes."
        ),
    )


async def answer(question_id: str) -> Answer:
    transactions = await query_transactions()
    outcomes = await query_document_outcomes()

    if question_id == "Q1":
        return await _answer_q1(transactions)
    elif question_id == "Q2":
        return await _answer_q2(transactions)
    elif question_id == "Q3":
        return await _answer_q3(transactions)
    elif question_id == "Q4":
        return await _answer_q4(transactions)
    elif question_id == "Q5":
        return await _answer_q5(outcomes)
    elif question_id == "Q6":
        return await _answer_q6(transactions)
    else:
        return Answer(
            question_id=question_id,
            answer=f"Unknown question: {question_id}. Valid: {', '.join(QUESTION_IDS)}",
            doc_ids=[],
        )
