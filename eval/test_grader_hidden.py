"""Tests for the hidden_tests grader and the hidden-tests corpus layout. No model runs."""
import json
from pathlib import Path

from grader import grade
from run import TASKS_DIR, _load_tasks

PASSING = "def test_ok():\n    assert True\n"
FAILING = "def test_bad():\n    assert False\n"


def _task(tmp_path: Path, hidden_src: str) -> tuple[dict, Path]:
    task_dir = tmp_path / "task"
    (task_dir / "hidden" / "tests").mkdir(parents=True)
    (task_dir / "hidden" / "tests" / "test_x.py").write_text(hidden_src, encoding="utf-8")
    wd = tmp_path / "wd"
    (wd / "tests").mkdir(parents=True)
    spec = {"type": "hidden_tests", "files": ["tests/test_x.py"],
            "cmd": "python3 -m pytest tests/test_x.py -q -p no:cacheprovider",
            "task_dir": str(task_dir)}
    return spec, wd


def test_hidden_file_overwrites_the_agents_copy(tmp_path):
    spec, wd = _task(tmp_path, PASSING)
    (wd / "tests" / "test_x.py").write_text(FAILING, encoding="utf-8")
    ok, detail = grade(spec, wd)
    assert ok, detail
    assert (wd / "tests" / "test_x.py").read_text(encoding="utf-8") == PASSING


def test_hidden_failure_fails_the_task(tmp_path):
    spec, wd = _task(tmp_path, FAILING)
    ok, detail = grade(spec, wd)
    assert not ok and detail.startswith("exit 1")


def test_missing_task_dir_is_a_grader_error_not_a_crash(tmp_path):
    spec, wd = _task(tmp_path, PASSING)
    del spec["task_dir"]
    ok, detail = grade(spec, wd)
    assert not ok and detail.startswith("grader error")


def test_load_tasks_stamps_task_dir():
    for task in _load_tasks([]):
        assert task["grade"]["task_dir"] == str(task["_dir"].resolve())


def test_hidden_tasks_list_only_hidden_files_that_exist():
    for tj in sorted(TASKS_DIR.glob("*/task.json")):
        spec = json.loads(tj.read_text(encoding="utf-8"))
        assert not (tj.parent / "repo" / ".git").exists()
        if spec["grade"]["type"] != "hidden_tests":
            continue
        for rel in spec["grade"]["files"]:
            assert (tj.parent / "hidden" / rel).is_file(), rel
            assert rel in spec["grade"]["cmd"]
