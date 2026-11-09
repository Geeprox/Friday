# Private local file services

File Steward and indexed content search operate over explicitly approved local
sources. They preserve source identity, freshness and bounded extraction lineage.
They do not grant arbitrary filesystem access or publish files to an Agent.
Synthetic regression fixtures are included; real documents and prior deployment
receipts are excluded. See architecture.md and privacy.md for current boundaries.

Expired extraction claims are reconciled during status reads and a new selected
text request. Job and queue state recover in one transaction; committed content,
cursor history and charged budgets remain. A busy database defers this metadata
repair without waiting for another writer. Recovery does not read an original:
indexing again requires a fresh explicit selection. A late result from the old
claim cannot replace the recovered job or its exact original-file reference.
