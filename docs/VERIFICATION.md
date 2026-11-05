# Verification and evidence boundaries

Verified November 4, 2026 on macOS arm64 with Python 3.12.15.

| Check | Result | Fidelity |
|---|---|---|
| Original selected permission, authorized retrieval, embedding-permission, source-policy and chat-presentation tests | 304 passed | Synthetic source corpus and fake database/model ports; all socket connections refused by runner |
| Export manifest | All included source digests match | File integrity/provenance; source substitutions declared |
| Static privacy review | No client records, environment files, credentials or original private history in release | Allowlisted files plus independent review |

Reproduce with `.venv/bin/python tools/verify_offline.py` after installing pinned requirements. The tests are existing product tests copied from the original system, not rewritten to fit a new demo. Packaging initially exposed a missing schema reference and inconsistent identity substitutions; those export issues were corrected before this result. Original implementation and test assertions remain in place with consistent identity-label substitutions.

These are software verification results. They do not measure production usage, live connector behavior, live database RLS, retrieval quality on a customer corpus, model answer quality, latency or business impact. No model/API calls, customer-system access or new paid benchmark runs were performed. The complete production deployment and its data remain private.
