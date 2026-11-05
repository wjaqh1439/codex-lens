"""웹 보기 주소 풀기(weblink.resolve) — tailscale·포트 접속은 바꿔 끼운다."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import weblink  # noqa: E402

DNS = "box.tail0.ts.net"
SERVE = {"TCP": {"19072": {"HTTPS": True}, "443": {"HTTPS": True}, "19100": {"HTTPS": True}},
         "Web": {DNS + ":19072": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:19038"}}},
                 DNS + ":443": {"Handlers": {"/": {"Proxy": "http://localhost:3000/"}}},
                 DNS + ":19100": {"Handlers": {"/api": {"Proxy": "http://127.0.0.1:5000"}}}}}
ME = {"dns": DNS, "ips": ["100.64.0.7", "fd7a::7"]}
UP = lambda port: port != 9  # noqa: E731


def res(url, info=(SERVE, ME), up=UP):
    return weblink.resolve(url, info=info, up=up)


class ResolveTest(unittest.TestCase):
    def test_loopback_port_maps_to_https_keeping_path(self):
        r = res("http://127.0.0.1:19038/demo/x?a=1#top")
        self.assertEqual(r["url"], "https://box.tail0.ts.net:19072/demo/x?a=1#top")
        self.assertTrue(r["changed"])

    def test_localhost_and_tailscale_ip_and_name_count_as_this_machine(self):
        for u in ("http://localhost:19038/", "http://100.64.0.7:19038/", "http://box:19038/", "http://[::1]:19038/"):
            self.assertEqual(res(u)["url"], "https://box.tail0.ts.net:19072/", u)

    def test_unspecified_address_counts_as_this_machine(self):
        self.assertEqual(res("http://0.0.0.0:19038/x")["url"], "https://box.tail0.ts.net:19072/x")
        r = res("http://0.0.0.0:8000/", info=(None, None))   # tailscale 없는 Linux: 루프백으로 바꿔 연다
        self.assertEqual((r["url"], r["changed"], r["reason"]), ("http://127.0.0.1:8000/", True, "no_tailscale"))
        self.assertEqual(res("http://[::]:19038/")["url"], "https://box.tail0.ts.net:19072/")

    def test_no_tailscale_keeps_loopback_url(self):
        r = res("http://localhost:5173/", info=(None, None))
        self.assertEqual((r["url"], r["changed"], r["reason"], r["local"]), ("http://localhost:5173/", False, "no_tailscale", True))

    def test_port_up_sees_ipv6_only_server(self):
        import socket
        try:
            srv = socket.socket(socket.AF_INET6)
            srv.bind(("::1", 0))
        except OSError:
            self.skipTest("IPv6 루프백 없음")
        srv.listen(1)
        try:
            self.assertTrue(weblink.port_up(srv.getsockname()[1]))
        finally:
            srv.close()

    def test_port_manager_public_url_wins_even_without_tailscale(self):
        pub = {20005: "https://lens.example:21001/"}.get
        r = weblink.resolve("http://127.0.0.1:20005/app?x=1", info=(None, None), up=UP, public_for=pub)
        self.assertEqual((r["url"], r["changed"]), ("https://lens.example:21001/app?x=1", True))
        self.assertEqual(weblink.resolve("http://127.0.0.1:19038/", info=(SERVE, ME), up=UP, public_for=pub)["url"],
                         "https://box.tail0.ts.net:19072/")

    def test_port_443_mapping_drops_port(self):
        self.assertEqual(res("http://127.0.0.1:3000/")["url"], "https://box.tail0.ts.net/")

    def test_only_root_handler_counts(self):
        self.assertEqual(res("http://127.0.0.1:5000/")["reason"], "no_https")

    def test_down_port(self):
        r = res("http://127.0.0.1:9/demo/")
        self.assertEqual((r["reason"], r["changed"], r["url"]), ("down", False, "http://127.0.0.1:9/demo/"))

    def test_no_tailscale(self):
        self.assertEqual(res("http://127.0.0.1:19038/", info=(None, None))["reason"], "no_tailscale")

    def test_other_hosts_untouched(self):
        for u in ("https://example.com/a", "http://192.168.0.9:8000/", "https://box.tail0.ts.net:19072/"):
            r = res(u)
            self.assertEqual((r["url"], r["changed"], r["reason"]), (u, False, None), u)

    def test_bad_urls(self):
        for u in ("javascript:alert(1)", "", "ftp://127.0.0.1/", "http://127.0.0.1:99999/"):
            self.assertEqual(res(u)["reason"], "bad", u)


if __name__ == "__main__":
    unittest.main()
