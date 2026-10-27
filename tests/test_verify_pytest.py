"""The shared gate must execute pytest functions without weakening offline acceptance."""

from contextlib import ExitStack, redirect_stdout
from io import StringIO
import os
from pathlib import Path
import tempfile
import textwrap
import unittest
from unittest.mock import patch

from scripts import verify


class StrictPytestFunctionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="tc-pytest-gate-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)

    def run_source(self, source, *, conftest=None):
        path = self.directory / "test_function_fixture.py"
        path.write_text(textwrap.dedent(source), encoding="utf-8")
        if conftest is not None:
            (self.directory / "conftest.py").write_text(textwrap.dedent(conftest), encoding="utf-8")
        return verify.run_strict_pytest((path,), stream=StringIO())

    def test_parameterized_functions_fixtures_and_plain_pytest_classes_execute(self):
        count = self.run_source('''
            import pytest
            calls = []

            @pytest.fixture
            def number():
                yield 7
                calls.append("teardown")

            @pytest.mark.parametrize("value", [1, 2, 3])
            def test_parameter(value, number):
                assert number == 7
                assert value in [1, 2, 3]

            class TestPlainClass:
                def test_fixture_was_finalized(self):
                    assert calls == ["teardown"] * 3
        ''')
        self.assertEqual(count, 4)

    def test_unittest_cases_are_never_executed_by_pytest_channel(self):
        self.assertEqual(self.run_source('''
            import unittest
            class TestAlreadyRun(unittest.TestCase):
                @classmethod
                def setUpClass(cls):
                    raise AssertionError("unittest was executed twice")
                def test_not_for_this_channel(self):
                    self.fail("unittest was executed twice")
            def test_function():
                assert True
        '''), 1)

    def test_legacy_test_helpers_remain_importable_without_repeating_unittest(self):
        self.assertEqual(self.run_source('''
            from test_research_workflow import WorkflowScriptModel
            def test_existing_helper():
                assert callable(WorkflowScriptModel)
        '''), 1)

    def test_passing_only_unittest_cases_cannot_fake_function_coverage(self):
        with self.assertRaises(verify.VerificationError):
            self.run_source('''
                import unittest
                class TestOnlyUnittest(unittest.TestCase):
                    def test_method(self):
                        assert True
            ''')

    def test_empty_collection_is_rejected(self):
        with self.assertRaises(verify.VerificationError):
            self.run_source("def helper():\n    pass\n")
        with self.assertRaises(verify.VerificationError):
            verify.run_strict_pytest((), stream=StringIO())

    def test_assertion_errors_are_rejected(self):
        with self.assertRaises(verify.VerificationError):
            self.run_source("def test_failure():\n    assert False\n")

    def test_fixture_setup_failure_is_rejected(self):
        with self.assertRaises(verify.VerificationError):
            self.run_source('''
                import pytest
                @pytest.fixture
                def broken():
                    raise ValueError("artificial setup failure")
                def test_case(broken):
                    pass
            ''')

    def test_fixture_teardown_failure_is_rejected_even_after_passing_call(self):
        with self.assertRaises(verify.VerificationError):
            self.run_source('''
                import pytest
                @pytest.fixture
                def broken():
                    yield
                    raise ValueError("artificial teardown failure")
                def test_case(broken):
                    assert True
            ''')

    def test_explicit_skip_and_fixture_skip_are_rejected(self):
        sources = ('''
            import pytest
            @pytest.mark.skip(reason="artificial skip")
            def test_skipped():
                pass
        ''', '''
            import pytest
            @pytest.fixture
            def absent():
                pytest.skip("artificial missing dependency")
            def test_missing(absent):
                pass
        ''')
        for source in sources:
            with self.subTest(source=source), self.assertRaises(verify.VerificationError):
                self.run_source(source)

    def test_xfail_and_xpass_are_rejected_including_non_strict_markers(self):
        for assertion in ("True", "False"):
            with self.subTest(assertion=assertion), self.assertRaises(verify.VerificationError):
                self.run_source(f'''
                    import pytest
                    @pytest.mark.xfail(reason="artificial expected failure", strict=False)
                    def test_expected():
                        assert {assertion}
                ''')

    def test_module_level_skip_cannot_hide_behind_a_passing_module(self):
        skipped = self.directory / "test_skipped_module.py"
        skipped.write_text('import pytest\npytest.skip("artificial module skip", allow_module_level=True)\n')
        passing = self.directory / "test_passing_module.py"
        passing.write_text("def test_pass():\n    assert True\n")
        with self.assertRaises(verify.VerificationError):
            verify.run_strict_pytest((skipped, passing), stream=StringIO())

    def test_collection_error_cannot_hide_behind_a_passing_test(self):
        broken = self.directory / "test_broken_module.py"
        broken.write_text("def test_invalid(:\n    pass\n")
        passing = self.directory / "test_passing_module.py"
        passing.write_text("def test_pass():\n    assert True\n")
        with self.assertRaises(verify.VerificationError):
            verify.run_strict_pytest((broken, passing), stream=StringIO())

    def test_collection_hook_cannot_silently_drop_one_function(self):
        with self.assertRaises(verify.VerificationError):
            self.run_source('''
                def test_first():
                    assert True
                def test_second():
                    assert True
            ''', conftest='''
                def pytest_collection_modifyitems(items):
                    items.pop()
            ''')

    def test_machine_plugin_and_test_selection_environment_cannot_change_execution(self):
        with patch.dict(os.environ, {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "0", "PYTEST_PLUGINS": "nonexistent_untrusted_plugin",
                                     "PYTEST_ADDOPTS": "--collect-only"}):
            self.assertEqual(self.run_source('''
                import os
                def test_environment():
                    assert os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"
                    assert os.environ["PYTEST_PLUGINS"] == ""
                    assert os.environ["PYTEST_ADDOPTS"] == ""
            '''), 1)
            self.assertEqual(os.environ["PYTEST_PLUGINS"], "nonexistent_untrusted_plugin")

    def test_existing_clean_environment_remains_effective_inside_function_channel(self):
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "artificial", "HTTPS_PROXY": "artificial"}):
            with verify.clean_environment(), verify.offline_network():
                self.assertEqual(self.run_source('''
                    import os
                    def test_private_configuration_is_not_forwarded():
                        assert "DEEPSEEK_API_KEY" not in os.environ
                        assert "HTTPS_PROXY" not in os.environ
                        assert os.environ["PYTHON_DOTENV_DISABLED"] == "1"
                '''), 1)

    def test_caught_network_attempt_still_fails_surrounding_offline_gate(self):
        with self.assertRaises(verify.VerificationError):
            with verify.clean_environment(), verify.offline_network():
                self.run_source('''
                    import socket
                    import pytest
                    from scripts.verify import VerificationError
                    def test_caught_network():
                        with pytest.raises(VerificationError):
                            socket.getaddrinfo("must-not-resolve.invalid", 443)
                ''')


