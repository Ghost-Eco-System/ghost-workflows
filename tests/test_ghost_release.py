"""Tests of scripts/ghost_release.py (standard library only: python -m unittest)."""
import hashlib
import json
import os
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import ghost_release as gr  # noqa: E402

FULL = "4075733" + "a" * 33
TAG = "v0.4.0-rc2"
REPO = "Ghost-Parking-Manager/ghost-parking-radar-firmware"


def ihex(data, base=0):
    """Intel HEX of @p data at @p base, 16 bytes per record."""
    lines = []
    for i in range(0, len(data), 16):
        chunk = data[i : i + 16]
        addr = base + i
        rec = bytes([len(chunk), addr >> 8 & 0xFF, addr & 0xFF, 0]) + chunk
        lines.append(":" + (rec + bytes([(-sum(rec)) & 0xFF])).hex().upper())
    lines.append(":00000001FF")
    return ("\n".join(lines) + "\n").encode()


def partition_table(nvs_offset=0x9000):
    def entry(ptype, sub, off, size, label):
        return b"\xaa\x50" + bytes([ptype, sub]) + struct.pack("<II", off, size) + label.ljust(16, b"\0") + b"\0" * 4

    t = entry(1, 2, nvs_offset, 0x5000, b"nvs") + entry(1, 0, 0xE000, 0x2000, b"otadata") + entry(0, 0x10, 0x10000, 0x300000, b"app0")
    return t + b"\xff" * (3072 - len(t))


def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


class Repo:
    """A firmware/ folder and a platformio.ini in a temporary directory."""

    def __init__(self, test):
        self.dir = tempfile.mkdtemp()
        self.fw = os.path.join(self.dir, "firmware")
        os.makedirs(self.fw)
        self.ini = os.path.join(self.dir, "platformio.ini")
        self.boards = os.path.join(self.dir, "boards.json")
        with open(self.boards, "w") as f:
            json.dump([{"id": "nanoatmega328new", "rom": 30720}, {"id": "esp32dev", "rom": 4194304}], f)
        self.write_ini("[env]\ncustom_fw_product = x\n")

    def write_ini(self, text):
        with open(self.ini, "w") as f:
            f.write(text)

    def part(self, name, data):
        with open(os.path.join(self.fw, name), "wb") as f:
            f.write(data)
        return {"file": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}

    def avr(self, version="0.4.0", commit="4075733", image=b"\x0c\x94" * 100):
        p = self.part("radar-%s-nano.hex" % version, ihex(image))
        p["format"] = "ihex"
        return self.manifest("radar-%s-nano.json" % version, version, commit, "nano", "nanoatmega328new",
                             {"tool": "avrdude", "mcu": "atmega328p", "programmer": "arduino", "baud": 115200, "parts": [p]})

    def esp(self, env="esp32", nvs=0x9000):
        parts = [
            dict(offset="0x1000", **self.part("gw-0.4.0-%s.bootloader.bin" % env, b"b" * 64)),
            dict(offset="0x8000", **self.part("gw-0.4.0-%s.partitions.bin" % env, partition_table(nvs))),
            dict(offset="0x10000", **self.part("gw-0.4.0-%s.bin" % env, b"a" * 256)),
        ]
        return self.manifest("gw-0.4.0-%s.json" % env, "0.4.0", "4075733", env, "esp32dev",
                             {"tool": "esptool", "chip": "esp32", "baud": 460800, "parts": parts})

    def manifest(self, name, version, commit, env, board, flash):
        m = {"schema": "ghost-firmware/1", "product": "x", "version": version, "device_type": "01",
             "env": env, "board": board, "commit": commit, "built_at": "2026-11-22T10:00:00Z", "flash": flash}
        path = os.path.join(self.fw, name)
        with open(path, "w") as f:
            json.dump(m, f, indent=2)
        return path

    def run(self, tag=TAG, commit=FULL):
        return gr.prepare(self.fw, repo=REPO, tag=tag, commit=commit, ini=self.ini, boards=self.boards)


