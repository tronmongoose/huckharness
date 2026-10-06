# Harness eval baseline: model `default-local`

- tasks: 11, repeats/task: 3
- resolved-rate (majority): 82% (9/11)
- median steps (resolved): 3

| task | pass/runs | pass frac | steps | wall s | edit err | bash harness err | repairs | note |
|---|---|---|---|---|---|---|---|---|
| add-sub | 3/3 | 1.00 | 3 | 55.0 | 1 | 0 | 0 |  |
| buried-bug | 2/3 | 0.67 | 5.0 | 61.9 | 2 | 0 | 0 |  |
| dedup-order | 3/3 | 1.00 | 3 | 42.3 | 0 | 0 | 0 |  |
| fix-add-bug | 3/3 | 1.00 | 3 | 34.5 | 0 | 0 | 0 |  |
| implement-palindrome | 2/3 | 0.67 | 3.0 | 46.8 | 0 | 0 | 2 |  |
| mutable-default | 1/3 | 0.33 | 3 | 45.3 | 0 | 0 | 0 | exit 0 (want 0): |
| off-by-one | 3/3 | 1.00 | 3 | 35.8 | 0 | 0 | 0 |  |
| remove-unused-import | 3/3 | 1.00 | 5 | 37.5 | 0 | 0 | 0 |  |
| safe-get | 3/3 | 1.00 | 5 | 38.4 | 0 | 0 | 0 |  |
| skip-none | 3/3 | 1.00 | 3 | 34.6 | 0 | 0 | 0 |  |
| two-file-change | 1/3 | 0.33 | 17 | 125.7 | 7 | 0 | 6 | exit 1 (want 0): Traceback (most recent call last): File "/p |

Fingerprint:

- harness_sha: a5b3700fcb4d052d1ba0d0267ba279d05d048dc4
- harness_dirty: False
- python: 3.14.7
- ollama_version: 0.34.4
- model: mistral-small3.2:latest
- model_digest: modelfile-sha256:5c7d0a3fc5527c9849165248adcc86f1bfa1239e48857718f40599c8d0746145
- profile_hash: 03697f6cda48
