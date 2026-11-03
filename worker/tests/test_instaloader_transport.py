"""Exercise the installed extractor at the HTTP boundary without network access."""

import json
import os
import pickle
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from smt import fetch

try:
    import instaloader
    import requests
except ImportError:
    instaloader = None


@unittest.skipUnless(instaloader, "Run in the worker image with locked dependencies")
class InstaloaderTransportTest(unittest.TestCase):
    def test_platform_challenge_stops_after_one_metadata_request(self):
        loader = fetch._instaloader("http://warp:1080", lambda: None, None, None)
        loader.context.username = "fixture-owner"
        loader.context.sleep = False
        loader.context._session.cookies.set("csrftoken", "fixture")
        loader.context._web_api_tokens = lambda: {"lsd": "fixture", "fb_dtsg": "fixture"}
        response = requests.Response()
        response.status_code = 400
        response.reason = "Bad Request"
        response.url = "https://www.instagram.com/api/graphql"
        response._content = b'{"status":"fail","message":"feedback_required"}'
        with patch.object(fetch, "_instaloader", return_value=loader), \
                patch("requests.adapters.HTTPAdapter.send", return_value=response) as send:
            with self.assertRaises(fetch.JobError) as caught:
                fetch.instagram_images("fixture", "http://warp:1080", lambda: None)
        self.assertEqual(caught.exception.cls, "challenge")
        self.assertEqual(send.call_count, 1)

    def test_owner_profile_uses_authenticated_id_and_checks_returned_username(self):
        loader = fetch._instaloader("http://warp:1080", lambda: None, None, None)
        loader.context.username = "fixture-owner"
        loader.context._session.cookies.set("ds_user_id", "123")
        for returned_username in ("fixture-owner", "different-owner"):
            profile = SimpleNamespace(username=returned_username, get_posts=lambda: [])
            with self.subTest(username=returned_username), \
                    patch.object(fetch, "_instaloader", return_value=loader), \
                    patch.object(instaloader.Profile, "from_id", return_value=profile) as by_id, \
                    patch.object(instaloader.Profile, "from_username") as by_name:
                if returned_username == "fixture-owner":
                    self.assertEqual(fetch.instagram_profile("fixture-owner", 3, None, "http://warp:1080",
                                                             lambda: None), [])
                else:
                    with self.assertRaises(fetch.JobError) as caught:
                        fetch.instagram_profile("fixture-owner", 3, None, "http://warp:1080", lambda: None)
                    self.assertEqual(caught.exception.cls, "login_required")
                by_id.assert_called_once_with(loader.context, 123)
                by_name.assert_not_called()

    def test_authenticated_metadata_uses_web_api_without_retry(self):
        loader = fetch._instaloader("http://warp:1080", lambda: None, None, None)
        context = loader.context
        context.username = "fixture-owner"
        context._session.cookies.set("csrftoken", "fixture")
        context.sleep = False
        context._web_api_tokens = lambda: {"lsd": "fixture", "fb_dtsg": "fixture"}
        sent = []

        def send(adapter, request, **kwargs):
            sent.append(request)
            response = requests.Response()
            response.status_code = 200
            response.url = request.url
            response._content = json.dumps({"status": "ok", "data": {
                "user": {"pk": "1", "username": "fixture-owner", "is_private": True},
                "xdt_api__v1__media__shortcode__web_info": {"items": [{
                    "pk": "2", "code": "fixture", "media_type": 1, "taken_at": 1,
                    "user": {"pk": "1", "username": "fixture-owner"}}]}}}).encode()
            return response

        with patch("requests.adapters.HTTPAdapter.send", send):
            profile = instaloader.Profile(context, {"id": "1", "username": "fixture-owner"})
            self.assertTrue(profile.is_private)
            post = instaloader.Post.from_shortcode(context, "fixture")
            self.assertFalse(post.is_video)
        self.assertEqual(len(sent), 2)
        for request in sent:
            self.assertEqual(request.url, "https://www.instagram.com/api/graphql")
            self.assertIn("fb_dtsg=fixture", request.body)

    def test_every_request_preserves_explicit_route(self):
        for proxy in ("http://warp:1080", None):
            with self.subTest(proxy=proxy), tempfile.TemporaryDirectory() as tmp:
                session = Path(tmp) / "session"
                session.write_bytes(pickle.dumps({"csrftoken": "fixture", "sessionid": "fixture"}))
                loader = fetch._instaloader(proxy, lambda: None, session, "fixture-owner")
                context = loader.context
                context.sleep = False
                context._web_api_tokens = lambda: {"lsd": "fixture", "fb_dtsg": "fixture"}
                sent = []

                def send(adapter, request, **kwargs):
                    sent.append((request.url, dict(kwargs.get("proxies") or {})))
                    response = requests.Response()
                    response.status_code = 200
                    response.url = request.url
                    response._content = (b"<html></html>" if request.url.endswith("/fixture-owner/")
                                         else json.dumps({"status": "ok", "data": {}}).encode())
                    return response

                with patch.dict(os.environ, {"HTTP_PROXY": "http://ambient:9", "HTTPS_PROXY": "http://ambient:9"}), \
                        patch("requests.adapters.HTTPAdapter.send", send):
                    context.get_page_data("fixture-owner/")
                    context.doc_id_graphql_query("1", {})
                    context.doc_id_graphql_query("2", {}, web_api=True)
                    context.get_iphone_json("api/v1/users/1/info/", {})
                self.assertEqual(len(sent), 4)
                expected = {"http": proxy, "https": proxy} if proxy else {}
                for url, proxies in sent:
                    self.assertEqual(proxies, expected, url)


if __name__ == "__main__":
    unittest.main()
