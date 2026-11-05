# Python dependency management

## Source constraints and resolved lock

- `requirements.txt` is the human-maintained direct-dependency constraint file.
- `requirements.lock` is the resolved, hash-pinned dependency graph used by the API/worker Docker image and Root/V2 CI.
- The lock is generated with Python 3.11.17, pip 26.2.2, and pip-tools 7.6.2. CI regenerates it and diffs the committed file before tests.
- The lock contains SHA-256 hashes and must be installed with `pip install --require-hashes -r requirements.lock`.

## Refresh procedure

1. Edit direct dependency constraints in `requirements.txt`.
2. Use the pinned resolver toolchain:
   ```sh
   python -m pip install --upgrade pip==26.2.2 pip-tools==7.6.2
   python -m piptools compile --generate-hashes --resolver=backtracking --allow-unsafe --output-file=requirements.lock requirements.txt
   ```
3. Commit both `requirements.txt` and `requirements.lock`.
4. Run Root integration (Python 3.11), preserved V2 regression (Python 3.12), and Docker image build. CI rejects a stale lock or a dependency set that cannot install on either runtime.

## Scope boundary

The Root lock is shared by the Root API/worker image and the preserved V2 regression workflow. `workstreams/tgmate-v2/requirements.txt` remains the preserved V2 workstream's own package manifest; it is not the dependency source used by the Root Dockerfile or current Root/V2 CI workflows.
