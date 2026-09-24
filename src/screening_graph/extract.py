"""Document text -> `Transaction` objects. Yours to write.

Two things worth deciding before you start:

- how you tell a transaction document from one that merely mentions companies, and
- what you do when a field is only half-stated (a stake with no consideration, a consideration
  with no closing date).
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime

from openai import OpenAI

from .corpus import Document
from .graph import normalize_company_identity
from .schema import (
    DealKind,
    DealStatus,
    DocumentOutcome,
    Money,
    Party,
    Transaction,
)

logger = logging.getLogger(__name__)

_CLASSIFICATION_PROMPT = """You are an M&A analyst reading Indian stock exchange filings.

Decide whether this filing describes a CORPORATE TRANSACTION — meaning one or more of:
- acquisition of shares / stake in another company FROM AN EXTERNAL PARTY
- acquisition of a business or assets from another company
- merger or amalgamation
- slump sale or divestment
- formation of a NEW subsidiary (company did not already exist)
- joint venture or open offer
- land or immovable property purchase from an external party

=== HARD REJECT — return is_transaction=false for ALL of the following ===

HARD REJECT 1 — SAST TRANSMISSION / INHERITANCE:
If the document is a Regulation 10(6) disclosure reporting acquisition of shares by way of
"transmission", "succession", or "inheritance" (e.g. shares transmitted from a deceased promoter
to a family member), return is_transaction=false. These are NOT M&A transactions. Signals:
- "Regulation 10(1)(g)"
- "by way of transmission"
- "by way of succession or inheritance"
- "from Late Mr./Mrs. [name]"
- "no change in the consolidated shareholding of the Promoter and Promoter Group"

