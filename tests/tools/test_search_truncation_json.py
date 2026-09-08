"""Regression tests for search_files truncation output staying pure JSON (#90322, #353).

The truncated-results hint used to be appended as plain text after the
serialized JSON payload (``{...}\\n\\n[Hint: ...]``), so the tool result was
no longer parseable JSON — downstream tool-message handling on providers
strict about tool-content formatting could reject or mishandle it, and
execute_code sandbox scripts calling search_files received a JSONDecodeError
instead of a dictionary.

The hint now rides inside the payload as a structured ``_hint`` field,
matching the existing ``_omitted``/``_warning`` side-channel convention in
the same function.
"""

import json
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest

import tools.file_tools  # registers search_files in registry
from tools.code_execution_tool import execute_code
from tools.file_tools import search_tool


class _FakeSearchResult:
    """Minimal stand-in for FileOperations.search return value."""

    def __init__(self, truncated=False):
        self.matches = []
        self._truncated = truncated

    def to_dict(self, densify=False) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"matches": [{"file": "test.py", "line": 1, "text": "match"}]}
        if self._truncated:
            payload["truncated"] = True
            payload["total_count"] = 20
        return payload


def _make_fake_file_ops(truncated):
    fake = MagicMock()
    fake.search = lambda **kw: _FakeSearchResult(truncated=truncated)
    return fake


class TestSearchTruncationStaysJson:
    """Unit tests for search_tool truncation output shape (AC-353-1)."""

    def setup_method(self):
        from tools.file_tools import _read_tracker
        _read_tracker.clear()

    @patch("tools.file_tools._get_file_ops", return_value=_make_fake_file_ops(True))
    def test_truncated_result_is_parseable_json(self, _mock_ops):
        """The whole tool output must round-trip through json.loads — no
        text appended after the serialized payload."""
        raw = search_tool("def main", offset=0, limit=20, task_id="t1")
        # On unpatched base, this raises json.JSONDecodeError due to trailing "[Hint: ...]"
        parsed = json.loads(raw)
        assert parsed["truncated"] is True
        assert not raw.endswith("]")
        assert "[Hint:" not in raw

    @patch("tools.file_tools._get_file_ops", return_value=_make_fake_file_ops(True))
    def test_truncation_hint_is_a_structured_field(self, _mock_ops):
        raw = search_tool("def main", offset=0, limit=20, task_id="t1")
        parsed = json.loads(raw)
        assert "_hint" in parsed
        assert "offset=20" in parsed["_hint"]
        assert parsed["_hint"] == (
            "Results truncated. Use offset=20 to see more, "
            "or narrow with a more specific pattern or file_glob."
        )

    @patch("tools.file_tools._get_file_ops", return_value=_make_fake_file_ops(True))
    def test_truncation_hint_with_nonzero_offset(self, _mock_ops):
        raw = search_tool("def main", offset=20, limit=20, task_id="t1")
        parsed = json.loads(raw)
        assert "_hint" in parsed
        assert "offset=40" in parsed["_hint"]

    @patch("tools.file_tools._get_file_ops", return_value=_make_fake_file_ops(False))
    def test_untruncated_result_has_no_hint(self, _mock_ops):
        raw = search_tool("def main", task_id="t1")
        parsed = json.loads(raw)
        assert "truncated" not in parsed
        assert "_hint" not in parsed


class TestSearchFilesExecuteCodeBridge:
    """Execute_code bridge tests (AC-353-2, AC-PRES-2)."""

    def test_search_files_providers_shape_truncated_bridge(self, tmp_path):
        """Reproduce search_files(pattern='*providers*', target='files', limit=15)
        in a temporary tree. Calling through execute_code must return a dict without
        JSONDecodeError, with truncated=True and _hint pointing to next offset."""
        providers_dir = tmp_path / "providers"
        providers_dir.mkdir()
        for i in range(25):
            (providers_dir / f"provider_{i:02d}.py").write_text(f"# provider {i}\n")

        code = (
            "import json\n"
            "from hermes_tools import search_files\n"
            f"res = search_files(pattern='*provider*', target='files', path={json.dumps(str(providers_dir))}, limit=15)\n"
            "if not isinstance(res, dict):\n"
            "    raise TypeError(f'Expected dict, got {type(res)}: {res!r}')\n"
            "if not res.get('truncated'):\n"
            "    raise AssertionError(f'Expected truncated=True, got {res}')\n"
            "if res.get('total_count') < 15:\n"
            "    raise AssertionError(f'Expected total_count >= 15, got {res.get(\"total_count\")}')\n"
            "if len(res.get('files', [])) != 15:\n"
            "    raise AssertionError(f'Expected 15 files, got {len(res.get(\"files\", []))}')\n"
            "hint = res.get('_hint', '')\n"
            "if 'offset=15' not in hint:\n"
            "    raise AssertionError(f'Expected offset=15 in _hint, got {hint!r}')\n"
            "print('OK:TRUNCATED_DICT_VERIFIED')\n"
        )
        res_raw = execute_code(
            code=code,
            task_id="test-bridge-trunc",
            enabled_tools=["search_files"],
        )
        res_dict = json.loads(res_raw)
        assert res_dict.get("status") == "success", f"execute_code failed: {res_dict.get('error') or res_dict.get('output')}"
        assert "OK:TRUNCATED_DICT_VERIFIED" in res_dict.get("output", "")

    def test_search_files_untruncated_control_bridge(self, tmp_path):
        """Non-truncated control remains unchanged in fields and parseable."""
        providers_dir = tmp_path / "providers"
        providers_dir.mkdir(exist_ok=True)
        for i in range(5):
            (providers_dir / f"ctrl_provider_{i:02d}.py").write_text(f"# provider {i}\n")

        code = (
            "import json\n"
            "from hermes_tools import search_files\n"
            f"res = search_files(pattern='*ctrl_provider*', target='files', path={json.dumps(str(providers_dir))}, limit=15)\n"
            "if not isinstance(res, dict):\n"
            "    raise TypeError(f'Expected dict, got {type(res)}: {res!r}')\n"
            "if res.get('truncated'):\n"
            "    raise AssertionError(f'Expected truncated falsy, got {res}')\n"
            "if res.get('total_count') != 5:\n"
            "    raise AssertionError(f'Expected total_count=5, got {res.get(\"total_count\")}')\n"
            "if len(res.get('files', [])) != 5:\n"
            "    raise AssertionError(f'Expected 5 files, got {len(res.get(\"files\", []))}')\n"
            "if '_hint' in res:\n"
            "    raise AssertionError(f'Expected no _hint, got {res[\"_hint\"]}')\n"
            "print('OK:CONTROL_DICT_VERIFIED')\n"
        )
        res_raw = execute_code(
            code=code,
            task_id="test-bridge-ctrl",
            enabled_tools=["search_files"],
        )
        res_dict = json.loads(res_raw)
        assert res_dict.get("status") == "success", f"execute_code failed: {res_dict.get('error') or res_dict.get('output')}"
        assert "OK:CONTROL_DICT_VERIFIED" in res_dict.get("output", "")
