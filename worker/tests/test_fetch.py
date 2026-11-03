"""Fetch adapter tests without platform requests or installed extractors."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from smt import fetch  # noqa: E402


class FakeInstaloader:
    def __init__(self, **kwargs):
        self.context = SimpleNamespace(_session=self.new_session(), get_json=lambda *a, **kw: None,
                                       _get_page=lambda *a, **kw: None,
                                       doc_id_graphql_query=lambda *a, **kw: None)

    @staticmethod
    def new_session():
        return SimpleNamespace(proxies={}, trust_env=True)

    def load_session_from_file(self, user, filename):
        # Instaloader replaces the HTTP session when loading saved cookies.
        self.context._session = self.new_session()
        self.context.username = user


class InstaloaderRouteTest(unittest.TestCase):
    def setUp(self):
        modules = patch.dict(sys.modules, {
            "instaloader": SimpleNamespace(Instaloader=FakeInstaloader),
            "instaloader.instaloadercontext": SimpleNamespace(RateController=object),
        })
        modules.start()
        self.addCleanup(modules.stop)

    def test_loaded_session_uses_the_requested_route(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_file = Path(tmp) / "session"
            session_file.touch()
            for proxy, expected in (("http://warp:1080", {"http": "http://warp:1080", "https": "http://warp:1080"}),
                                    (None, {})):
                with self.subTest(proxy=proxy):
                    loader = fetch._instaloader(proxy, lambda: None, session_file, "test-user")
                    self.assertEqual(loader.context.username, "test-user")
                    self.assertEqual(loader.context._session.proxies, expected)
                    self.assertFalse(loader.context._session.trust_env)

    def test_without_a_saved_session_uses_the_requested_route(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing-session"
            for proxy, expected in (("http://warp:1080", {"http": "http://warp:1080", "https": "http://warp:1080"}),
                                    (None, {})):
                for session_file, user in ((None, None), (missing, "test-user")):
                    with self.subTest(proxy=proxy, session=session_file):
                        loader = fetch._instaloader(proxy, lambda: None, session_file, user)
                        self.assertEqual(loader.context._session.proxies, expected)
                        self.assertFalse(loader.context._session.trust_env)


if __name__ == "__main__":
    unittest.main()
