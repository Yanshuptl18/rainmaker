"""The `screening-graph` command line.

`corpus` works out of the box, so you can check your environment before writing anything. The
other three commands are yours. Keep them, add to them, or restructure them — but one command
per stage, and `--help` should tell a stranger how to run the pipeline.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

import typer
from dotenv import load_dotenv

from . import corpus
from .ask import QUESTION_IDS, Answer, answer
from .schema import DocumentOutcome, Transaction

load_dotenv()
if "OPENROUTER_API_KEY" in os.environ and "OPENAI_API_KEY" not in os.environ:
    os.environ["OPENAI_API_KEY"] = os.environ["OPENROUTER_API_KEY"]
if "OPENROUTER_BASE_URL" in os.environ and "OPENAI_BASE_URL" not in os.environ:
    os.environ["OPENAI_BASE_URL"] = os.environ["OPENROUTER_BASE_URL"]

# Ensure stdout handles non-ASCII characters on Windows
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf-8-sig"):
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

app = typer.Typer(add_completion=False, help="Build a screening graph from Indian exchange filings.")


@app.command("corpus")
def corpus_cmd(kind: str | None = typer.Option(None, help="filing | news")) -> None:
    """List the shipped documents. Use this to check your setup."""
    docs = corpus.documents(kind)
    for doc in docs:
        label = doc.subject or doc.title or ""
        typer.echo(f"{doc.doc_id}  {doc.kind:7}  {(doc.company_name or '')[:34]:34}  {label[:44]}")
    typer.echo(f"\n{len(docs)} documents")


# Formats extracted transactions for readable terminal output.
def _print_extraction_summary(transactions: list[Transaction], outcome: DocumentOutcome) -> None:
    if not transactions:
        typer.echo("")
        typer.echo("Transaction: No")
        typer.echo(f"Reason: {outcome.reason or 'Not stated'}")
        return

    for idx, txn in enumerate(transactions, start=1):
        typer.echo("")
        typer.echo(f"Transaction {idx}")
        typer.echo("Transaction: Yes")

        acquirers = [p.name for p in txn.parties if p.role.lower() == "acquirer"]
        targets = [p.name for p in txn.parties if p.role.lower() == "target"]
        sellers = [p.name for p in txn.parties if p.role.lower() == "seller"]
        other_roles: dict[str, list[str]] = {}
        for p in txn.parties:
            role_lower = p.role.lower()
            if role_lower not in {"acquirer", "target", "seller"}:
                other_roles.setdefault(p.role, []).append(p.name)

        typer.echo(f"Acquirer: {', '.join(acquirers) if acquirers else 'Not stated'}")
        typer.echo(f"Target: {', '.join(targets) if targets else 'Not stated'}")
        typer.echo(f"Seller: {', '.join(sellers) if sellers else 'Not stated'}")
        for role, names in other_roles.items():
            role_label = role.replace("_", " ").capitalize()
            typer.echo(f"{role_label}: {', '.join(names)}")

        stake_str = f"{txn.stake_pct:g}%" if txn.stake_pct is not None else "Not stated"
        typer.echo(f"Stake: {stake_str}")

        raw_status = txn.status.value if hasattr(txn.status, "value") else str(txn.status)
        stage_str = raw_status.replace("_", " ").capitalize() if raw_status else "Not stated"
        typer.echo(f"Stage: {stage_str}")

        typer.echo(f"Subject: {txn.subject_matter or 'Not stated'}")

        if txn.consideration:
            c = txn.consideration
            typer.echo(f"Consideration: {c.amount:g} {c.currency} {c.unit} ({c.as_written})")
        else:
            typer.echo("Consideration: Not stated")

        typer.echo(f"Announced on: {txn.announced_on or 'Not stated'}")
        typer.echo(f"Completed on: {txn.completed_on or 'Not stated'}")

        doc_ids_str = ", ".join(txn.doc_ids) if txn.doc_ids else "Not stated"
        typer.echo(f"Document ID: {doc_ids_str}")

        if txn.evidence:
            clean_ev = txn.evidence.strip()
            ev_str = clean_ev if clean_ev.startswith('"') and clean_ev.endswith('"') else f'"{clean_ev}"'
            typer.echo(f"Evidence: {ev_str}")
        else:
            typer.echo("Evidence: Not stated")


@app.command()
def extract(
    doc_id: str | None = typer.Option(None, help="one document; default is all of them"),
    out: str = typer.Option("out/transactions.json", help="where to write the result"),
) -> None:
    """Read transactions out of the documents using a two-stage LLM extraction."""

    async def _run() -> None:
        from .extract import extract_with_outcome

        out_path = Path(out)
        out_path.parent.mkdir(parents=True, exist_ok=True)

        if doc_id:
            docs = [corpus.document(doc_id)]
        else:
            docs = list(corpus.documents())

        typer.echo(f"Extracting {len(docs)} document(s)...")

        all_transactions: list[dict] = []
        all_outcomes: list[dict] = []

        for doc in docs:
            typer.echo(f"  {doc.doc_id} ({doc.company_name or doc.kind})...")
            transactions, outcome = await extract_with_outcome(doc)
            _print_extraction_summary(transactions, outcome)
            for txn in transactions:
                all_transactions.append(txn.model_dump())
            all_outcomes.append({
                "doc_id": outcome.doc_id,
                "has_transaction": outcome.has_transaction,
                "transaction_count": outcome.transaction_count,
                "reason": outcome.reason,
            })

        result = {
            "transactions": all_transactions,
            "outcomes": all_outcomes,
        }
        out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
        typer.echo(f"\nExtracted {len(all_transactions)} transaction(s) from {len(docs)} document(s).")
        typer.echo(f"Results written to {out_path}")

    try:
        asyncio.run(_run())
    except Exception as exc:
        typer.echo(f"ERROR: {exc}", err=True)
        raise typer.Exit(1)


@app.command()
def load(
    src: str | None = typer.Option(
        None,
        help="extracted JSON file to load (default: out/all-transactions.json or out/transactions.json)",
    ),
    reset: bool = typer.Option(
        True,
        "--reset/--no-reset",
        help="clear existing graph nodes before loading",
    ),
) -> None:
    """Load extracted transactions into Graphiti (backed by FalkorDB)."""

    async def _run() -> None:
        from .graph import build_graph
        from .schema import DealKind, DealStatus, Money, Party

        if src:
            src_path = Path(src)
        elif Path("out/all-transactions.json").exists():
            src_path = Path("out/all-transactions.json")
        else:
            src_path = Path("out/transactions.json")

        if not src_path.exists():
            typer.echo(f"ERROR: {src_path} not found. Run 'extract' first.", err=True)
            raise typer.Exit(1)

        typer.echo(f"Reading extraction data from {src_path}...")
        data = json.loads(src_path.read_text(encoding="utf-8"))
        raw_txns = data.get("transactions", [])
        raw_outcomes = data.get("outcomes", [])

        transactions = []
        for raw in raw_txns:
            try:
                cons = None
                if raw.get("consideration"):
                    cons = Money(**raw["consideration"])
                txn = Transaction(
                    doc_ids=raw["doc_ids"],
                    kind=DealKind(raw["kind"]),
                    status=DealStatus(raw["status"]),
                    parties=[Party(**p) for p in raw.get("parties", [])],
                    subject_matter=raw["subject_matter"],
                    stake_pct=raw.get("stake_pct"),
                    consideration=cons,
                    consideration_description=raw.get("consideration_description"),
                    announced_on=raw.get("announced_on"),
                    completed_on=raw.get("completed_on"),
                    evidence=raw.get("evidence", ""),
                )
                transactions.append(txn)
            except Exception as exc:
                typer.echo(f"  Skipping malformed transaction: {exc}", err=True)

        outcomes = [
            DocumentOutcome(
                doc_id=o["doc_id"],
                has_transaction=o.get("has_transaction", False),
                transaction_count=o.get("transaction_count", 0),
                reason=o.get("reason", ""),
            )
            for o in raw_outcomes
        ]

        typer.echo(f"Loading {len(transactions)} transaction(s) and {len(outcomes)} document outcome(s)...")
        await build_graph(transactions, outcomes, reset=reset)
        typer.echo("Load complete.")

    try:
        asyncio.run(_run())
    except Exception as exc:
        typer.echo(f"ERROR: {exc}", err=True)
        raise typer.Exit(1)


@app.command()
def ask(
    question: str | None = typer.Argument(None, help=f"one of {', '.join(QUESTION_IDS)}"),
    all_questions: bool = typer.Option(False, "--all", help="answer every question in QUESTIONS.md"),
) -> None:
    """Answer the questions in QUESTIONS.md from the graph (no file re-reading)."""

    async def _run() -> None:
        if all_questions:
            questions = list(QUESTION_IDS)
        elif question:
            q = question.upper()
            if q not in QUESTION_IDS:
                typer.echo(f"ERROR: Unknown question '{q}'. Valid: {', '.join(QUESTION_IDS)}", err=True)
                raise typer.Exit(1)
            questions = [q]
        else:
            typer.echo("Provide a question (Q1..Q6) or use --all.", err=True)
            raise typer.Exit(1)

        for q in questions:
            ans: Answer = await answer(q)
            typer.echo(f"{'-'*30}")
            typer.echo(f"{ans.question_id}")
            typer.echo(f"{'-'*30}")
            typer.echo(ans.answer)
            typer.echo(f"\nDoc IDs: {', '.join(ans.doc_ids) if ans.doc_ids else 'none'}")
            typer.echo(f"Derivation: {ans.derivation}")

    try:
        asyncio.run(_run())
    except Exception as exc:
        typer.echo(f"ERROR: {exc}", err=True)
        raise typer.Exit(1)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
