"""route-proxy.py, run as its own process: who gets in, where it may go."""
import base64
import json
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent
AUTH = "Basic " + base64.b64encode(b"smt:secret").decode()


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class RouteProxyTest(unittest.TestCase):
    def start(self, allow="127.0.0.1"):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        shutil.copy(HERE / "route-proxy.py", tmp / "route-proxy.py")
        port = free_port()
        (tmp / "route-proxy.json").write_text(json.dumps({"listen": "127.0.0.1", "port": port, "user": "smt",
            "password": "secret", "allow": allow, "source": "0.0.0.0"}))
        proc = subprocess.Popen([sys.executable, str(tmp / "route-proxy.py")])
        self.addCleanup(proc.kill)
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", port), 0.2).close()
                break
            except OSError:
                time.sleep(0.1)
        return port

    def ask(self, port, line, auth=AUTH):
        s = socket.create_connection(("127.0.0.1", port), 10)
        try:
            s.sendall(f"{line}\r\n{'Proxy-Authorization: ' + auth + chr(13) + chr(10) if auth else ''}\r\n".encode())
            return s.recv(200).decode("latin-1").split("\r\n")[0]
        except OSError:  # Windows reports a cut as an aborted connection
            return ""
        finally:
            s.close()

    def test_without_the_password_nothing_opens(self):
        port = self.start()
        self.assertIn("407", self.ask(port, "CONNECT www.instagram.com:443 HTTP/1.1", auth=None))
        self.assertIn("407", self.ask(port, "CONNECT www.instagram.com:443 HTTP/1.1",
                                      auth="Basic " + base64.b64encode(b"smt:guess").decode()))

    def test_any_other_address_is_cut_off_before_a_word(self):
        port = self.start(allow="203.0.113.9")
        self.assertEqual(self.ask(port, "CONNECT www.instagram.com:443 HTTP/1.1"), "")

    def test_only_https_and_no_private_address(self):
        port = self.start()
        self.assertIn("403", self.ask(port, "CONNECT www.instagram.com:80 HTTP/1.1"))
        self.assertIn("403", self.ask(port, "CONNECT 192.168.178.1:443 HTTP/1.1"))
        self.assertIn("405", self.ask(port, "GET http://example.com/ HTTP/1.1"))

    def test_urllib_reaches_an_https_page_through_it(self):
        port = self.start()
        # The tunnel is what is tested; this PC's own TLS inspection (WARP)
        # may not be trusted by this Python, so the certificate is not.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler(
            {"https": f"http://smt:secret@127.0.0.1:{port}"}),
            urllib.request.HTTPSHandler(context=ssl._create_unverified_context()))
        try:
            with opener.open("https://www.example.com/", timeout=20) as r:
                self.assertEqual(r.status, 200)
        except OSError as exc:
            self.skipTest(f"no internet here: {exc}")


if __name__ == "__main__":
    unittest.main()