class TagTest(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(gr.parse_tag("v1.2.3"), ("1.2.3", False))
        self.assertEqual(gr.parse_tag("v1.2.3-rc4"), ("1.2.3", True))
        for bad in ("1.2.3", "v1.2", "v1.2.3-beta", "v01.2.3", "v1.2.3-rc0"):
            with self.assertRaises(gr.ReleaseError, msg=bad):
                gr.parse_tag(bad)


class AvrTest(unittest.TestCase):
    def test_fills_source_and_lists_files(self):
        r = Repo(self)
        path = r.avr()
        out = r.run()
        m = load(path)
        self.assertEqual(m["source"], {"repo": REPO, "tag": TAG, "commit": FULL})
        self.assertNotIn("nvs_offset", m)
        self.assertTrue(out.prerelease)
        self.assertEqual(sorted(os.path.basename(f) for f in out.files), ["radar-0.4.0-nano.hex", "radar-0.4.0-nano.json"])
        self.assertEqual([os.path.basename(f) for f in out.manifests], ["radar-0.4.0-nano.json"])

    def test_stable_tag(self):
        r = Repo(self)
        r.avr()
        self.assertFalse(r.run(tag="v0.4.0").prerelease)

    def test_tag_must_equal_version(self):
        r = Repo(self)
        r.avr(version="0.4.1")
        with self.assertRaisesRegex(gr.ReleaseError, "0.4.1.*v0.4.0-rc2|version"):
            r.run()

    def test_dirty_or_other_commit_refused(self):
        r = Repo(self)
        r.avr(commit="4075733-dirty")
        with self.assertRaisesRegex(gr.ReleaseError, "dirty"):
            r.run()
        r2 = Repo(self)
        r2.avr(commit="abcdef0")
        with self.assertRaisesRegex(gr.ReleaseError, "commit"):
            r2.run()

    def test_image_over_the_bootloader_refused(self):
        r = Repo(self)
        r.avr(image=b"\x00" * 30721)
        with self.assertRaisesRegex(gr.ReleaseError, "bootloader"):
            r.run()

    def test_changed_part_refused(self):
        r = Repo(self)
        r.avr()
        with open(os.path.join(r.fw, "radar-0.4.0-nano.hex"), "ab") as f:
            f.write(b"x")
        with self.assertRaisesRegex(gr.ReleaseError, "SHA-256"):
            r.run()

    def test_no_manifest(self):
        r = Repo(self)
        with self.assertRaisesRegex(gr.ReleaseError, "no manifest"):
            r.run()


class EspTest(unittest.TestCase):
    INI = "[env]\nx = 1\n[env:esp32]\ncustom_fw_nvs_offset = 0x9000\n[env:esp32_enc28j60]\nextends = env:esp32\n"

    def test_nvs_offset_checked_and_written(self):
        r = Repo(self)
        r.write_ini(self.INI)
        a = r.esp("esp32")
        b = r.esp("esp32_enc28j60")
        out = r.run()
        for p in (a, b):
            self.assertEqual(load(p)["nvs_offset"], "0x9000")
        self.assertEqual(len(out.manifests), 2)
        self.assertEqual(len(out.files), 8)

    def test_moved_nvs_refused(self):
        r = Repo(self)
        r.write_ini(self.INI)
        r.esp("esp32", nvs=0xA000)
        with self.assertRaisesRegex(gr.ReleaseError, "NVS"):
            r.run()

    def test_undeclared_nvs_refused(self):
        r = Repo(self)
        r.esp("esp32")
        with self.assertRaisesRegex(gr.ReleaseError, "custom_fw_nvs_offset"):
            r.run()


class IniTest(unittest.TestCase):
    def test_option_follows_extends_then_env(self):
        r = Repo(self)
        r.write_ini("[env]\na = base\n[env:x]\nb = 1\n[env:y]\nextends = env:x\nb = 2\n[env:z]\nextends = env:y\n")
        self.assertEqual(gr.env_option(r.ini, "z", "b"), "2")
        self.assertEqual(gr.env_option(r.ini, "z", "a"), "base")
        self.assertIsNone(gr.env_option(r.ini, "z", "c"))


if __name__ == "__main__":
    unittest.main()
