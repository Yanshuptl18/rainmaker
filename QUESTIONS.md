# The six questions

Your CLI must answer these **from the graph**, after loading — not by reading `data/` again at
question time, and not from a table you typed by hand.

For every answer, print:

- the answer itself, in whatever shape fits (a list, a table, a sentence),
- the `doc_id`s it rests on,
- one line on how you got it: which nodes, edges or search you used.

Where a question cannot be answered from the documents, **say that**. An honest "the filings do not
state this" scores better than a number nobody can check.

---

**Q1. Every transaction in the corpus, largest consideration first.**
Acquirer, target, what was bought (stake / business / asset), consideration, announcement date,
`doc_id`. Deals with no stated consideration go at the end, marked as such — not as zero.

**Q2. Who acquired the Kinder Women's Hospital and Fertility Centre, from whom, when, and for how
much?** More than one document touches this, and they do not say the same thing. Report what differs
and say which document you would put in front of a client, and why. Read the money rows carefully —
not every large figure in a filing is a price.

**Q3. Which transactions are described by more than one document?**
For each, list the `doc_id`s and say whether the facts agree. Careful: two documents about the same
*company* are not necessarily about the same *deal*.

**Q4. Which transactions were approved or agreed but not stated as completed?**
As at the date of the document that describes them. This is the stated-versus-concluded line: a
board approval, a binding term sheet and a completed transfer are three different things.

**Q5. Which documents describe no transaction at all, and why?**
One line each. Getting a decoy wrong in either direction costs the same.

**Q6. As at the end of 25 August 2026, what transactions did the corpus know about?**
Answer this from the time information on your facts, not by filtering filenames or re-reading the
manifest. If your graph cannot answer it, say what is missing and what you would change.
