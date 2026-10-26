# Development

Read AGENTS.md. Use locked dependencies and synthetic fixtures. Do not create a
new household session or run models/hardware for ordinary test verification.

Runtime events use fixed, bounded codes and do not log secrets, raw transcripts,
media, names or exception bodies. Put generated receipts under .artifacts/.
Privacy tests must verify actual data boundaries, not only UI wording.

The regression environment is Python 3.12 (including AST contract pins). Create it
with `uv sync --locked --python 3.12 --group dev --extra airplay --extra nas-connection`.
Install the optional Geek worker test dependencies with
`npm --prefix workers/geek ci --ignore-scripts` before the complete suite.
The basic Console installation still supports Python 3.11+.
