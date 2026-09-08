"""Execute the helper usage taught by the public schema and recovery hints."""

import json
import re

import pytest

from tools import code_execution_tool
from tools.code_execution_tool import (
    _sandbox_failure_hint,
    build_execute_code_schema,
    execute_code,
    generate_hermes_tools_module,
)


PROBE = '''
from __future__ import annotations
{imports}
import json
print(json.dumps([json_parse('{{"value": 7}}')["value"],
                  shell_quote("two words"), retry(lambda: "ok", delay=0)]))
'''


@pytest.fixture(autouse=True)
def clean_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    yield


def run(code):
    raw = execute_code(code, task_id="helper-contract")
    return json.loads(raw)


def test_schema_helper_instructions_work():
    schema = build_execute_code_schema()
    assert schema is not None
    description = schema["description"]

    # Invariant: schema must state explicit import requirement and not advertise built-in no-import helpers
    assert "Helpers require imports: `from hermes_tools import json_parse, shell_quote, retry`." in description
    assert "Built-in helpers (no import)" not in description
    assert "built-in helpers" not in description.lower()

    matches = re.findall(r"`(from hermes_tools import [\w, ]+)`", description)
    assert matches, f"No hermes_tools import instruction found in description: {description}"
    imports = "\n".join(matches)

    result = run(PROBE.format(imports=imports))
    assert result["status"] == "success", result
    assert json.loads(result["output"]) == [7, "'two words'", "ok"]


def test_missing_helper_recovery_instructions_execute():
    for helper in ("json_parse", "shell_quote", "retry"):
        failed = run(f"print({helper})")
        assert failed["status"] == "error", failed
        hint = failed.get("hint", "")
        assert f"Import {helper} before calling it: from hermes_tools import {helper}" in hint
        assert "directly" not in hint
        assert "built into" not in hint

        instruction = re.search(r"from hermes_tools import \w+", hint)
        assert instruction, failed
        recovered = run(instruction.group() + f"\nprint(callable({helper}))")
        assert recovered["status"] == "success", recovered
        assert recovered["output"].strip() == "True"

        skew = _sandbox_failure_hint(
            f"ImportError: cannot import name '{helper}' from 'hermes_tools'")
        assert skew is not None
        assert instruction.group() in skew
        assert "sys.path" in skew
        assert "stale" in skew
        assert "remove" not in skew.lower()
        assert "no import" not in skew.lower()


def test_generated_module_coverage_for_transports():
    for transport in ("uds", "file"):
        src = generate_hermes_tools_module(["terminal"], transport=transport)
        assert "def json_parse(" in src
        assert "def shell_quote(" in src
        assert "def retry(" in src
