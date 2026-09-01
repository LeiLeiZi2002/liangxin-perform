import importlib.util
import json
from pathlib import Path


def _load_script_module():
    script_path = (
        Path(__file__).parents[3] / "scripts" / "run-short-character-blackbox.py"
    )
    spec = importlib.util.spec_from_file_location(
        "run_short_character_blackbox",
        script_path,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_blackbox_results_are_kept_in_separate_route_files(tmp_path: Path) -> None:
    module = _load_script_module()
    results = [
        {"name": "open", "session_id": "open-session"},
        {"name": "harmful", "session_id": "harmful-session"},
    ]

    written = module.write_route_results(results, output_dir=tmp_path)

    assert written == [
        tmp_path / "short-character-blackbox-open.json",
        tmp_path / "short-character-blackbox-harmful.json",
    ]
    assert json.loads(written[0].read_text(encoding="utf-8"))["session_id"] == (
        "open-session"
    )
    assert json.loads(written[1].read_text(encoding="utf-8"))["session_id"] == (
        "harmful-session"
    )
