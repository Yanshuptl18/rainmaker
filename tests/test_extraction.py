"""Tests for extraction logic.

These are unit tests that run against known fixture text — no LLM calls, no network.
They test the build/parse layers that sit below the LLM.
Additional integration tests are guarded by @pytest.mark.integration.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from screening_graph.extract import (
    _build_transaction,
    _looks_like_share_count,
    _parse_consideration,
    _parse_date,
    deduplicate_intra_document_transactions,
    normalize_company_name,
)
from screening_graph.schema import DealKind, DealStatus, Money, Party, Transaction

# ---------- helpers & fixtures ----------

KINDER_RAW = {
    "kind": "stake_purchase",
    "status": "agreement_signed",
    "parties": [
        {"name": "Manipal Health Enterprises Private Limited", "role": "acquirer", "identifier": None},
        {"name": "Kinder Women's Hospital and Fertility Centre", "role": "target", "identifier": None},
        {"name": "Kindorama Healthcare Private Limited", "role": "seller", "identifier": None},
    ],
    "subject_matter": "Kinder Women's Hospital and Fertility Centre",
    "stake_pct": None,
    "consideration": {
        "amount": 130.0,
        "currency": "INR",
        "unit": "crore",
        "as_written": "Rs. 130,00,00,000 (Rupees One Hundred Thirty Crore Only)",
    },
    "announced_on": "2026-08-17",
    "completed_on": None,
    "evidence": (
        "The Company has entered into a Business Transfer Agreement (BTA) on 17th August 2026 "
        "with Kindorama Healthcare Private Limited for the acquisition of Kinder Women's Hospital "
        "and Fertility Centre for a consideration of Rs. 130,00,00,000."
    ),
}

DIVIDEND_TEXT = """
BSE Limited
Ref: ACUTAAS
Subject: Intimation of Record Date for payment of Final Dividend
The Board has fixed Friday, 12 September 2026 as the Record Date for the purpose of payment
of Final Dividend for FY 2025-26.
"""

PROMOTER_GIFT_TEXT = """
SAST Disclosure
Asian Hotels (East) Limited
Name of acquirer: Mr Arun Kumar Saraf
Details: Inter-se transfer of shares by way of Gift amongst Promoter(s) who are immediate relatives.
Seller: Mrs Ratna Saraf.
Price: NIL (gift).
"""

IRB_RAW = {
    "kind": "stake_purchase",
    "status": "board_approved",
    "parties": [
        {"name": "IRB Infrastructure Developers Limited", "role": "acquirer", "identifier": "532947"},
        {"name": "IRB InvIT Fund", "role": "target", "identifier": None},
    ],
    "subject_matter": "units in IRB InvIT Fund via preferential issue",
    "stake_pct": None,
    "consideration": {
        "amount": 351.0,
        "currency": "INR",
        "unit": "crore",
        "as_written": "Rs. 351,00,00,000 (Indian Rupees Three Hundred Fifty-One Crore Only)",
    },
    "announced_on": "2026-08-26",
    "completed_on": None,
    "evidence": (
        "The Board approved investment in IRB InvIT Fund... for an amount of up to "
        "Rs. 351,00,00,000."
    ),
}

GREAVES_RAW = {
    "kind": "stake_purchase",
    "status": "completed",
    "parties": [
        {"name": "Greaves Cotton Limited", "role": "acquirer", "identifier": "501455"},
        {"name": "Excel Controlinkage Private Limited", "role": "target", "identifier": None},
    ],
    "subject_matter": "remaining 20% shareholding in Excel Controlinkage",
    "stake_pct": 20.0,
    "consideration": None,
    "announced_on": "2026-08-17",
    "completed_on": "2026-08-13",
    "evidence": (
        "Greaves Cotton Limited today announced that it has acquired the remaining 20% shareholding "
        "in Excel Controlinkage Private Limited through the secondary route."
    ),
}

JAIN_RAW = {
    "kind": "stake_purchase",
    "status": "board_approved",
    "parties": [
        {"name": "Jain Resource Recycling Limited", "role": "acquirer", "identifier": None},
        {"name": "Jain Ikon Global Ventures FZC", "role": "target", "identifier": None},
    ],
    "subject_matter": "equity shares via loan-to-equity conversion in Jain Ikon Global Ventures FZC",
    "stake_pct": None,
    "consideration": {
        "amount": 44.5,
        "currency": "INR",
        "unit": "crore",
        "as_written": "AED 1,70,64,000 (equivalent of about INR 44.50 Crores)",
    },
    "announced_on": "2026-08-26",
    "completed_on": None,
    "evidence": (
        "The Borrowing and Investment Committee of the Company approved the conversion of an "
        "outstanding loan of AED 1,70,64,000 (equivalent of about INR 44.50 Crores)."
    ),
}

# FIL-023 style raw: consideration is a share count, not money.
SHARE_CONSIDERATION_RAW = {
    "kind": "stake_purchase",
    "status": "agreement_signed",
    "parties": [
        {"name": "Acquirer Corp", "role": "acquirer", "identifier": None},
        {"name": "Target Corp", "role": "target", "identifier": None},
    ],
    "subject_matter": "100% stake in Target Corp",
    "stake_pct": 100.0,
    "consideration": {
        "amount": 14404204,
        "currency": "INR",
        "unit": "absolute",
        "as_written": "1,44,04,204 equity shares of the acquirer",
    },
    "announced_on": "2026-08-20",
    "completed_on": None,
    "evidence": "Acquirer Corp agreed to issue 1,44,04,204 equity shares to the seller as consideration.",
}


# ---------- tests ----------

class TestNormalizeCompanyName:
    def test_pvt_ltd_normalized(self):
        assert normalize_company_name("Kindorama Healthcare Pvt. Ltd.") == "Kindorama Healthcare Private Limited"

    def test_ltd_normalized(self):
        assert normalize_company_name("Excel Controlinkage Ltd.") == "Excel Controlinkage Limited"

    def test_already_normalized(self):
        assert normalize_company_name("IRB Infrastructure Developers Limited") == (
            "IRB Infrastructure Developers Limited"
        )

    def test_strips_extra_spaces(self):
        result = normalize_company_name("  Acme   Technologies  ")
        assert result == "Acme Technologies"


class TestParseDate:
    def test_iso_format_passthrough(self):
        assert _parse_date("2026-08-17") == "2026-08-17"

    def test_none_returns_none(self):
        assert _parse_date(None) is None

    def test_empty_returns_none(self):
        assert _parse_date("") is None


class TestLooksLikeShareCount:
    def test_equity_shares_detected(self):
        assert _looks_like_share_count("1,44,04,204 equity shares of the acquirer") is True

    def test_plain_shares_detected(self):
        assert _looks_like_share_count("5,00,000 shares") is True

    def test_monetary_phrase_not_flagged(self):
        assert _looks_like_share_count("Rs. 130,00,00,000 (Rupees One Hundred Thirty Crore)") is False

    def test_empty_string_not_flagged(self):
        assert _looks_like_share_count("") is False

    def test_securities_detected(self):
        assert _looks_like_share_count("securities issued as consideration") is True


class TestParseConsideration:
    def test_valid_money_passes_through(self):
        raw = {"amount": 130.0, "currency": "INR", "unit": "crore", "as_written": "Rs. 130 Crore"}
        money, desc = _parse_consideration(raw)
        assert money is not None
        assert money.amount == 130.0
        assert desc is None

    def test_share_count_vetoed_to_description(self):
        raw = {
            "amount": 14404204,
            "currency": "INR",
            "unit": "absolute",
            "as_written": "1,44,04,204 equity shares of the acquirer",
        }
        money, desc = _parse_consideration(raw)
        assert money is None, "Share count must not become a Money object"
        assert desc is not None
        assert "equity shares" in desc

    def test_none_input_returns_nones(self):
        money, desc = _parse_consideration(None)
        assert money is None
        assert desc is None

    def test_no_amount_returns_none_money(self):
        money, desc = _parse_consideration({"currency": "INR", "unit": "crore", "as_written": "Rs. ?"})
        assert money is None
        assert desc is None


class TestBuildTransaction:
    def test_kinder_acquirer_not_swapped(self):
        """Direction is the error that survives review. Acquirer must be Manipal, not Kinder."""
        txn = _build_transaction(KINDER_RAW, "FIL-011")
        assert txn is not None
        acquirers = [p.name for p in txn.parties if p.role == "acquirer"]
        targets = [p.name for p in txn.parties if p.role == "target"]
        assert any("Manipal" in a for a in acquirers), f"Expected Manipal as acquirer, got: {acquirers}"
        assert any("Kinder" in t for t in targets), f"Expected Kinder as target, got: {targets}"

    def test_consideration_unit_preserved(self):
        """'Rs. 130,00,00,000' must stay as 'crore' with 130.0, not as 13000000000 or 0."""
        txn = _build_transaction(KINDER_RAW, "FIL-011")
        assert txn is not None
        assert txn.consideration is not None
        assert txn.consideration.amount == 130.0
        assert txn.consideration.unit == "crore"
        assert "130" in txn.consideration.as_written

    def test_missing_consideration_stays_none(self):
        """Greaves filing does not state a price — must remain None, not 0."""
        txn = _build_transaction(GREAVES_RAW, "FIL-016")
        assert txn is not None
        assert txn.consideration is None, (
            f"Expected None consideration, got {txn.consideration}"
        )

    def test_board_approval_not_completion(self):
        """IRB board approval is not the same as completed. Status must be board_approved."""
        txn = _build_transaction(IRB_RAW, "FIL-005")
        assert txn is not None
        assert txn.status == DealStatus.BOARD_APPROVED
        assert txn.status != DealStatus.COMPLETED

    def test_agreement_signed_not_completion(self):
        """Kinder BTA signed != completed."""
        txn = _build_transaction(KINDER_RAW, "FIL-011")
        assert txn is not None
        assert txn.status == DealStatus.AGREEMENT_SIGNED
        assert txn.status != DealStatus.COMPLETED

    def test_evidence_preserved(self):
        """Evidence must come from the document, not be empty."""
        txn = _build_transaction(KINDER_RAW, "FIL-011")
        assert txn is not None
        assert len(txn.evidence) > 20, "Evidence must not be empty"
        assert "130" in txn.evidence or "BTA" in txn.evidence or "Kinder" in txn.evidence

    def test_doc_id_preserved(self):
        """The doc_id must appear in the returned transaction."""
        txn = _build_transaction(IRB_RAW, "FIL-005")
        assert txn is not None
        assert "FIL-005" in txn.doc_ids

    def test_stake_pct_captured(self):
        """Greaves acquired exactly 20%."""
        txn = _build_transaction(GREAVES_RAW, "FIL-016")
        assert txn is not None
        assert txn.stake_pct == 20.0

    def test_aed_consideration_unit(self):
        """Jain Ikon loan conversion is in INR crore equivalent (not AED)."""
        txn = _build_transaction(JAIN_RAW, "FIL-007")
        assert txn is not None
        assert txn.consideration is not None
        # amount is the INR-equivalent figure (44.5 Cr)
        assert txn.consideration.amount == 44.5

    def test_bad_raw_returns_none(self):
        """Malformed input must not raise — return None instead."""
        result = _build_transaction({"kind": "not_a_kind", "parties": []}, "FIL-999")
        # Parties list is empty, so should return None
        assert result is None

    def test_completed_status_for_completed_deal(self):
        """Greaves filing says 'completed' — status must be DealStatus.COMPLETED."""
        txn = _build_transaction(GREAVES_RAW, "FIL-016")
        assert txn is not None
        assert txn.status == DealStatus.COMPLETED

    def test_share_consideration_vetoed_to_description(self):
        """Share count in consideration must not become a Money object; stored in consideration_description."""
        txn = _build_transaction(SHARE_CONSIDERATION_RAW, "FIL-023")
        assert txn is not None
        assert txn.consideration is None, (
            f"Share count must not become Money, got: {txn.consideration}"
        )
        assert txn.consideration_description is not None
        assert "equity shares" in txn.consideration_description


@pytest.mark.asyncio
class TestExtractWithOutcome:
    """Integration-style tests using a mocked LLM — no network required."""

    async def test_a_filing_with_no_transaction_yields_nothing(self):
        """FIL-025 is a record date. Extraction must produce no transaction from it."""
        from screening_graph.corpus import Document
        from screening_graph.extract import extract_with_outcome

        mock_doc = MagicMock(spec=Document)
        mock_doc.doc_id = "FIL-025"
        mock_doc.text.return_value = DIVIDEND_TEXT

        with patch("screening_graph.extract._classify", return_value=(False, "record date for dividend")):
            txns, outcome = await extract_with_outcome(mock_doc)

        assert txns == [], f"Expected empty list, got {txns}"
        assert outcome.has_transaction is False
        assert outcome.extraction_failed is False
        assert outcome.doc_id == "FIL-025"

    async def test_promoter_gift_yields_nothing(self):
        """SAST inter-se gift transfer must not be treated as an M&A transaction."""
        from screening_graph.corpus import Document
        from screening_graph.extract import extract_with_outcome

        mock_doc = MagicMock(spec=Document)
        mock_doc.doc_id = "FIL-012"
        mock_doc.text.return_value = PROMOTER_GIFT_TEXT

        with patch("screening_graph.extract._classify", return_value=(False, "inter-se promoter gift")):
            txns, outcome = await extract_with_outcome(mock_doc)

        assert txns == []
        assert outcome.has_transaction is False
        assert outcome.extraction_failed is False

    async def test_extraction_failure_sets_flag(self):
        """LLM failure must set extraction_failed=True, not silently appear as no-transaction."""
        from screening_graph.corpus import Document
        from screening_graph.extract import extract_with_outcome

        mock_doc = MagicMock(spec=Document)
        mock_doc.doc_id = "FIL-004"
        mock_doc.text.return_value = "Some filing text"

        with (
            patch("screening_graph.extract._classify", return_value=(True, "is a transaction")),
            patch("screening_graph.extract._extract_raw", side_effect=RuntimeError("LLM parse failed")),
        ):
            txns, outcome = await extract_with_outcome(mock_doc)

        assert txns == []
        assert outcome.has_transaction is False
        assert outcome.extraction_failed is True
        assert "extraction_error" in outcome.reason

    async def test_the_acquirer_and_target_are_not_swapped(self):
        """Direction is the error that survives review — pin it on a real raw dict."""
        # This tests _build_transaction directly (no mocking needed)
        txn = _build_transaction(KINDER_RAW, "FIL-011")
        acquirers = [p.name for p in txn.parties if p.role == "acquirer"]
        targets = [p.name for p in txn.parties if p.role == "target"]
        assert any("Manipal" in a for a in acquirers)
        assert any("Kinder" in t for t in targets)
        # Acquirer must NOT be Kinder
        assert not any("Kinder" in a for a in acquirers), "Kinder is the target, not the acquirer"

    async def test_a_consideration_keeps_the_unit_the_document_printed(self):
        """The filing says Rs. 130 Crore. Consideration must keep amount=130.0, unit='crore'."""
        txn = _build_transaction(KINDER_RAW, "FIL-011")
        assert txn.consideration is not None
        assert txn.consideration.unit == "crore"
        assert txn.consideration.amount == 130.0

    async def test_a_missing_consideration_stays_missing(self):
        """No stated figure means None. Never 0, never an estimate."""
        txn = _build_transaction(GREAVES_RAW, "FIL-016")
        assert txn.consideration is None


class TestDeduplicateIntraDocumentTransactions:
    def test_merges_duplicate_deal_at_adjacent_lifecycle_stages(self):
        """FIL-003 scenario: Board approved term sheet and binding agreement announcement for same deal."""
        t1 = Transaction(
            doc_ids=["FIL-003"],
            kind=DealKind.STAKE_PURCHASE,
            status=DealStatus.BOARD_APPROVED,
            parties=[
                Party(name="Jubilant Ingrevia Limited", role="acquirer"),
                Party(name="Zettaone Technologies India Private Limited", role="target"),
            ],
            subject_matter="40% strategic equity stake in Zettaone",
            stake_pct=40.0,
            consideration=Money(amount=189.2, currency="INR", unit="crore", as_written="₹ 189.2 Cr. (approximately)"),
            announced_on="2026-08-18",
            evidence="the Board of Directors approved entry into a Binding Term Sheet with Zettaone Technologies for 40% stake",
        )
        t2 = Transaction(
            doc_ids=["FIL-003"],
            kind=DealKind.STAKE_PURCHASE,
            status=DealStatus.AGREEMENT_SIGNED,
            parties=[
                Party(name="Jubilant Ingrevia Limited", role="acquirer"),
                Party(name="Zettaone Technologies India Pvt Ltd.", role="target"),
            ],
            subject_matter="40% strategic stake",
            stake_pct=40.0,
            consideration=Money(amount=189.2, currency="INR", unit="crore", as_written="INR 189.2 crore"),
            announced_on="2026-08-18",
            evidence="Jubilant Ingrevia Limited announced that it entered into a binding agreement with Zettaone Technologies",
        )

        deduped = deduplicate_intra_document_transactions([t1, t2])
        assert len(deduped) == 1
        merged = deduped[0]
        assert merged.status == DealStatus.AGREEMENT_SIGNED
        assert merged.stake_pct == 40.0
        assert merged.consideration is not None
        assert merged.consideration.amount == 189.2
        assert any("Jubilant" in p.name for p in merged.parties if p.role == "acquirer")
        assert any("Zettaone" in p.name for p in merged.parties if p.role == "target")
        assert "binding agreement" in merged.evidence

    def test_does_not_merge_different_stake_percentages(self):
        """Transactions between same companies with different stakes must remain separate."""
        t1 = Transaction(
            doc_ids=["DOC-1"],
            kind=DealKind.STAKE_PURCHASE,
            status=DealStatus.AGREEMENT_SIGNED,
            parties=[Party(name="Corp A", role="acquirer"), Party(name="Corp B", role="target")],
            subject_matter="10% stake in Corp B",
            stake_pct=10.0,
            evidence="Acquires 10% stake",
        )
        t2 = Transaction(
            doc_ids=["DOC-1"],
            kind=DealKind.STAKE_PURCHASE,
            status=DealStatus.AGREEMENT_SIGNED,
            parties=[Party(name="Corp A", role="acquirer"), Party(name="Corp B", role="target")],
            subject_matter="25% additional stake in Corp B",
            stake_pct=25.0,
            evidence="Acquires 25% stake",
        )
        deduped = deduplicate_intra_document_transactions([t1, t2])
        assert len(deduped) == 2

    def test_does_not_merge_different_targets(self):
        """Transactions with different target companies must remain separate."""
        t1 = Transaction(
            doc_ids=["DOC-1"],
            kind=DealKind.STAKE_PURCHASE,
            status=DealStatus.AGREEMENT_SIGNED,
            parties=[Party(name="Corp A", role="acquirer"), Party(name="Target One", role="target")],
            subject_matter="acquisition of Target One",
            evidence="Acquires Target One",
        )
        t2 = Transaction(
            doc_ids=["DOC-1"],
            kind=DealKind.STAKE_PURCHASE,
            status=DealStatus.AGREEMENT_SIGNED,
            parties=[Party(name="Corp A", role="acquirer"), Party(name="Target Two", role="target")],
            subject_matter="acquisition of Target Two",
            evidence="Acquires Target Two",
        )
        deduped = deduplicate_intra_document_transactions([t1, t2])
        assert len(deduped) == 2

    def test_does_not_merge_different_considerations(self):
        """Transactions with significantly different consideration amounts must remain separate."""
        t1 = Transaction(
            doc_ids=["DOC-1"],
            kind=DealKind.STAKE_PURCHASE,
            status=DealStatus.AGREEMENT_SIGNED,
            parties=[Party(name="Corp A", role="acquirer"), Party(name="Target B", role="target")],
            subject_matter="tranche 1 acquisition",
            consideration=Money(amount=50.0, currency="INR", unit="crore", as_written="Rs 50 Cr"),
            evidence="Tranche 1 for Rs 50 Cr",
        )
        t2 = Transaction(
            doc_ids=["DOC-1"],
            kind=DealKind.STAKE_PURCHASE,
            status=DealStatus.AGREEMENT_SIGNED,
            parties=[Party(name="Corp A", role="acquirer"), Party(name="Target B", role="target")],
            subject_matter="tranche 2 acquisition",
            consideration=Money(amount=200.0, currency="INR", unit="crore", as_written="Rs 200 Cr"),
            evidence="Tranche 2 for Rs 200 Cr",
        )
        deduped = deduplicate_intra_document_transactions([t1, t2])
        assert len(deduped) == 2



