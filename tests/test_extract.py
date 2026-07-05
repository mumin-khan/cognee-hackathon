"""Tests for rca.extract: tree-sitter extraction of functions/classes/imports
with correct line ranges, for both Python and JS fixture files.

Per SPEC.md:
    def extract_file(path: str, source: str, language: str) -> ExtractedFile
        # -> CodeFile + FunctionDefinition/ClassDefinition/ImportStatement with line ranges
    def symbols_for_lines(extracted: ExtractedFile, lines: set[int]) -> list[Symbol]

These are pure extraction tests: no cognee graph/dataset involved, so they
run without the `graph` marker and without `clean_cognee`.
"""

from __future__ import annotations

from tests.fixtures.repo_builder import (
    _API_PY_V1,
    _CLIENT_JS_V1,
    _DB_PY_V1,
    _HELPERS_JS_V1,
    _PAYMENTS_PY_V1,
    _UTILS_PY_V1,
)

from rca.extract import extract_file, symbols_for_lines


def _names(defs) -> set[str]:
    return {d.name for d in defs}


class TestExtractPython:
    def test_payments_py_functions(self):
        extracted = extract_file("app/payments.py", _PAYMENTS_PY_V1, "python")

        assert extracted.code_file.file_path == "app/payments.py"
        assert extracted.code_file.language == "python"

        fn_names = _names(extracted.functions)
        assert fn_names == {"validate_card", "charge_card", "process_payment"}

        # No classes in this fixture file.
        assert extracted.classes == []

        # Imports: `from app.db import save_record`, `from app.utils import format_currency`.
        import_modules = {imp.module for imp in extracted.imports}
        assert "app.db" in import_modules
        assert "app.utils" in import_modules

    def test_payments_py_line_ranges(self):
        extracted = extract_file("app/payments.py", _PAYMENTS_PY_V1, "python")
        by_name = {fn.name: fn for fn in extracted.functions}

        # def validate_card(card):  -> lines 7-9 (body ends before the two blank lines)
        assert by_name["validate_card"].start_line == 7
        assert by_name["validate_card"].end_line == 9

        # def charge_card(card, amount): -> lines 12-14
        assert by_name["charge_card"].start_line == 12
        assert by_name["charge_card"].end_line == 14

        # def process_payment(order): -> lines 17-25 (last line of file)
        assert by_name["process_payment"].start_line == 17
        assert by_name["process_payment"].end_line == 25

    def test_utils_py_functions_and_ranges(self):
        extracted = extract_file("app/utils.py", _UTILS_PY_V1, "python")
        fn_names = _names(extracted.functions)
        assert fn_names == {"format_currency", "retry"}

        by_name = {fn.name: fn for fn in extracted.functions}
        assert by_name["format_currency"].start_line == 4
        assert by_name["format_currency"].end_line == 6
        assert by_name["retry"].start_line == 9
        assert by_name["retry"].end_line == 17

    def test_db_py_functions(self):
        extracted = extract_file("app/db.py", _DB_PY_V1, "python")
        assert _names(extracted.functions) == {"get_connection", "save_record"}
        assert extracted.imports == []

    def test_api_py_imports_and_functions(self):
        extracted = extract_file("app/api.py", _API_PY_V1, "python")
        assert _names(extracted.functions) == {"handle_checkout"}
        import_modules = {imp.module for imp in extracted.imports}
        assert "app.payments" in import_modules

    def test_symbols_for_lines_hits_enclosing_function(self):
        extracted = extract_file("app/payments.py", _PAYMENTS_PY_V1, "python")

        # Line 22 is inside process_payment's body.
        symbols = symbols_for_lines(extracted, {22})
        names = {s.name for s in symbols}
        assert "process_payment" in names
        assert "validate_card" not in names
        assert "charge_card" not in names

    def test_symbols_for_lines_multiple_functions(self):
        extracted = extract_file("app/payments.py", _PAYMENTS_PY_V1, "python")

        # Line 8 -> validate_card body, line 22 -> process_payment body.
        symbols = symbols_for_lines(extracted, {8, 22})
        names = {s.name for s in symbols}
        assert names == {"validate_card", "process_payment"}

    def test_symbols_for_lines_no_match_outside_any_def(self):
        extracted = extract_file("app/payments.py", _PAYMENTS_PY_V1, "python")

        # Line 1 is the module docstring, outside every function.
        symbols = symbols_for_lines(extracted, {1})
        assert symbols == []


class TestExtractJavaScript:
    def test_helpers_js_functions(self):
        extracted = extract_file("web/helpers.js", _HELPERS_JS_V1, "javascript")

        assert extracted.code_file.file_path == "web/helpers.js"
        assert extracted.code_file.language == "javascript"

        fn_names = _names(extracted.functions)
        assert fn_names == {"formatPrice", "logEvent"}

    def test_helpers_js_line_ranges(self):
        extracted = extract_file("web/helpers.js", _HELPERS_JS_V1, "javascript")
        by_name = {fn.name: fn for fn in extracted.functions}

        # function formatPrice(cents) { ... } -> lines 3-5
        assert by_name["formatPrice"].start_line == 3
        assert by_name["formatPrice"].end_line == 5

        # function logEvent(name) { ... } -> lines 7-9
        assert by_name["logEvent"].start_line == 7
        assert by_name["logEvent"].end_line == 9

    def test_client_js_imports_helpers(self):
        extracted = extract_file("web/client.js", _CLIENT_JS_V1, "javascript")

        assert _names(extracted.functions) == {"submitOrder"}
        # const { formatPrice, logEvent } = require("./helpers.js");
        import_modules = {imp.module for imp in extracted.imports}
        assert any("helpers" in mod for mod in import_modules)

    def test_symbols_for_lines_js(self):
        extracted = extract_file("web/client.js", _CLIENT_JS_V1, "javascript")

        # Line 6 is inside submitOrder's body.
        symbols = symbols_for_lines(extracted, {6})
        names = {s.name for s in symbols}
        assert names == {"submitOrder"}
