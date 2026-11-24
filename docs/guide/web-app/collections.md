<!--docs
title = "Collections"
summary = "Keep private sources and ask questions answered from them."
order = 40
covers = ["/collections"]
aliases = ["/docs/collections"]
-->
# Collections

## Make a collection {#collections}

[Open Collections](/collections) to search your private sets and see their source readiness. Select New collection, choose a local embedding model and sensitivity level, then upload UTF-8 text or a searchable PDF. Select Index / retry and wait for Ready before searching.

The requested model can run on any eligible node hosting that exact name. Differently named copies are refused. A failed source shows the error reason and a request diagnostics link; View collection event log opens its related events. **Cancel** on an indexing source, or a WeightRoom restart during indexing, leaves it **Stopped** with a neutral badge: nothing failed, its partial index is removed and nothing is retried by itself. Select **Index / retry** to index it.

## Ask and check the sources {#collections-ask}

Ask a question and choose an answer model, or retrieve excerpts only. Review the cited sources: similarity is a retrieval score, not a guarantee that an answer is correct. Collections and their sources are private to their owner. Resetting the embedding space clears its vectors and requires indexing again.

A length-limited or reasoning-only reply explains why no cited answer was accepted and links the recorded request when available. Try a non-reasoning answer model; citation checks still apply.

The guided workspace places sources above the question and evidence. Prepare sources, retrieve evidence, then review the answer against known facts. Create workflow starts a new research-to-draft plan with this collection selected.

[Shared form controls and confirmations](/docs/getting-started/navigation#forms-and-confirmations)
