#!/usr/bin/env python3
"""
Reverse a PLDM PDR repository C array back into YAML PDR definitions (best effort).

Inputs:
  --schema-dir : Directory of PLDM JSON schemas (type_*.json)
  --in-c       : Path to a C/H file containing pdr_repository[] (hex/dec initialiser)
                 OR
  --in-bin     : Raw binary blob of concatenated PDRs
  --out-dir    : Directory to write reconstructed YAML files (one per PDR handle)

Notes and limitations:
  - Requires schema files to understand field order and widths.
  - Variable-length arrays are decoded using simple heuristics: looks for preceding
    '*Count', '*Size', '*Length', or singular forms to derive counts/byte lengths.
  - Strings: schema type 'string' decodes as UTF-8; strUTF-16BE decodes with utf-16-be;
    otherwise falls back to byte arrays.
  - If an array length cannot be inferred, remaining bytes for that field are emitted
    as a raw byte array.
"""

from __future__ import annotations

import argparse
import json
import re
import struct
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

import yaml  # type: ignore


HEADER_SIZE = 10  # recordHandle(4) + PDRHeaderVersion(1) + PDRType(1) + recordChangeNumber(2) + dataLength(2)

FMT_MAP = {
    "B": ("uint8", "<B"),
    "H": ("uint16", "<H"),
    "I": ("uint32", "<I"),
    "b": ("int8", "<b"),
    "h": ("int16", "<h"),
    "i": ("int32", "<i"),
    "Q": ("uint64", "<Q"),
    "q": ("int64", "<q"),
}


