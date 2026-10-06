"""Suite-wide defaults for the harness's own tests.

The green-before-done gate and the targeted tests stay off unless a test
opts in through the Session fields: a Session built with this repo as cwd
would otherwise discover this very suite and run it recursively from inside
each session test.
"""
import os
import tempfile

# Captured before anything below can change it, so test_state_isolation can
# compare test-visible paths against the operator's real config tree.
REAL_HOME = os.path.expanduser("~")

os.environ["HARNESS_DONE_GATE"] = "0"
os.environ["HARNESS_TARGETED_TESTS"] = "0"
# The developer's own ~/.config/bjorn/settings.json is not test input. A real
# PreToolUse hook there made build_hook_runner return a runner, which sent
# print_mode down its hooks branch and broke fakes that only implement run() —
# red on a machine with hooks configured, green everywhere else. Tests that
# exercise settings point USER_SETTINGS at a tmp path and override this.
os.environ["HARNESS_SETTINGS"] = "off"
# The developer's trust file is not test input either. Tests of the trust gate
# lift this with monkeypatch.delenv and point TRUST_FILE at a tmp path.
os.environ["HARNESS_TRUST_PROJECTS"] = "all"
# Session transcripts, snapshots and audit logs default to the real vault.
# Tests wrote thousands of transcripts there, and the GUI's history list reads
# that directory, so every test run showed up as a past session.
os.environ["HARNESS_META_DIR"] = tempfile.mkdtemp(prefix="bjorn-test-meta-")
# Memory writes default to ~/.config/bjorn/memory, the operator's real corpus.
# Tests that pin the HOME-derived default lift this with monkeypatch.delenv.
os.environ["HARNESS_MEMORY_DIR"] = tempfile.mkdtemp(prefix="bjorn-test-memory-")
