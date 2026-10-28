"""Browser launch follows the actual listener, never just application startup."""

import asyncio
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import socket
import unittest
from unittest.mock import patch

from fastapi import FastAPI

from trading_companion.api_server import _run_browser_server, main


class BrowserStartupTests(unittest.TestCase):
    def setUp(self):
        self.app = FastAPI()
        self.server = None

    def free_port(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            return listener.getsockname()[1]

    def serve_once(self, server):
        self.server = server

        async def exercise():
            server.config.load()
            server.lifespan = server.config.lifespan_class(server.config)
            try:
                await server.startup()
                if server.started:
                    # Also verify the listener survives a browser launch failure.
                    self.assert_listening(server.config.port)
            finally:
                if server.started:
                    await server.shutdown()

        asyncio.run(exercise())

    def assert_listening(self, port):
        self.assertTrue(self.server.started)
        self.assertTrue(self.server.servers)
        listeners = [sock for server in self.server.servers for sock in server.sockets]
        self.assertTrue(listeners)
        self.assertTrue(all(sock.fileno() >= 0 for sock in listeners))
        self.assertEqual({sock.getsockname()[:2] for sock in listeners}, {("127.0.0.1", port)})

    def test_browser_opens_once_at_selected_port_after_listener_is_bound(self):
        port = self.free_port()
        fetched = []

        def open_browser(url, *, new):
            self.assert_listening(port)
            fetched.append(port)
            return True

        with patch("uvicorn.Server.run", new=lambda server: self.serve_once(server)), \
                patch("webbrowser.open", side_effect=open_browser) as opened:
            _run_browser_server(self.app, port=port)
        opened.assert_called_once_with(f"http://127.0.0.1:{port}/", new=2)
        self.assertEqual(fetched, [port])

    def test_occupied_port_never_opens_an_existing_service(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            port = listener.getsockname()[1]
            with patch("uvicorn.Server.run", new=lambda server: self.serve_once(server)), patch("webbrowser.open") as opened, \
                    self.assertRaises(SystemExit) as caught:
                _run_browser_server(self.app, port=port)
            self.assertNotEqual(caught.exception.code, 0)
            opened.assert_not_called()

    def test_browser_failures_leave_the_server_running_and_print_the_url(self):
        for failure in (False, OSError("private browser details")):
            port = self.free_port()
            options = {"side_effect": failure} if isinstance(failure, Exception) else {"return_value": failure}
            with self.subTest(failure=type(failure).__name__), \
                    patch("uvicorn.Server.run", new=lambda server: self.serve_once(server)), \
                    patch("webbrowser.open", **options), redirect_stderr(StringIO()) as output:
                _run_browser_server(self.app, port=port)
            self.assertIn(f"http://127.0.0.1:{port}/", output.getvalue())
            self.assertNotIn("private browser details", output.getvalue())

    def test_browser_option_reaches_launcher_and_can_be_disabled(self):
        for flags, expected in ((["--open-browser"], True), (["--open-browser", "--no-open-browser"], False)):
            with self.subTest(flags=flags), redirect_stdout(StringIO()), patch("uvicorn.run") as regular, \
                    patch("trading_companion.api_server._run_browser_server") as browser:
                self.assertEqual(main(["--demo", "--port", "8123", *flags]), 0)
            if expected:
                self.assertEqual(browser.call_args.kwargs, {"port": 8123})
                regular.assert_not_called()
            else:
                browser.assert_not_called()
                regular.assert_called_once()

    def test_help_and_bad_port_do_not_open_a_browser(self):
        for flags, code in ((["--help"], 0), (["--demo", "--port", "0"], 2)):
            with self.subTest(flags=flags), redirect_stdout(StringIO()), redirect_stderr(StringIO()), \
                    patch("webbrowser.open") as opened, self.assertRaises(SystemExit) as caught:
                main(["--open-browser", *flags])
            self.assertEqual(caught.exception.code, code)
            opened.assert_not_called()

    def test_configuration_failure_does_not_attempt_browser_startup(self):
        from trading_companion.model import ModelConfigurationError

        with redirect_stdout(StringIO()), redirect_stderr(StringIO()), patch(
                "trading_companion.model_registry.configured_models", side_effect=ModelConfigurationError("fixture")), \
                patch("trading_companion.api_server._run_browser_server") as browser:
            self.assertEqual(main(["--data", "synthetic", "--open-browser"]), 2)
        browser.assert_not_called()


if __name__ == "__main__":
    unittest.main()
