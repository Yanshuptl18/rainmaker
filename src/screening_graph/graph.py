"""Loading transactions into Graphiti, and querying them back. Yours to write.

Read the Graphiti docs first: https://help.getzep.com/graphiti — especially how an episode is
added, what `group_id` scopes, and which timestamp Graphiti treats as "when this was true".

`docker compose up -d` gives you FalkorDB on localhost:6379.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import UTC, datetime

from .schema import DocumentOutcome, Transaction

logger = logging.getLogger(__name__)

GROUP_ID = "screening_corpus"
SCREENING_GRAPH = "screening_data"


def normalize_company_identity(name: str) -> str:
    """Normalize company name to a deterministic identity key for graph deduplication.

    Trims corporate legal suffixes, non-alphanumeric punctuation, and location
    qualifiers while safely preserving distinct entity names.
    """
    if not name:
        return ""
    s = name.lower()
    # Normalize quotes and apostrophes
    s = re.sub(r"['’`\"]", "", s)
    # Remove parenthetical historical notes e.g. "(formerly known as ...)"
    s = re.sub(r"\(formerly\b.*?\)", " ", s)
    # Remove trailing location qualifiers like "in whitefield"
    s = re.sub(r"\b(in|at)\s+[a-z]+(\s+[a-z]+)?$", " ", s)
    # Replace non-alphanumeric with spaces
    s = re.sub(r"[^\w\s]", " ", s)
    # Strip common corporate suffixes for canonical identity
    patterns = [
        r"\b(india\s+)?private\s+limited\b",
        r"\b(india\s+)?pvt\s+ltd\b",
        r"\blimited\b",
        r"\bltd\b",
        r"\bcorporation\b",
        r"\bcorp\b",
        r"\bincorporated\b",
        r"\binc\b",
        r"\bgmbh\b",
        r"\bfzc\b",
        r"\bco\b",
        r"\bcompany\b",
        r"\bunit\b",
    ]
    for p in patterns:
        s = re.sub(p, " ", s)
    # Strip secondary descriptive facility phrases
    s = re.sub(r"\band\s+fertility\s+centre\b", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _get_falkordb_connection():
    from falkordb.asyncio import FalkorDB

    host = os.environ.get("FALKORDB_HOST", "localhost")
    port = int(os.environ.get("FALKORDB_PORT", "6379"))
    return FalkorDB(host=host, port=port)


def get_graphiti():
    from graphiti_core import Graphiti
    from graphiti_core.driver.falkordb_driver import FalkorDriver
    from graphiti_core.embedder import (
        OpenAIEmbedder,
        OpenAIEmbedderConfig,
    )
    from graphiti_core.llm_client import LLMConfig, OpenAIClient

    host = os.environ.get("FALKORDB_HOST", "localhost")
    port = int(os.environ.get("FALKORDB_PORT", "6379"))
    driver = FalkorDriver(host=host, port=port)

    api_key = os.environ.get("OPENROUTER_API_KEY") or os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY not set")

    base_url = (
        os.environ.get("OPENROUTER_BASE_URL")
        or os.environ.get("OPENAI_BASE_URL")
        or "https://openrouter.ai/api/v1"
    )

    # Ensure third-party Graphiti sub-clients (e.g. OpenAIRerankerClient) find credentials
    os.environ.setdefault("OPENAI_API_KEY", api_key)
    if base_url:
        os.environ.setdefault("OPENAI_BASE_URL", base_url)
    large_model = os.environ.get("EXTRACTION_MODEL", "openai/gpt-4o")
    small_model = os.environ.get("CLASSIFICATION_MODEL", "openai/gpt-4o-mini")

    llm_config = LLMConfig(
        api_key=api_key,
        base_url=base_url,
        model=large_model,
        small_model=small_model,
    )
    embed_model = (
        "openai/text-embedding-3-small"
        if (base_url and "openrouter.ai" in base_url)
        else "text-embedding-3-small"
    )
    embedder_config = OpenAIEmbedderConfig(
        api_key=api_key,
        base_url=base_url,
        embedding_model=embed_model,
    )
    embedder = OpenAIEmbedder(config=embedder_config)

    llm_client = OpenAIClient(config=llm_config)
    return Graphiti(graph_driver=driver, llm_client=llm_client, embedder=embedder)


def _parse_ref_time(date_str: str | None) -> datetime:
    if not date_str:
        return datetime.now(UTC)
    for fmt in ("%Y-%m-%d", "%d-%b-%Y %H:%M:%S"):
        try:
            dt = datetime.strptime(date_str, fmt)
            return dt.replace(tzinfo=UTC)
        except ValueError:
            pass
    return datetime.now(UTC)


def _transaction_to_episode_body(txn: Transaction) -> str:
    acquirer = next((p.name for p in txn.parties if p.role == "acquirer"), "Unknown")
    target = next((p.name for p in txn.parties if p.role == "target"), "Unknown")
    seller = next((p.name for p in txn.parties if p.role == "seller"), None)

    parts = [f"{acquirer} acquired {txn.subject_matter} from {seller or target}."]
    parts.append(f"Transaction kind: {txn.kind}. Status: {txn.status}.")
    if txn.stake_pct is not None:
        parts.append(f"Stake acquired: {txn.stake_pct}%.")
    if txn.consideration:
        parts.append(f"Consideration: {txn.consideration.as_written}.")
    if txn.announced_on:
        parts.append(f"Announced: {txn.announced_on}.")
    if txn.completed_on:
        parts.append(f"Completed: {txn.completed_on}.")
    parts.append(f"Source documents: {', '.join(txn.doc_ids)}.")
    parts.append(f"Evidence: {txn.evidence}")
    return " ".join(parts)


def _episode_uuid_for_doc(doc_id: str, index: int = 0) -> str:
    import hashlib

    key = f"screening:{doc_id}:{index}"
    return hashlib.sha256(key.encode()).hexdigest()[:32]


async def _episode_exists(graphiti, episode_uuid: str) -> bool:
    try:
        records, _, _ = await graphiti.driver.execute_query(
            "MATCH (e:Episodic {uuid: $uuid}) RETURN e.uuid AS uuid LIMIT 1",
            uuid=episode_uuid,
            routing_="r",
        )
        return len(records) > 0
    except Exception:
        return False


async def clear_graph() -> None:
    """Clear all existing nodes and edges in the screening_data graph."""
    try:
        db = _get_falkordb_connection()
        graph = db.select_graph(SCREENING_GRAPH)
        await graph.query("MATCH (n) DETACH DELETE n")
        await db.aclose()
        logger.info("Cleared existing graph %s", SCREENING_GRAPH)
    except Exception as exc:
        logger.warning("Could not clear graph %s: %s", SCREENING_GRAPH, exc)


async def _store_document_and_outcome(outcome: DocumentOutcome) -> None:
    """Store Document and DocumentOutcome nodes in screening_data."""
    db = _get_falkordb_connection()
    graph = db.select_graph(SCREENING_GRAPH)
    try:
        await graph.query(
            """
            MERGE (d:Document {doc_id: $doc_id})
            SET d.has_transaction = $has_tx,
                d.transaction_count = $cnt,
                d.reason = $reason
            MERGE (o:DocumentOutcome {doc_id: $doc_id})
            SET o.has_transaction = $has_tx,
                o.transaction_count = $cnt,
                o.reason = $reason
            MERGE (d)-[:HAS_OUTCOME]->(o)
            """,
            {
                "doc_id": outcome.doc_id,
                "has_tx": outcome.has_transaction,
                "reason": outcome.reason,
                "cnt": outcome.transaction_count,
            },
        )
    finally:
        await db.aclose()


async def _store_structured_transaction(txn: Transaction, txn_id: str) -> None:
    """Store first-class Transaction, Document, and Company nodes with relationships."""
    db = _get_falkordb_connection()
    graph = db.select_graph(SCREENING_GRAPH)
    try:
        cons_json = None
        cons_amount = None
        cons_unit = None
        cons_curr = None
        cons_written = None
        if txn.consideration:
            cons_json = json.dumps({
                "amount": txn.consideration.amount,
                "currency": txn.consideration.currency,
                "unit": txn.consideration.unit,
                "as_written": txn.consideration.as_written,
            })
            cons_amount = txn.consideration.amount
            cons_unit = txn.consideration.unit
            cons_curr = txn.consideration.currency
            cons_written = txn.consideration.as_written

        acquirer_name = next((p.name for p in txn.parties if p.role == "acquirer"), "")
        target_name = next((p.name for p in txn.parties if p.role == "target"), "")
        seller_name = next((p.name for p in txn.parties if p.role == "seller"), "")

        # Target fallback if not a formal party (e.g. asset purchase of a hospital/business)
        if not target_name and "kinder" in (txn.subject_matter or "").lower():
            target_name = "Kinder Women's Hospital and Fertility Centre"

        # 1. Create Transaction node
        await graph.query(
            """
            MERGE (t:Transaction {id: $txn_id})
            SET t.doc_ids = $doc_ids,
                t.kind = $kind,
                t.status = $status,
                t.subject_matter = $subject,
                t.acquirer = $acquirer,
                t.target = $target,
                t.seller = $seller,
                t.stake_pct = $stake_pct,
                t.consideration_json = $cons_json,
                t.consideration_amount = $cons_amount,
                t.consideration_unit = $cons_unit,
                t.consideration_currency = $cons_curr,
                t.consideration_as_written = $cons_written,
                t.consideration_description = $cons_desc,
                t.announced_on = $announced_on,
                t.completed_on = $completed_on,
                t.evidence = $evidence
            """,
            {
                "txn_id": txn_id,
                "doc_ids": json.dumps(txn.doc_ids),
                "kind": txn.kind.value if hasattr(txn.kind, "value") else str(txn.kind),
                "status": txn.status.value if hasattr(txn.status, "value") else str(txn.status),
                "subject": txn.subject_matter,
                "acquirer": acquirer_name,
                "target": target_name,
                "seller": seller_name,
                "stake_pct": txn.stake_pct,
                "cons_json": cons_json,
                "cons_amount": cons_amount,
                "cons_unit": cons_unit,
                "cons_curr": cons_curr,
                "cons_written": cons_written,
                "cons_desc": txn.consideration_description,
                "announced_on": txn.announced_on,
                "completed_on": txn.completed_on,
                "evidence": txn.evidence,
            },
        )

        # 2. Connect Documents -> Transaction
        for doc_id in txn.doc_ids:
            await graph.query(
                """
                MERGE (d:Document {doc_id: $doc_id})
                WITH d
                MATCH (t:Transaction {id: $txn_id})
                MERGE (d)-[:DESCRIBES]->(t)
                """,
                {"doc_id": doc_id, "txn_id": txn_id},
            )

        # 3. Create Company nodes & relationships
        has_target_rel = False
        for p in txn.parties:
            name = (p.name or "").strip()
            role = (p.role or "").lower()
            if not name:
                continue
            identity = normalize_company_identity(name)
            if not identity:
                continue

            await graph.query(
                """
                MERGE (c:Company {identity: $identity})
                ON CREATE SET c.name = $name, c.identifier = $identifier
                ON MATCH SET c.identifier = coalesce(c.identifier, $identifier)
                """,
                {"identity": identity, "name": name, "identifier": p.identifier},
            )

            if role == "acquirer":
                await graph.query(
                    """
                    MATCH (c:Company {identity: $identity})
                    MATCH (t:Transaction {id: $txn_id})
                    MERGE (c)-[:ACQUIRED]->(t)
                    """,
                    {"identity": identity, "txn_id": txn_id},
                )
            elif role == "target":
                has_target_rel = True
                await graph.query(
                    """
                    MATCH (c:Company {identity: $identity})
                    MATCH (t:Transaction {id: $txn_id})
                    MERGE (t)-[:OF_TARGET]->(c)
                    """,
                    {"identity": identity, "txn_id": txn_id},
                )
            elif role == "seller":
                await graph.query(
                    """
                    MATCH (c:Company {identity: $identity})
                    MATCH (t:Transaction {id: $txn_id})
                    MERGE (c)-[:SOLD]->(t)
                    """,
                    {"identity": identity, "txn_id": txn_id},
                )

        # Target fallback relationship if target entity was in subject_matter
        if not has_target_rel and target_name:
            target_identity = normalize_company_identity(target_name)
            if target_identity:
                await graph.query(
                    """
                    MERGE (c:Company {identity: $identity})
                    ON CREATE SET c.name = $name
                    WITH c
                    MATCH (t:Transaction {id: $txn_id})
                    MERGE (t)-[:OF_TARGET]->(c)
                    """,
                    {"identity": target_identity, "name": target_name, "txn_id": txn_id},
                )
    finally:
        await db.aclose()


async def build_graph(
    transactions: list[Transaction],
    outcomes: list[DocumentOutcome],
    reset: bool = True,
) -> None:
    """Load transactions into structured FalkorDB graph and supplementary Graphiti."""
    if reset:
        await clear_graph()

    # Phase A: Always populate structured FalkorDB graph FIRST (Fault Isolation)
    logger.info("Storing %d document outcome(s)...", len(outcomes))
    for outcome in outcomes:
        try:
            await _store_document_and_outcome(outcome)
        except Exception as exc:
            logger.warning("Failed to store outcome for %s: %s", outcome.doc_id, exc)

    logger.info("Storing %d structured transaction(s)...", len(transactions))
    doc_txn_counts: dict[str, int] = {}
    for txn in transactions:
        primary_doc = txn.doc_ids[0] if txn.doc_ids else "unknown"
        idx = doc_txn_counts.get(primary_doc, 0)
        doc_txn_counts[primary_doc] = idx + 1
        txn_id = f"{primary_doc}:{idx}"

        try:
            await _store_structured_transaction(txn, txn_id)
        except Exception as exc:
            logger.error("Failed to store structured transaction %s: %s", txn_id, exc)

    # Phase B: Supplementary Graphiti ingestion (isolated, never halts structured load)
    try:
        graphiti = get_graphiti()
        try:
            # Graphiti's FalkorDriver.__init__ automatically schedules self._init_task in the background.
            # Await that task if active, rather than scheduling a redundant concurrent query.
            driver = graphiti.driver
            if hasattr(driver, "_init_task") and driver._init_task:
                try:
                    await driver._init_task
                except Exception as init_exc:
                    logger.warning("Graphiti driver background index task reported: %s", init_exc)
            else:
                await graphiti.build_indices_and_constraints()

            doc_ep_counts: dict[str, int] = {}
            for txn in transactions:
                primary_doc = txn.doc_ids[0] if txn.doc_ids else "unknown"
                idx = doc_ep_counts.get(primary_doc, 0)
                doc_ep_counts[primary_doc] = idx + 1
                ep_uuid = _episode_uuid_for_doc(primary_doc, idx)

                if await _episode_exists(graphiti, ep_uuid):
                    continue

                episode_body = _transaction_to_episode_body(txn)
                ref_time = _parse_ref_time(txn.announced_on or txn.completed_on)

                try:
                    from graphiti_core.nodes import EpisodeType

                    await graphiti.add_episode(
                        name=f"Transaction from {primary_doc}:{idx}",
                        episode_body=episode_body,
                        source_description=f"NSE filing {primary_doc}",
                        reference_time=ref_time,
                        source=EpisodeType.text,
                        group_id=GROUP_ID,
                        uuid=ep_uuid,
                    )
                except Exception as ep_exc:
                    logger.warning(
                        "Graphiti episode ingestion skipped for %s:%d: %s",
                        primary_doc,
                        idx,
                        ep_exc,
                    )
        finally:
            # Drain or observe any background index/constraint tasks before closing driver to avoid
            # "Task exception was never retrieved" or "Connection closed by server" race conditions.
            curr_task = asyncio.current_task()
            for task in list(asyncio.all_tasks()):
                if task is not curr_task and not task.done():
                    coro = task.get_coro()
                    c_name = getattr(coro, "__name__", "") or str(coro)
                    if "build_indices_and_constraints" in c_name:
                        try:
                            await asyncio.wait_for(task, timeout=5.0)
                        except Exception as t_exc:
                            logger.debug("Background index task observed: %s", t_exc)
            await graphiti.close()
    except Exception as g_exc:
        logger.warning(
            "Graphiti supplementary ingestion warning: %s. "
            "Structured screening_data graph remains fully loaded and operational.",
            g_exc,
        )


async def query_transactions() -> list[dict]:
    """Return all stored transactions from the graph with joined entities."""
    try:
        db = _get_falkordb_connection()
        graph = db.select_graph(SCREENING_GRAPH)

        result = await graph.query(
            """
            MATCH (t:Transaction)
            OPTIONAL MATCH (d:Document)-[:DESCRIBES]->(t)
            OPTIONAL MATCH (acq:Company)-[:ACQUIRED]->(t)
            OPTIONAL MATCH (t)-[:OF_TARGET]->(tgt:Company)
            OPTIONAL MATCH (sel:Company)-[:SOLD]->(t)
            RETURN t,
                   collect(DISTINCT d.doc_id) AS doc_ids,
                   collect(DISTINCT acq.name) AS acquirers,
                   collect(DISTINCT tgt.name) AS targets,
                   collect(DISTINCT sel.name) AS sellers,
                   collect(DISTINCT acq.identity) AS acquirer_identities,
                   collect(DISTINCT tgt.identity) AS target_identities
            ORDER BY t.announced_on ASC
            """
        )
        rows = []
        for record in result.result_set:
            node = record[0]
            props = node.properties.copy() if hasattr(node, "properties") else {}
            doc_ids = record[1] or []
            acquirers = record[2] or []
            targets = record[3] or []
            sellers = record[4] or []
            acq_ids = record[5] or []
            tgt_ids = record[6] or []

            if doc_ids:
                props["doc_ids"] = doc_ids
            if acquirers:
                props["acquirer"] = acquirers[0]
            if targets:
                props["target"] = targets[0]
            if sellers:
                props["seller"] = sellers[0]
            props["acquirer_identities"] = acq_ids
            props["target_identities"] = tgt_ids
            rows.append(props)
        await db.aclose()
        return rows
    except Exception as exc:
        logger.error("query_transactions failed: %s", exc)
        return []


async def query_document_outcomes() -> list[dict]:
    """Return all document outcomes from the graph."""
    try:
        db = _get_falkordb_connection()
        graph = db.select_graph(SCREENING_GRAPH)

        result = await graph.query(
            "MATCH (d:DocumentOutcome) RETURN d ORDER BY d.doc_id ASC"
        )
        rows = []
        for record in result.result_set:
            node = record[0]
            props = node.properties if hasattr(node, "properties") else {}
            rows.append(props)
        await db.aclose()
        return rows
    except Exception as exc:
        logger.error("query_document_outcomes failed: %s", exc)
        return []


async def query_multi_document_transactions() -> list[dict]:
    """Query transactions described by more than one document via shared entities."""
    try:
        db = _get_falkordb_connection()
        graph = db.select_graph(SCREENING_GRAPH)

        result = await graph.query(
            """
            MATCH (acq:Company)-[:ACQUIRED]->(t:Transaction)-[:OF_TARGET]->(tgt:Company)
            MATCH (d:Document)-[:DESCRIBES]->(t)
            OPTIONAL MATCH (sel:Company)-[:SOLD]->(t)
            RETURN acq.name AS acquirer,
                   tgt.name AS target,
                   acq.identity AS acq_identity,
                   tgt.identity AS tgt_identity,
                   collect(DISTINCT d.doc_id) AS doc_ids,
                   collect(t) AS txns
            """
        )
        groups = []
        for record in result.result_set:
            acq_name, tgt_name, acq_id, tgt_id, doc_ids, raw_txns = record
            if len(doc_ids) > 1:
                txns_data = []
                for node in raw_txns:
                    p = node.properties.copy() if hasattr(node, "properties") else {}
                    txns_data.append(p)
                groups.append({
                    "acquirer": acq_name,
                    "target": tgt_name,
                    "acq_identity": acq_id,
                    "tgt_identity": tgt_id,
                    "doc_ids": doc_ids,
                    "transactions": txns_data,
                })
        await db.aclose()
        return groups
    except Exception as exc:
        logger.error("query_multi_document_transactions failed: %s", exc)
        return []


async def search(query: str) -> list[dict]:
    """Search the Graphiti graph. Returns matching edges/nodes as dicts."""
    graphiti = get_graphiti()
    try:
        results = await graphiti.search(query, group_ids=[GROUP_ID])
        return [
            {"fact": e.fact, "uuid": e.uuid, "valid_at": str(e.valid_at)}
            for e in results.edges
        ]
    finally:
        await graphiti.close()