def die(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def read_bin_from_c(path: Path) -> bytes:
    text = path.read_text(encoding="utf-8", errors="ignore")
    # Try to capture the initializer of pdr_repository
    m = re.search(r"pdr_repository\s*\[.*?\]\s*=\s*{(.*?)};", text, re.S)
    if not m:
        die(f"could not find pdr_repository initializer in {path}")
    body = m.group(1)
    body = re.sub(r"/\*.*?\*/", "", body, flags=re.S)  # strip block comments
    nums = re.findall(r"0x[0-9a-fA-F]+|\d+", body)
    b = bytearray()
    for n in nums:
        b.append(int(n, 16 if n.startswith("0x") else 10) & 0xFF)
    return bytes(b)


def read_repo_bytes(args: argparse.Namespace) -> bytes:
    if args.in_bin:
        return Path(args.in_bin).read_bytes()
    if args.in_c:
        return read_bin_from_c(Path(args.in_c))
    die("either --in-bin or --in-c must be provided")


def split_records(repo: bytes) -> List[Tuple[int, int, bytes]]:
    records = []
    offset = 0
    while offset + HEADER_SIZE <= len(repo):
        header = repo[offset : offset + HEADER_SIZE]
        handle, ver, pdr_type, rc, data_len = struct.unpack("<IBBHH", header)
        total = HEADER_SIZE + data_len
        if offset + total > len(repo):
            die(f"record at offset {offset} overruns repository")
        payload = repo[offset : offset + total]
        records.append((handle, pdr_type, payload))
        offset += total
    return records


def load_schema(schema_dir: Path, pdr_type: int) -> Dict[str, Any]:
    path = schema_dir / f"type_{pdr_type}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        die(f"schema file not found: {path}")
    except json.JSONDecodeError as exc:
        die(f"failed to parse schema {path}: {exc}")


def scalar_fmt_and_name(schema: Dict[str, Any] | None) -> Tuple[str, str]:
    if not schema:
        return "uint8", "<B"
    fmt = schema.get("binaryFormat")
    if fmt == "ver32":
        return "ver32", "<I"
    if fmt in FMT_MAP:
        return FMT_MAP[fmt]
    if schema.get("type") == "string":
        return "strUTF-8", "strUTF-8"
    if schema.get("type") == "integer":
        maximum = schema.get("maximum", 255)
        if maximum <= 0xFF:
            return "uint8", "<B"
        if maximum <= 0xFFFF:
            return "uint16", "<H"
        if maximum <= 0xFFFFFFFF:
            return "uint32", "<I"
        return "uint64", "<Q"
    return "uint8", "<B"


def decode_string(raw: bytes, schema: Dict[str, Any] | None) -> str:
    if schema and schema.get("binaryFormat") == "utf-16-be":
        return raw.decode("utf-16-be").rstrip("\x00")
    # Default UTF-8
    return raw.decode("utf-8", errors="ignore").rstrip("\x00")


def find_length(name: str, parsed: Dict[str, Any]) -> Tuple[int | None, bool]:
    """Return (length, is_byte_length)."""
    candidates = [
        f"{name}Size",
        f"{name}size",
        f"{name}Length",
        f"{name}LengthBytes",
        f"{name}Count",
    ]
    if name.endswith("s"):
        singular = name[:-1]
        candidates.append(f"{singular}Count")
    for c in candidates:
        if c in parsed:
            val = parsed[c]["value"]
            if "Count" in c or c.endswith("count"):
                return int(val), False
            if "Length" in c or "Size" in c:
                return int(val), True
    return None, False


def decode_field(name: str, schema: Dict[str, Any] | None, buf: memoryview, pos: int, parsed: Dict[str, Any]) -> Tuple[Any, int]:
    if schema and schema.get("type") == "array":
        items_schema = schema.get("items", {})
        length, is_bytes = find_length(name, parsed)
        if length is None:
            # Fallback: consume all remaining bytes as uint8 array
            remaining = len(buf) - pos
            arr = list(buf[pos : pos + remaining].tolist())
            return {"type": "uint8[]", "value": arr}, len(buf)
        if is_bytes:
            raw = bytes(buf[pos : pos + length])
            # If array of integers
            if items_schema.get("type") == "integer":
                return {"type": "uint8[]", "value": list(raw)}, pos + length
            # If array of bytes representing string
            if items_schema.get("type") == "string":
                return {"type": "strUTF-8", "value": raw.decode("utf-8", errors="ignore").rstrip('\x00')}, pos + length
            # Otherwise, raw bytes
            return {"type": "uint8[]", "value": list(raw)}, pos + length
        # count-based array of fixed-size scalars or objects
        arr_vals = []
        cur = pos
        for _ in range(length):
            val, cur = decode_field(f"{name}[]", items_schema, buf, cur, {})
            arr_vals.append(val["value"] if isinstance(val, dict) and "value" in val else val)
        return {"type": f"{scalar_fmt_and_name(items_schema)[0]}[]", "value": arr_vals}, cur

    if schema and schema.get("type") == "object":
        props = schema.get("properties", {})
        order = schema.get("binaryOrder", list(props.keys()))
        cur = pos
        obj: Dict[str, Any] = {}
        for key in order:
            if key not in props:
                continue
            val, cur = decode_field(key, props.get(key, {}), buf, cur, obj)
            obj[key] = val
        return obj, cur

    # scalar
    tname, fmt = scalar_fmt_and_name(schema)
    if fmt in ("strUTF-8", "strUTF-16BE"):
        # For strings, try to decode until null terminator.
        mv = buf[pos:]
        terminator = b"\x00\x00" if fmt == "strUTF-16BE" else b"\x00"
        idx = mv.tobytes().find(terminator)
        if idx == -1:
            die(f"unterminated string at offset {pos}")
        raw = bytes(mv[: idx + len(terminator)])
        val = decode_string(raw, schema)
        return {"type": tname, "value": val}, pos + idx + len(terminator)
    size = struct.calcsize(fmt)
    raw = buf[pos : pos + size]
    val = struct.unpack(fmt, raw)[0]
    return {"type": tname, "value": val}, pos + size


def decode_body(body: bytes, schema: Dict[str, Any]) -> Dict[str, Any]:
    mv = memoryview(body)
    props = {k: v for k, v in schema.get("properties", {}).items() if k != "pdrHeader"}
    order = schema.get("binaryOrder", list(props.keys()))
    cur = 0
    parsed: Dict[str, Any] = {}
    for key in order:
        if key not in props:
            continue
        val, cur = decode_field(key, props[key], mv, cur, parsed)
        parsed[key] = val
    # If there are remaining props not in binaryOrder, skip them
    return parsed


def write_yaml(out_dir: Path, handle: int, body: Dict[str, Any], header_fields: Dict[str, Any]) -> None:
    data = {"pdrHeader": header_fields}
    data.update(body)
    out_path = out_dir / f"pdr_{handle}.yaml"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(description="Reconstruct YAML PDRs from a pdr_repository C array or binary blob.")
    ap.add_argument("--schema-dir", required=True, type=Path, help="Directory with type_*.json schemas")
    ap.add_argument("--in-c", type=Path, help="C/H file containing pdr_repository[]")
    ap.add_argument("--in-bin", type=Path, help="Raw binary repository blob")
    ap.add_argument("--out-dir", required=True, type=Path, help="Directory to write reconstructed YAMLs")
    args = ap.parse_args()

    repo = read_repo_bytes(args)
    records = split_records(repo)

    for handle, pdr_type, payload in records:
        header = payload[:HEADER_SIZE]
        body = payload[HEADER_SIZE:]
        recordHandle, ver, ptype, rc, data_len = struct.unpack("<IBBHH", header)
        header_yaml = {
            "recordHandle": {"type": "uint32", "value": recordHandle},
            "PDRHeaderVersion": {"type": "uint8", "value": ver},
            "PDRType": {"type": "uint8", "value": ptype},
            "recordChangeNumber": {"type": "uint16", "value": rc},
            "dataLength": {"type": "uint16", "value": data_len},
        }

        schema = load_schema(args.schema_dir, pdr_type)
        body_yaml = decode_body(body, schema)
        write_yaml(args.out_dir, handle, body_yaml, header_yaml)
        print(f"wrote {args.out_dir}/pdr_{handle}.yaml (Type {pdr_type})")


if __name__ == "__main__":
    main()