class PythonChannelIntegrationTests(unittest.TestCase):
    def test_function_channel_is_required_inside_same_clean_offline_context(self):
        observed = []
        def run_functions():
            observed.append("functions")
            self.assertNotIn("DEEPSEEK_API_KEY", os.environ)
            self.assertEqual(os.environ["PYTHON_DOTENV_DISABLED"], "1")
            self.assertNotEqual(verify.socket.getaddrinfo.__module__, "socket")
            return 4
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {"DEEPSEEK_API_KEY": "artificial"}))
            stack.enter_context(patch.object(unittest.defaultTestLoader, "discover", return_value=unittest.TestSuite()))
            suite = stack.enter_context(patch.object(verify, "run_strict_suite", return_value=3))
            functions = stack.enter_context(patch.object(verify, "run_strict_pytest", side_effect=run_functions))
            stack.enter_context(patch("trading_companion.agent_evaluation.run_evaluation", return_value={}))
            stack.enter_context(patch("trading_companion.mvp_evaluation.run_mvp_evaluation", return_value={}))
            stack.enter_context(patch("scripts.evaluate_company_research.evaluate_company_research", return_value={}))
            for name in ("check_evaluation", "check_mvp_evaluation", "check_company_evaluation"):
                stack.enter_context(patch.object(verify, name))
            stack.enter_context(patch("trading_companion.api_demo.main", return_value=0))
            output = stack.enter_context(redirect_stdout(StringIO()))
            verify.python_checks()
        suite.assert_called_once()
        functions.assert_called_once_with()
        self.assertEqual(observed, ["functions"])
        self.assertIn("3 unittest tests and 4 pytest function cases", output.getvalue())


if __name__ == "__main__":
    unittest.main()
