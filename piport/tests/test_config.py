"""Unit tests for config.py's pure logic (no hardware, no network)."""
import os
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import config  # noqa: E402


class TestNetmask(unittest.TestCase):
    def test_common_masks(self):
        self.assertEqual(config.netmask_to_prefix("255.255.255.0"), 24)
        self.assertEqual(config.netmask_to_prefix("255.255.0.0"), 16)
        self.assertEqual(config.netmask_to_prefix("255.0.0.0"), 8)
        self.assertEqual(config.netmask_to_prefix("255.255.255.255"), 32)

    def test_invalid(self):
        self.assertIsNone(config.netmask_to_prefix(""))
        self.assertIsNone(config.netmask_to_prefix("not-a-mask"))
        self.assertIsNone(config.netmask_to_prefix("255.255.255"))


class TestMisc(unittest.TestCase):
    def test_get_bool(self):
        for v in ("1", "true", "TRUE", "yes", "on", " On "):
            self.assertTrue(config.get_bool({"x": v}, "x"), v)
        for v in ("0", "false", "no", "off", ""):
            self.assertFalse(config.get_bool({"x": v}, "x"), v)
        self.assertFalse(config.get_bool({}, "missing"))

    def test_sysid_int(self):
        self.assertEqual(config.sysid_int({"sysid": "0xa5f1"}), 0xA5F1)
        self.assertEqual(config.sysid_int({"sysid": "42481"}), 42481)


class TestResolveIface(unittest.TestCase):
    def test_explicit_wins(self):
        self.assertEqual(config.resolve_iface("ap-main", {"iface": "eth0"}), "ap-main")

    def test_cfg_wins(self):
        self.assertEqual(config.resolve_iface(None, {"iface": "enP4p65s0"}), "enP4p65s0")

    def test_blank_auto_detects(self):
        # auto-detect must return a string, never None (falls back to "eth0")
        self.assertIsInstance(config.resolve_iface(None, {"iface": ""}), str)
        self.assertIsInstance(config.resolve_iface(), str)

    def test_default_iface_is_str(self):
        self.assertIsInstance(config._default_iface(), str)

    def test_parent_precedence(self):
        self.assertEqual(config.resolve_parent_iface(
            {"parent_iface": "enP4p65s0", "iface": "eth0"}), "enP4p65s0")
        self.assertEqual(config.resolve_parent_iface(
            {"parent_iface": "", "iface": "enP4p65s0"}), "enP4p65s0")
        self.assertIsInstance(config.resolve_parent_iface({"parent_iface": "", "iface": ""}), str)


class TestStreamDir(unittest.TestCase):
    def test_default_is_ram_backed(self):
        d = config.stream_dir()
        # falls back to "default" key; must be an absolute path, never the state dir
        self.assertTrue(os.path.isabs(d))
        self.assertEqual(config.stream_dir(), d)

    def test_per_state_dir_is_unique_and_stable(self):
        a = config.stream_dir("/srv/x/instances/main")
        b = config.stream_dir("/srv/x/instances/two")
        self.assertNotEqual(a, b)
        self.assertEqual(config.stream_dir("/srv/x/instances/main"), a)
        # relative vs absolute of the same state dir must agree
        os.chdir("/tmp")
        self.assertEqual(config.stream_dir("instances/main"),
                         config.stream_dir("/tmp/instances/main"))

    def test_tmpfs_base_writable(self):
        self.assertTrue(os.path.isdir(config._tmpfs_base()))


class TestLoadConfig(unittest.TestCase):
    def test_defaults_and_override(self):
        with tempfile.NamedTemporaryFile("w", suffix=".cfg", delete=False) as f:
            f.write("[identity]\nhostname = lab2\n[network]\niface = enP4p65s0\n")
            path = f.name
        try:
            cfg = config.load_config(path)
            self.assertEqual(cfg["hostname"], "lab2")
            self.assertEqual(cfg["iface"], "enP4p65s0")
            self.assertEqual(cfg["platform"], config.DEFAULTS["platform"])  # untouched key
        finally:
            os.unlink(path)

    def test_missing_file_falls_back(self):
        cfg = config.load_config("/nonexistent/path/aiport.cfg")
        self.assertEqual(cfg["platform"], "UVC AI Port")
        self.assertEqual(cfg["sysid"], "0xa5f1")


if __name__ == "__main__":
    unittest.main()
