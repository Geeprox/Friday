# Private local file services

File Steward and indexed content search operate over explicitly approved local
sources. They preserve source identity, freshness and bounded extraction lineage.
They do not grant arbitrary filesystem access or publish files to an Agent.
Synthetic regression fixtures are included; real documents and prior deployment
receipts are excluded. See architecture.md and privacy.md for current boundaries.

文件管家契约定义来源指纹、已索引单元预算与错误码。设置、队列、提取和查询基于同一契约，
通过文件管家服务组合。独立 Apple provider 继续从原服务模块导入同一错误类型。

Expired extraction claims are reconciled during status reads and a new selected
text request. Job and queue state recover in one transaction; committed content,
cursor history and charged budgets remain. A busy database defers this metadata
repair without waiting for another writer. Recovery does not read an original:
indexing again requires a fresh explicit selection. A late result from the old
claim cannot replace the recovered job or its exact original-file reference.
