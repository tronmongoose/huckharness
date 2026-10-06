# Harness eval baseline: model `default-local`

- tasks: 11, repeats/task: 1
- resolved-rate (majority): 91% (10/11)
- median steps (resolved): 3.0

| task | pass/runs | pass frac | steps | wall s | edit err | bash harness err | repairs | note |
|---|---|---|---|---|---|---|---|---|
| add-sub | 1/1 | 1.00 | 4 | 45.5 | 0 | 0 | 1 |  |
| buried-bug | 1/1 | 1.00 | 3 | 33.3 | 0 | 0 | 0 |  |
| dedup-order | 1/1 | 1.00 | 3 | 35.5 | 0 | 0 | 0 |  |
| fix-add-bug | 1/1 | 1.00 | 3 | 30.0 | 0 | 0 | 0 |  |
| implement-palindrome | 1/1 | 1.00 | 3 | 30.5 | 0 | 0 | 0 |  |
| mutable-default | 0/1 | 0.00 | None | 76.7 | 4 | 0 | 1 | exit 1 (want 0): Traceback (most recent call last): File "<s |
| off-by-one | 1/1 | 1.00 | 3 | 29.5 | 0 | 0 | 0 |  |
| remove-unused-import | 1/1 | 1.00 | 5 | 30.8 | 0 | 0 | 0 |  |
| safe-get | 1/1 | 1.00 | 5 | 32.5 | 0 | 0 | 0 |  |
| skip-none | 1/1 | 1.00 | 3 | 30.2 | 0 | 0 | 0 |  |
| two-file-change | 1/1 | 1.00 | 10 | 75.9 | 0 | 0 | 3 |  |

Fingerprint:

- harness_sha: 366e4b078a09e7731c18b663808f09f84d556a5a
- harness_dirty: True
- python: 3.14.4
- ollama_version: 0.20.0
- model: mistral-small3.2:latest
- model_digest: modelfile-sha256:5c7d0a3fc5527c9849165248adcc86f1bfa1239e48857718f40599c8d0746145
