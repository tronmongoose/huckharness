"""Put the eval dir on sys.path so its tests import eval modules by bare name from the repo root.

Also point harness state and memory at temp dirs and ignore the operator's
settings: these tests run under `make test` but
outside coding_harness/tests, so that conftest's isolation does not reach them.
"""
import os
import sys
import tempfile
from pathlib import Path

os.environ["HARNESS_META_DIR"] = tempfile.mkdtemp(prefix="bjorn-eval-test-meta-")
os.environ["HARNESS_MEMORY_DIR"] = tempfile.mkdtemp(prefix="bjorn-eval-test-memory-")
os.environ["HARNESS_SETTINGS"] = "off"
# The developer's trust file is not test input either. Tests of the trust gate
# lift this with monkeypatch.delenv and point TRUST_FILE at a tmp path.
os.environ["HARNESS_TRUST_PROJECTS"] = "all"
sys.path.insert(0, str(Path(__file__).resolve().parent))
