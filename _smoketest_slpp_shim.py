"""Smoke-test-only Lua extractor. NOT for production use.

In production the scripts depend on the `slpp` package from PyPI (pip install slpp).
This file exists only so the test environment, which lacks PyPI access, can
exercise ingest.py end-to-end. It implements just enough of slpp's decode() API
to handle MasterMerchant.lua's EXPORT block and GBLData.lua's history block.
"""
import re
import sys
import types

USER = "@jeffk42"
GUILD_NAME = "AK Tamriel Trade"


def _extract_block(text, key_path):
    pos = 0
    for k in key_path:
        pattern = (r'(?:\["' + re.escape(k) + r'"\]|(?<![A-Za-z0-9_])'
                   + re.escape(k) + r'(?![A-Za-z0-9_]))\s*=\s*\{')
        m = re.search(pattern, text[pos:])
        if not m:
            return {}
        pos += m.end()

    depth = 1
    i = pos
    while i < len(text) and depth > 0:
        c = text[i]
        if c == '"':
            i += 1
            while i < len(text) and text[i] != '"':
                if text[i] == '\\':
                    i += 2
                else:
                    i += 1
        elif c == '{':
            depth += 1
        elif c == '}':
            depth -= 1
        i += 1
    block = text[pos:i-1]

    out = {}
    for m in re.finditer(r'\[(\d+)\]\s*=\s*"((?:[^"\\]|\\.)*)"', block):
        out[int(m.group(1))] = m.group(2)
    return out


def decode(s):
    if s.startswith("{"):
        s = s[1:]
    if s.endswith("}"):
        s = s[:-1]
    if "ShopkeeperSavedVars" in s:
        export = _extract_block(s, ["ShopkeeperSavedVars", "Default", USER, "$AccountWide", "EXPORT", GUILD_NAME])
        return {"ShopkeeperSavedVars": {"Default": {USER: {"$AccountWide": {"EXPORT": {GUILD_NAME: export}}}}}}
    elif "GBLDataSavedVariables" in s:
        history = _extract_block(s, ["GBLDataSavedVariables", "Default", USER, "$AccountWide", "history", GUILD_NAME])
        return {"GBLDataSavedVariables": {"Default": {USER: {"$AccountWide": {"history": {GUILD_NAME: history}}}}}}
    return {}


def install():
    mod = types.ModuleType("slpp")
    sub = types.ModuleType("slpp.slpp")
    sub.decode = decode
    mod.slpp = sub
    sys.modules["slpp"] = mod
    sys.modules["slpp.slpp"] = sub


if __name__ == "__main__":
    install()
