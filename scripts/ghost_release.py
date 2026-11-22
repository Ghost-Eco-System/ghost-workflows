"""Prepares the files of a firmware release (GOS RFC-0008) after `pio run`, before signing.

For every manifest `ghost-firmware/1` in firmware/:
  - the tag vX.Y.Z[-rcN] matches `version`;
  - the build is not dirty and comes from the commit of the tag;
  - every part is unchanged (size, SHA-256);
  - AVR: the image ends below the bootloader (the board's maximum size);
  - ESP32: the NVS partition of the generated table is where the product declares it
    (`custom_fw_nvs_offset` in platformio.ini), so a reflash keeps the identity;
then `source` (and `nvs_offset` on ESP32) is written into the manifest.

Standard library only; no PlatformIO import. Usage (in the release workflow):

  python ghost_release.py --firmware firmware --repo "$GITHUB_REPOSITORY" --tag "$GITHUB_REF_NAME"
      --commit "$GITHUB_SHA" --ini platformio.ini --boards boards.json [--github-output "$GITHUB_OUTPUT"]

`boards.json` is `pio boards --json-output`.
"""
import argparse
import configparser
import hashlib
import json
import os
import re
import struct
import sys
from collections import namedtuple

SCHEMA = "ghost-firmware/1"
TAG_RE = re.compile(r"^v((0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*))(-rc([1-9]\d*))?$")

Prepared = namedtuple("Prepared", "manifests files prerelease")


class ReleaseError(Exception):
    pass


def parse_tag(tag):
    """(version, prerelease) of a tag vX.Y.Z or vX.Y.Z-rcN."""
    m = TAG_RE.match(tag)
    if not m:
        raise ReleaseError("tag %r is not vX.Y.Z or vX.Y.Z-rcN" % tag)
    return m.group(1), m.group(5) is not None


def env_option(ini, env, name):
    """Option of [env:<env>], following `extends`, then [env]; None when absent."""
    cp = configparser.ConfigParser(interpolation=None, strict=False)
    cp.read(ini, encoding="utf-8")
    seen = set()

    def look(section):
        if section in seen or not cp.has_section(section):
            return None
        seen.add(section)
        if cp.has_option(section, name):
            return cp.get(section, name).strip()
        for parent in (cp.get(section, "extends", fallback="") or "").split(","):
            parent = parent.strip()
            if parent:
                v = look(parent)
                if v is not None:
                    return v
        return None

    v = look("env:" + env)
    if v is None and cp.has_section("env") and cp.has_option("env", name):
        v = cp.get("env", name).strip()
    return v


def _read_part(fw_dir, part):
    path = os.path.join(fw_dir, part["file"])
    if os.path.basename(part["file"]) != part["file"] or not os.path.isfile(path):
        raise ReleaseError("%s: part missing" % part["file"])
    with open(path, "rb") as f:
        data = f.read()
    if len(data) != part["size"] or hashlib.sha256(data).hexdigest() != part["sha256"]:
        raise ReleaseError("%s: size or SHA-256 differs from the manifest" % part["file"])
    return path, data


def ihex_end(text):
    """One past the highest address written by an Intel HEX image."""
    base, end = 0, 0
    for line in text.decode("ascii").splitlines():
        line = line.strip()
        if not line.startswith(":"):
            continue
        rec = bytes.fromhex(line[1:])
        n, addr, kind = rec[0], rec[1] << 8 | rec[2], rec[3]
        if kind == 0:
            end = max(end, base + addr + n)
        elif kind == 2:
            base = (rec[4] << 8 | rec[5]) << 4
        elif kind == 4:
            base = (rec[4] << 8 | rec[5]) << 16
    return end


def nvs_offset(table):
    """Offset of the first NVS data partition (type 1, subtype 2) of an ESP32 partition table."""
    for i in range(0, len(table) - 31, 32):
        e = table[i : i + 32]
        if e[:2] != b"\xaa\x50":
            break
        if e[2] == 1 and e[3] == 2:
            return struct.unpack("<I", e[4:8])[0]
    raise ReleaseError("no NVS partition in the partition table")


