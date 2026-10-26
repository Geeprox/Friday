# Contributing

Use synthetic fixtures. Keep household-specific configuration, device models,
serials, network addresses and geometry out of implementation decisions. Extend
capability discovery and test at least two different registries/spec mappings.

Install the locked development environment and run the commands in README.md.
Include regression tests for privacy, authority and device-routing changes. Put
all generated evidence in .artifacts/. Run scripts/audit_publication.py before
submitting; do not paste its operator-local private-pattern file into a PR.
