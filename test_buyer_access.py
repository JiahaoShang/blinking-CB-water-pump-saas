import http.client
import tempfile
import threading
import unittest
from pathlib import Path

from app import make_server


class BuyerConversationAccessTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.server = make_server(Path(self.temp_dir.name) / "app.db", port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.server.RequestHandlerClass.db.conn.close()
        self.thread.join()
        self.temp_dir.cleanup()

    def request(self, method, path, body=None, cookie=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        if cookie:
            headers["Cookie"] = cookie
        conn.request(method, path, body=body, headers=headers)
        response = conn.getresponse()
        result = (response.status, dict(response.getheaders()), response.read().decode())
        conn.close()
        return result

    def test_private_conversation_requires_issued_cookie_for_read_and_write(self):
        status, headers, _ = self.request("GET", "/inquiry")
        self.assertEqual(status, 303)
        path = headers["Location"]
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        self.assertIn("HttpOnly", headers["Set-Cookie"])

        self.assertEqual(self.request("GET", path)[0], 404)
        self.assertEqual(self.request("POST", path.split("?")[0] + "/message", "message=secret")[0], 404)
        self.assertEqual(self.request("GET", path, cookie=cookie)[0], 200)
        self.assertEqual(self.request("POST", path.split("?")[0] + "/message", "message=valid", cookie=cookie)[0], 303)
        self.assertIn("valid", self.request("GET", path, cookie=cookie)[2])

        other_status, other_headers, _ = self.request("GET", "/inquiry")
        self.assertEqual(other_status, 303)
        other_cookie = other_headers["Set-Cookie"].split(";", 1)[0]
        self.assertEqual(self.request("GET", path, cookie=other_cookie)[0], 404)


if __name__ == "__main__":
    unittest.main()