def _check_avr(m, parts, boards):
    board = m.get("board")
    rom = boards.get(board)
    if rom is None:
        raise ReleaseError("board %r unknown to PlatformIO: cannot check the bootloader" % board)
    (_, data), = parts
    end = ihex_end(data)
    if end > rom:
        raise ReleaseError("%s: the image ends at %d, past %d bytes: it would overwrite the bootloader" % (m["env"], end, rom))


def _check_esp(m, parts, ini):
    declared = env_option(ini, m["env"], "custom_fw_nvs_offset")
    if not declared:
        raise ReleaseError("env %s: declare custom_fw_nvs_offset in platformio.ini" % m["env"])
    table = [d for (p, d), part in zip(parts, m["flash"]["parts"]) if part["file"].endswith(".partitions.bin")]
    if len(table) != 1:
        raise ReleaseError("env %s: one .partitions.bin part expected" % m["env"])
    found = nvs_offset(table[0])
    if found != int(declared, 16):
        raise ReleaseError("env %s: NVS at 0x%x, the product declares %s: a reflash would lose the identity" % (m["env"], found, declared))
    return "0x%x" % found


def prepare(fw_dir, repo, tag, commit, ini, boards):
    version, prerelease = parse_tag(tag)
    if not re.match(r"^[0-9a-f]{40}$", commit):
        raise ReleaseError("commit %r is not a full SHA" % commit)
    with open(boards, encoding="utf-8") as f:
        roms = {b["id"]: b.get("rom") for b in json.load(f)}
    manifests, files = [], []
    for name in sorted(os.listdir(fw_dir)):
        if not name.endswith(".json") or name.endswith(".sigstore.json"):
            continue
        path = os.path.join(fw_dir, name)
        with open(path, encoding="utf-8") as f:
            try:
                m = json.load(f)
            except ValueError:
                continue
        if not isinstance(m, dict) or m.get("schema") != SCHEMA:
            continue
        if m.get("version") != version:
            raise ReleaseError("%s: version %s, tag %s: the tag and the version differ" % (name, m.get("version"), tag))
        c = m.get("commit") or ""
        if c.endswith("-dirty"):
            raise ReleaseError("%s: built from a dirty tree" % name)
        if not c or not commit.startswith(c):
            raise ReleaseError("%s: built from commit %s, not the commit of the tag %s" % (name, c or "?", commit[:12]))
        parts = [_read_part(fw_dir, p) for p in m["flash"]["parts"]]
        tool = m["flash"].get("tool")
        if tool == "avrdude":
            _check_avr(m, parts, roms)
        elif tool == "esptool":
            m["nvs_offset"] = _check_esp(m, parts, ini)
        else:
            raise ReleaseError("%s: unknown flash tool %r" % (name, tool))
        m["source"] = {"repo": repo, "tag": tag, "commit": commit}
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            json.dump(m, f, indent=2)
            f.write("\n")
        manifests.append(path)
        files.append(path)
        files.extend(p for p, _ in parts)
    if not manifests:
        raise ReleaseError("no manifest %s in %s" % (SCHEMA, fw_dir))
    return Prepared(manifests, files, prerelease)


def main(argv=None):
    a = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    a.add_argument("--firmware", default="firmware")
    a.add_argument("--repo", required=True)
    a.add_argument("--tag", required=True)
    a.add_argument("--commit", required=True)
    a.add_argument("--ini", default="platformio.ini")
    a.add_argument("--boards", required=True)
    a.add_argument("--github-output")
    o = a.parse_args(argv)
    try:
        r = prepare(o.firmware, o.repo, o.tag, o.commit, o.ini, o.boards)
    except ReleaseError as e:
        print("::error::%s" % e)
        return 1
    for p in r.manifests:
        print("manifest %s" % os.path.basename(p))
    if o.github_output:
        with open(o.github_output, "a", encoding="utf-8") as f:
            f.write("prerelease=%s\n" % ("true" if r.prerelease else "false"))
            f.write("manifests=%s\n" % " ".join(r.manifests))
            f.write("files=%s\n" % " ".join(r.files))
    return 0


if __name__ == "__main__":
    sys.exit(main())
