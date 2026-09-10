from __future__ import annotations

import socket
import unittest

from tests.network_guard import BossNetworkBlocked, install_boss_network_guard, is_boss_host


class BossNetworkGuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        install_boss_network_guard()

    def test_matches_only_boss_domain_and_subdomains(self) -> None:
        self.assertTrue(is_boss_host("zhipin.com"))
        self.assertTrue(is_boss_host("www.zhipin.com"))
        self.assertTrue(is_boss_host(b"api.zhipin.com"))
        self.assertFalse(is_boss_host("notzhipin.com"))
        self.assertFalse(is_boss_host("localhost"))

    def test_dns_and_direct_connection_fail_before_network(self) -> None:
        with self.assertRaisesRegex(BossNetworkBlocked, "blocked BOSS DNS"):
            socket.getaddrinfo("www.zhipin.com", 443)
        with self.assertRaisesRegex(BossNetworkBlocked, "blocked BOSS connection"):
            socket.create_connection(("www.zhipin.com", 443), timeout=0.01)


if __name__ == "__main__":
    unittest.main()