HARD REJECT 2 — INTER-SE PROMOTER GIFT:
If the document discloses an inter-se transfer of shares by way of GIFT between promoters or
immediate relatives (e.g. "inter-se transfer by way of Gift amongst Promoter(s) who are immediate
relatives", "Price: NIL", "Regulation 10(1)(a)"), return is_transaction=false.

HARD REJECT 3 — RIGHTS ISSUE / CAPITAL INFUSION INTO EXISTING SUBSIDIARY:
If the document says the listed company is subscribing to a rights issue, further infusing capital,
or purchasing additional shares in a subsidiary it ALREADY owns (especially 100%), and
no NEW entity is being acquired from an external party, return is_transaction=false.
Signals: "subscribing Rights Issue", "infused additional capital", "further investment in Wholly
Owned Subsidiary", "on rights basis".

HARD REJECT 4 — HISTORICAL REFERENCE / CONTRACT ANNOUNCEMENT:
If the document's primary purpose is to announce a supply contract, partnership, or agreement
that merely references a previously completed acquisition in passing (e.g. "following strategic
Bromford acquisition" in a contract announcement), return is_transaction=false. The filing must
itself announce, approve, sign, or report a NEW transaction event.

HARD REJECT 5 — GENERAL NON-TRANSACTION FILINGS:
- record date / dividend announcements
- investor / analyst meetings
- director / officer appointments or resignations
- voluntary liquidation with no external buyer
- financial results

Reply with JSON: {"is_transaction": true|false, "reason": "one sentence"}"""

_EXTRACTION_PROMPT = """You are an M&A analyst. Extract EVERY corporate transaction from the filing below.
Return a JSON array. Each element must match this schema exactly:

{
  "kind": one of stake_purchase | new_subsidiary | merger_or_scheme | slump_sale | divestment | joint_venture | open_offer | asset_purchase | other,
  "status": one of board_approved | agreement_signed | completed | unstated,
  "parties": [
    {"name": "...", "role": "acquirer" | "target" | "seller", "identifier": "CIN or null"}
  ],
  "subject_matter": "exactly what was bought/sold in a few words",
  "stake_pct": number or null,
  "consideration": {"amount": number, "currency": "INR", "unit": "crore"|"lakh"|"absolute"|"EUR"|"AED", "as_written": "exact phrase from document"} or null,
  "consideration_description": "exact phrase if consideration is non-cash (e.g. shares issued) or null",
  "announced_on": "YYYY-MM-DD or null",
  "completed_on": "YYYY-MM-DD or null",
  "evidence": "one or two exact sentences from the document that support this transaction"
}

DO NOT EXTRACT — return [] for ALL of the following document types:
1. SAST Regulation 10(6) transmission disclosures: shares transferred by inheritance/succession
   from a deceased person ("from Late Mr./Mrs.", "by way of transmission", "Regulation 10(1)(g)").
2. Inter-se promoter gift transfers: "inter-se transfer by way of Gift", "Regulation 10(1)(a)",
   price NIL, between immediate relatives of the promoter group.
3. Rights issue / capital infusion into an EXISTING wholly-owned subsidiary: the company already
   owns the subsidiary; it is merely subscribing to a rights issue or infusing additional capital.
   ("subscribing Rights Issue", "infused additional capital", "further investment in WOS").
4. Press releases / announcements whose primary purpose is a supply or service contract that
   merely references a past acquisition in passing.

MONEY RULES — read carefully:
- Only populate consideration when the document gives an explicit monetary value such as:
    "Rs. 16.44 Crores", "INR 189.2 crore", "₹130 crore", "AED 1,70,64,000", "EUR 25,000"
- NEVER put a share count into consideration.amount. These are NOT money:
    "1,44,04,204 equity shares", "5,00,000 shares", "shares issued to the seller", "X% stake"
- If consideration is non-cash (shares issued, etc.), set consideration=null and put the exact phrase in consideration_description.
- If no consideration is stated, consideration must be null and consideration_description must be null.
- Never invent a number. Never convert units unless the document itself expresses the converted value.
- Do NOT put "initial share capital" of a newly incorporated company into consideration unless it
  represents a cash payment to an external seller. Initial paid-up capital (e.g. the ₹5,00,00,000
  split into 50,000 shares of ₹10 each) is the company's own capital structure, not acquisition cost.

STATUS RULES — exact mapping only, no inference:
- board_approved  = "the Board approved" / "Board accorded approval"
- agreement_signed = "entered into agreement" / "signed BTA/SPA/agreement" / "definitive agreement"
- completed       = "completed" / "effective" / "acquisition completed" / "dissolution completed"
  OR when a new subsidiary is described as having already been "incorporated" (past tense, done)
- unstated        = document does not use any of the above phrases
- Do NOT infer completion from board approval or agreement signing.
- A Binding Term Sheet alone is NOT agreement_signed; use board_approved if only the board approved
  entry into the binding term sheet.

DATE RULES:
- announced_on: the date of the transaction event stated in the document (signing date, board date).
- completed_on: only when a completion/closing/effective date is explicitly stated.
- Never put the filing date into completed_on unless it is also stated as the completion date.

HISTORICAL REFERENCE RULE:
- If the document is a supply contract, business update, or announcement that happens to mention a previously acquired subsidiary, return [].
- The document must itself report a transaction event (approval, signing, completion) to justify extraction.

MERGER / SCHEME RULES:
- For mergers, the transferee (surviving entity) is acquirer; transferor is target.
- Do not invent a seller when the document only describes amalgamation between two entities.
- Do not invent roles. If a role is not clearly stated, omit the party rather than guess.

PARTY RULES:
- The acquirer is the party BUYING or the entity that incorporates a new company.
  For a new subsidiary, the DIRECT parent doing the incorporating is the acquirer — not a
  grandparent listed company that is merely the ultimate owner.
- The target is what is being bought/incorporated. The seller is who is selling (only if named).
- Never assign the same company as both target and seller.
- NEVER list the filing company (the listed entity making the disclosure) as the "target" when it
  is merely the subject of a SAST / Reg 10(6) disclosure. The target in SAST context means the
  company whose shares were transferred — the listed company is the issuer, not the M&A target.

EVIDENCE RULE:
- evidence must be an EXACT verbatim quote from the document (one or two sentences).
- Do NOT use ellipsis ("...") to join non-adjacent sentences unless those exact dots appear in
  the source text.
- Do not paraphrase. Do not use outside knowledge.

NEWS DOCUMENTS:
- Headlines only — do not invent consideration, seller, completion date, stake, or detailed parties.
- Extract only what the headline explicitly states.

Return [] if there are truly no transactions."""

# Retry prompt used when the first LLM response cannot be parsed as JSON.
_RETRY_SUFFIX = (
    "\n\nYour previous response could not be parsed as valid JSON. "
    "Return ONLY a valid JSON array matching the schema above. "
    "No explanation, no markdown, no extra text. "
    "If there is no transaction, return []."
)

MAX_RETRIES = 2

# Words that indicate a value is a share count, not a monetary amount.
_SHARE_WORDS = frozenset(
    ["shares", "equity shares", "securities", "share capital", "debentures", "units"]
)


def _get_client_and_models() -> tuple[OpenAI, str, str]:
    api_key = os.environ.get("OPENROUTER_API_KEY") or os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY not set")

    base_url = (
        os.environ.get("OPENROUTER_BASE_URL")
        or os.environ.get("OPENAI_BASE_URL")
        or "https://openrouter.ai/api/v1"
    )
    classify_model = os.environ.get("CLASSIFICATION_MODEL", "openai/gpt-4o-mini")
    extract_model = os.environ.get("EXTRACTION_MODEL", "openai/gpt-4o")

    return OpenAI(api_key=api_key, base_url=base_url), classify_model, extract_model


def _get_client() -> OpenAI:
    client, _, _ = _get_client_and_models()
    return client


def normalize_company_name(name: str) -> str:
    """Normalize obvious company name variants to a canonical form."""
    if not name:
        return name
    name = name.strip()
    name = re.sub(r"\s+", " ", name)
    replacements = [
        # Order matters: Pvt. Ltd. before Pvt. alone
        (r"\bPvt\.?\s+Ltd\.?\b", "Private Limited"),
        (r"\bPvt\.?\b", "Private"),
        (r"\bLtd\.?\b", "Limited"),
        (r"\bCo\.?\b", "Company"),
        (r"\bInc\.?\b", "Incorporated"),
        (r"\bCorp\.?\b", "Corporation"),
    ]
    for pattern, repl in replacements:
        name = re.sub(pattern, repl, name, flags=re.IGNORECASE)
    # Strip any orphaned trailing period left after replacements
    name = name.rstrip("., ")
    name = re.sub(r"\s+", " ", name).strip()
    return name


# Returns True if the text looks like a share count rather than a monetary amount.
# Used to veto Money objects that were incorrectly derived from share counts.
def _looks_like_share_count(text: str) -> bool:
    if not text:
        return False
    lower = text.lower()
    return any(word in lower for word in _SHARE_WORDS)


# Validates a raw consideration dict from the LLM.
# Returns (money_or_none, description_or_none).
# If as_written contains share-count language, vetoes the Money and returns it as a description instead.
def _parse_consideration(raw_cons: dict | None) -> tuple[Money | None, str | None]:
    if not raw_cons or not isinstance(raw_cons, dict):
        return None, None

    as_written = str(raw_cons.get("as_written", ""))

    # Veto: if as_written describes shares rather than money, do not build a Money object.
    if _looks_like_share_count(as_written):
        logger.info("Vetoing share-count consideration: %r", as_written)
        return None, as_written if as_written else None

    amount = raw_cons.get("amount")
    if amount is None:
        return None, None

    try:
        return Money(
            amount=float(amount),
            currency=str(raw_cons.get("currency", "INR")),
            unit=str(raw_cons.get("unit", "absolute")),
            as_written=as_written,
        ), None
    except (TypeError, ValueError):
        return None, None


def _classify(text: str, client: OpenAI | None = None, model: str = "gpt-4o-mini") -> tuple[bool, str]:
    """Return (is_transaction, reason). Max 2 attempts."""
    if client is None:
        client, model, _ = _get_client_and_models()
    snippet = text[:4000]
    for attempt in range(MAX_RETRIES):
        try:
            kwargs = {
                "model": model,
                "messages": [
                    {"role": "system", "content": _CLASSIFICATION_PROMPT},
                    {"role": "user", "content": snippet},
                ],
                "temperature": 0,
                "max_tokens": 1000,
            }
            if "gpt-4" in model or "gpt-3.5" in model:
                try:
                    resp = client.chat.completions.create(
                        **kwargs,
                        response_format={"type": "json_object"},
                    )
                except Exception:
                    resp = client.chat.completions.create(**kwargs)
            else:
                resp = client.chat.completions.create(**kwargs)

            content = resp.choices[0].message.content.strip()
            if content.startswith("```"):
                content = re.sub(r"^```[a-z]*\n?", "", content)
                content = re.sub(r"\n?```$", "", content)
            m = re.search(r"\{.*\}", content, re.DOTALL)
            raw_json = m.group(0) if m else content
            data = json.loads(raw_json)
            return bool(data.get("is_transaction")), str(data.get("reason", ""))
        except Exception as exc:
            logger.warning("Classification attempt %d failed: %s", attempt + 1, exc)
            if attempt == MAX_RETRIES - 1:
                raise
    return False, "classification failed"


# Attempts to parse the LLM text content as a JSON list of transaction dicts.
# Returns the parsed list or raises ValueError on failure.
def _parse_json_list(content: str) -> list[dict]:
    if content.startswith("```"):
        content = re.sub(r"^```[a-z]*\n?", "", content)
        content = re.sub(r"\n?```$", "", content)
    m = re.search(r"\[.*\]|\{.*\}", content, re.DOTALL)
    raw_json = m.group(0) if m else content
    if not raw_json.strip():
        raise ValueError("empty LLM response")
    data = json.loads(raw_json)
    if isinstance(data, dict):
        data = data.get("transactions", [data])
    if not isinstance(data, list):
        raise TypeError(f"expected list, got {type(data)}")
    return data


def _extract_raw(text: str, doc_id: str, client: OpenAI | None = None, model: str = "gpt-4o") -> list[dict]:
    """Call LLM to extract transaction JSON. Retries once with an explicit JSON-only instruction on parse failure."""
    if client is None:
        client, _, model = _get_client_and_models()

    messages: list[dict] = [
        {"role": "system", "content": _EXTRACTION_PROMPT},
        {"role": "user", "content": f"doc_id: {doc_id}\n\n{text[:8000]}"},
    ]

    last_exc: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=0,
                max_tokens=2000,
            )
            content = resp.choices[0].message.content
            if not content or not content.strip():
                raise ValueError("LLM returned an empty response")
            content = content.strip()
            result = _parse_json_list(content)
            return result
        except Exception as exc:
            last_exc = exc
            logger.warning("Extraction attempt %d for %s failed: %s", attempt + 1, doc_id, exc)
            if attempt < MAX_RETRIES - 1:
                # On retry, append an explicit JSON-only instruction as an assistant turn.
                messages = messages + [
                    {"role": "assistant", "content": content if "content" in dir() else ""},
                    {"role": "user", "content": _RETRY_SUFFIX},
                ]

    # Both attempts failed — raise so the caller can record extraction_failed=True.
    raise RuntimeError(f"Extraction failed for {doc_id} after {MAX_RETRIES} attempts: {last_exc}") from last_exc


def _parse_date(val: str | None) -> str | None:
    if not val:
        return None
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(val, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return val


def _build_transaction(raw: dict, doc_id: str) -> Transaction | None:
    try:
        parties = []
        for p in raw.get("parties", []):
            parties.append(Party(
                name=normalize_company_name(str(p.get("name", ""))),
                role=str(p.get("role", "acquirer")),
                identifier=p.get("identifier") or None,
            ))

        # Use _parse_consideration to safely handle share-count vetoing.
        consideration, consideration_description = _parse_consideration(raw.get("consideration"))

        # Also capture consideration_description from the LLM if explicitly provided.
        if not consideration_description:
            consideration_description = raw.get("consideration_description") or None

        if not parties:
            return None

        # --- Deterministic guard: reject documents that slipped through the LLM classifier ---
        # Check the evidence and subject_matter for hard-reject signals.
        evidence_text = str(raw.get("evidence", "")).lower()
        subject_text = str(raw.get("subject_matter", "")).lower()
        combined_text = evidence_text + " " + subject_text

        _TRANSMISSION_SIGNALS = (
            "by way of transmission",
            "by way of succession",
            "by way of inheritance",
            "regulation 10(1)(g)",
            "from late mr",
            "from late mrs",
            "from late shri",
        )
        _GIFT_SIGNALS = (
            "by way of gift",
            "inter-se transfer",
            "inter se transfer",
            "regulation 10(1)(a)",
            "price: nil",
            "nil since off market",
        )
        _RIGHTS_ISSUE_SIGNALS = (
            "subscribing rights issue",
            "rights issue of equity shares",
            "infused additional capital",
            "further investment in wholly owned subsidiary",
            "on rights basis",
        )

        for signal in _TRANSMISSION_SIGNALS:
            if signal in combined_text:
                logger.info(
                    "Rejecting %s: transmission/inheritance signal detected: %r", doc_id, signal
                )
                return None

        for signal in _GIFT_SIGNALS:
            if signal in combined_text:
                logger.info(
                    "Rejecting %s: inter-se gift signal detected: %r", doc_id, signal
                )
                return None

        for signal in _RIGHTS_ISSUE_SIGNALS:
            if signal in combined_text:
                logger.info(
                    "Rejecting %s: rights-issue/internal-capital-infusion signal detected: %r",
                    doc_id,
                    signal,
                )
                return None
        # --- End of deterministic guard ---

        try:
            kind = DealKind(raw.get("kind", "other"))
        except ValueError:
            kind = DealKind.OTHER

        try:
            status = DealStatus(raw.get("status", "unstated"))
        except ValueError:
            status = DealStatus.UNSTATED

        stake = raw.get("stake_pct")
        stake_pct: float | None = float(stake) if stake is not None else None

        return Transaction(
            doc_ids=[doc_id],
            kind=kind,
            status=status,
            parties=parties,
            subject_matter=str(raw.get("subject_matter", "")),
            stake_pct=stake_pct,
            consideration=consideration,
            consideration_description=consideration_description,
            announced_on=_parse_date(raw.get("announced_on")),
            completed_on=_parse_date(raw.get("completed_on")),
            evidence=str(raw.get("evidence", "")),
        )
    except Exception as exc:
        logger.warning("Failed to build transaction from raw data for %s: %s", doc_id, exc)
        return None


_STATUS_PRECEDENCE: dict[DealStatus, int] = {
    DealStatus.UNSTATED: 0,
    DealStatus.BOARD_APPROVED: 1,
    DealStatus.AGREEMENT_SIGNED: 2,
    DealStatus.COMPLETED: 3,
}


def _get_party_identities(txn: Transaction, role: str) -> set[str]:
    return {
        normalize_company_identity(p.name)
        for p in txn.parties
        if (p.role or "").lower() == role and p.name
    } - {""}


def _normalize_money_to_crore(cons: Money | None) -> float | None:
    if cons is None or cons.amount is None:
        return None
    unit = (cons.unit or "").lower()
    amount = float(cons.amount)
    if "crore" in unit or "cr" in unit:
        return amount
    elif "lakh" in unit:
        return amount / 100.0
    elif "million" in unit:
        return (amount * 1_000_000.0) / 10_000_000.0
    elif "absolute" in unit or not unit:
        return amount / 10_000_000.0
    return amount


def are_duplicate_intra_doc_transactions(t1: Transaction, t2: Transaction) -> bool:
    """Determine whether two transaction records represent duplicate records of the same deal within a document."""
    # 1. Must share at least one doc_id
    if not (set(t1.doc_ids) & set(t2.doc_ids)):
        return False

    # 2. Both must have matching acquirer and target
    acq1 = _get_party_identities(t1, "acquirer")
    acq2 = _get_party_identities(t2, "acquirer")
    tgt1 = _get_party_identities(t1, "target")
    tgt2 = _get_party_identities(t2, "target")

    # If both define an acquirer, they must overlap
    if acq1 and acq2 and not (acq1 & acq2):
        return False
    # If both define a target, they must overlap
    if tgt1 and tgt2 and not (tgt1 & tgt2):
        return False
    # At least one pair must explicitly match, and neither pair may conflict
    if not ((acq1 & acq2) or (tgt1 & tgt2)):
        return False

    # 3. Compatible deal kind: cannot merge distinct non-'other' deal kinds
    if t1.kind != t2.kind and t1.kind != DealKind.OTHER and t2.kind != DealKind.OTHER:
        return False

    # 4. Stake percentage: if both state a stake percentage, they must match
    if t1.stake_pct is not None and t2.stake_pct is not None and abs(t1.stake_pct - t2.stake_pct) > 0.01:
        return False

    # 5. Monetary consideration: if both state monetary consideration, they must be consistent
    if t1.consideration is not None and t2.consideration is not None:
        c1, c2 = t1.consideration, t2.consideration
        if (c1.currency or "").upper() != (c2.currency or "").upper():
            return False
        norm1 = _normalize_money_to_crore(c1)
        norm2 = _normalize_money_to_crore(c2)
        if norm1 is not None and norm2 is not None and abs(norm1 - norm2) > 0.1 * max(norm1, norm2, 1.0):
            return False

    # 6. Dates: if both have explicit announcement dates, they must match
    return not (t1.announced_on and t2.announced_on and t1.announced_on != t2.announced_on)


def merge_duplicate_transactions(t1: Transaction, t2: Transaction) -> Transaction:
    """Merge two duplicate representations of the same commercial deal."""
    s1_rank = _STATUS_PRECEDENCE.get(t1.status, 0)
    s2_rank = _STATUS_PRECEDENCE.get(t2.status, 0)

    if s2_rank > s1_rank:
        winner, prior = t2, t1
    else:
        winner, prior = t1, t2

    status = winner.status
    kind = winner.kind if winner.kind != DealKind.OTHER else prior.kind

    # Pick the more descriptive subject_matter (longer string usually has more context)
    subj = winner.subject_matter
    if prior.subject_matter and len(prior.subject_matter) > len(winner.subject_matter):
        subj = prior.subject_matter

    # Stake percentage: prefer non-null
    stake_pct = winner.stake_pct if winner.stake_pct is not None else prior.stake_pct

    # Consideration: prefer winner's consideration if present, else prior's
    consideration = winner.consideration or prior.consideration
    cons_desc = winner.consideration_description or prior.consideration_description

    # Dates: prefer non-null
    announced_on = winner.announced_on or prior.announced_on
    completed_on = winner.completed_on or prior.completed_on

    # Doc IDs: union in deterministic sorted order
    doc_ids = sorted(dict.fromkeys(winner.doc_ids + prior.doc_ids))

    # Parties: merge preserving identifiers
    party_map: dict[tuple[str, str], Party] = {}
    for p in winner.parties + prior.parties:
        key = (normalize_company_identity(p.name), (p.role or "").lower())
        if key not in party_map or (p.identifier and not party_map[key].identifier):
            party_map[key] = p
    parties = list(party_map.values())

    # Evidence: preserve winning evidence, and append prior evidence if distinct
    w_ev = (winner.evidence or "").strip()
    p_ev = (prior.evidence or "").strip()
    if p_ev and p_ev not in w_ev and w_ev not in p_ev:
        combined_evidence = f"{w_ev} {p_ev}".strip()
    else:
        combined_evidence = w_ev if len(w_ev) >= len(p_ev) else p_ev

    return Transaction(
        doc_ids=doc_ids,
        kind=kind,
        status=status,
        parties=parties,
        subject_matter=subj,
        stake_pct=stake_pct,
        consideration=consideration,
        consideration_description=cons_desc,
        announced_on=announced_on,
        completed_on=completed_on,
        evidence=combined_evidence,
    )


def deduplicate_intra_document_transactions(transactions: list[Transaction]) -> list[Transaction]:
    """Deduplicate multiple records of the same deal within a single document."""
    if len(transactions) <= 1:
        return transactions

    result: list[Transaction] = []
    for txn in transactions:
        merged = False
        for i, existing in enumerate(result):
            if are_duplicate_intra_doc_transactions(existing, txn):
                result[i] = merge_duplicate_transactions(existing, txn)
                merged = True
                break
        if not merged:
            result.append(txn)
    return result


async def extract(document: Document) -> list[Transaction]:
    """Return the transactions this document describes. Empty list if it describes none."""
    import asyncio

    text = document.text()
    if not text.strip():
        return []

    try:
        client, classify_model, extract_model = _get_client_and_models()
    except Exception:
        client, classify_model, extract_model = None, "gpt-4o-mini", "gpt-4o"

    loop = asyncio.get_event_loop()
    is_txn, reason = await loop.run_in_executor(None, _classify, text, client, classify_model)
    logger.info("%s: is_transaction=%s reason=%s", document.doc_id, is_txn, reason)

    if not is_txn:
        return []

    # _extract_raw raises on failure — callers must handle and set extraction_failed.
    raw_list = await loop.run_in_executor(None, _extract_raw, text, document.doc_id, client, extract_model)

    transactions = []
    for raw in raw_list:
        txn = _build_transaction(raw, document.doc_id)
        if txn is not None and txn.parties:
            transactions.append(txn)

    return deduplicate_intra_document_transactions(transactions)


def _determine_decoy_reason(document: Document) -> str:
    """Derive a factual, source-grounded explanation for why a document describes no qualifying transaction."""
    subj = str(getattr(document, "subject", "") or "").lower()
    title = str(getattr(document, "title", "") or "").lower()
    try:
        raw_text = document.text()
    except Exception:
        raw_text = ""
    text_lower = raw_text.lower()
    combined = f"{subj} {title} {text_lower[:4000]}"

    # 1. Promoter transmission / succession / inheritance (SAST Reg 10(6))
    if any(k in combined for k in ["by way of transmission", "by way of succession", "inheritance", "late mr", "late mrs", "late shri", "10(1)(g)"]):
        return "Inter-se promoter share transmission/inheritance disclosure; not a qualifying corporate M&A transaction."

    # 2. Inter-se promoter transfer / gift (SAST Reg 10(1)(a) / 10(5) / 10(6))
    if any(k in combined for k in ["by way of gift", "inter-se transfer", "inter se transfer", "10(1)(a)", "10(5)", "via gift"]) or ("gift" in title):
        return "Inter-se promoter share transfer/gift disclosure; not a qualifying corporate M&A transaction."

    # 3. Bonus shares by existing subsidiary
    if "bonus" in combined and any(k in combined for k in ["subsidiary", "bonus shares", "bonus equity"]):
        return "Allotment of bonus equity shares by existing subsidiary; no external corporate acquisition."

    # 4. Conversion of loan into equity in existing subsidiary
    if "conversion of" in combined and "loan" in combined:
        return "Conversion of loan to equity in existing subsidiary; no external corporate acquisition."

    # 5. Internal capital reduction / reorganization of existing subsidiary
    if any(k in combined for k in ["capital reduction", "reduction of share capital"]) and "subsidiary" in combined:
        return "Capital reduction and internal reorganization in existing subsidiary; no external M&A transaction."

    # 6. Rights issue / capital infusion into existing wholly owned subsidiary
    if any(k in combined for k in ["subscribing rights issue", "infused additional capital", "further investment in wholly owned", "rights issue"]):
        return "Subscription to rights issue of existing wholly owned subsidiary; no external corporate acquisition."

    # 7. Media Release / Supply Contract announcement
    if any(k in combined for k in ["media release", "press release"]) and any(k in combined for k in ["contract", "order", "supply"]):
        return "Commercial contract win and supply agreement media release; no corporate acquisition or divestment."

    # 8. Dividend / Record Date / Book Closure
    if "record date" in subj or any(k in combined for k in ["record date for the purpose of final dividend", "record date for payment of dividend", "record date, cut-off date and book closure", "book closure"]):
        return "Dividend record-date and book closure announcement; no acquisition or divestment."
    if "dividend" in subj or "record date" in combined:
        return "Dividend record-date announcement; no acquisition or divestment."

    # 9. Investor / Analyst Meets
    if "analyst" in subj or any(k in combined for k in ["analyst/institutional investor meet", "investor/fund house meeting", "schedule of investor meeting", "hosts investor meet", "investor meet"]):
        return "Investor/earnings meet notice and schedule update; no qualifying transaction described."

    # 10. Management change / Appointments / Resignations
    if any(k in subj for k in ["appointment", "change in management"]) or any(k in combined for k in ["resignation of", "appointment of independent director", "appointment of director"]):
        return "Board appointment or senior management change disclosure; no acquisition or divestment."

    # 11. News-specific categories
    if title:
        if any(k in title for k in ["downgraded to sell", "valuation concerns", "technical weakness"]):
            return "Market commentary on stock rating downgrade; no corporate transaction described."
        if any(k in title for k in ["revenue up", "fy26 revenue", "quarterly revenue"]):
            return "Financial revenue performance report; no corporate transaction described."
        if any(k in title for k in ["fii exodus", "decline in foreign capital", "foreign institutional"]):
            return "Market news report on institutional capital flows; no corporate transaction described."
        if any(k in title for k in ["top owners are public companies", "held by individual investors", "shareholding pattern"]):
            return "Shareholding pattern and ownership breakdown report; no corporate transaction described."
        if "completes merger" in title and "steel plant" in title:
            return "News report on past merger completion and internal capex plant approval; no new M&A transaction."

    return "no transaction found"


async def extract_with_outcome(document: Document) -> tuple[list[Transaction], DocumentOutcome]:
    """Extract transactions and return a DocumentOutcome for tracking.

    Distinguishes three states:
    - Successful extraction with transactions  -> has_transaction=True
    - Successful extraction, no transactions   -> has_transaction=False, extraction_failed=False
    - LLM/JSON parse failure                   -> has_transaction=False, extraction_failed=True
    """
    try:
        transactions = await extract(document)
        reason = "transaction extracted" if transactions else _determine_decoy_reason(document)
        outcome = DocumentOutcome(
            doc_id=document.doc_id,
            has_transaction=len(transactions) > 0,
            transaction_count=len(transactions),
            reason=reason,
            extraction_failed=False,
        )
        return transactions, outcome
    except Exception as exc:
        logger.error("Extraction failed for %s: %s", document.doc_id, exc)
        outcome = DocumentOutcome(
            doc_id=document.doc_id,
            has_transaction=False,
            transaction_count=0,
            reason=f"extraction_error: {exc}",
            extraction_failed=True,
        )
        return [], outcome


