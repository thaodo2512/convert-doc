#!/usr/bin/env python3
"""
Generate a PLDM PDR repository header from a directory of PDR YAML files.

Input:
  --pdr-dir:    Directory containing PDR YAML files (e.g., source/data)
  --schema-dir: Directory containing PLDM JSON schemas (e.g., source/schema)
  --macro-defs: YAML defining handle/offset/field macros
  --out:        Output header path

Behavior:
  - Reads every *.yaml in --pdr-dir
  - Packs each PDR using the YAML's pdrHeader plus body fields
  - Computes dataLength from the packed body (header is always 10 bytes)
  - Builds a contiguous pdr_repository[] blob and pdr_offsets[] table
  - Emits macros from macro_defs.yaml (handles, repo offsets, field offsets)

Assumptions:
  - Little-endian packing (DSP0248 baseline)
  - PDR header layout per DSP0248 v1.3.0 Clause 28.1
  - Field offset macros are relative to the start of each PDR blob
"""

from __future__ import annotations

import argparse
import glob
import json
import struct
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Tuple

try:
    import yaml  # type: ignore
except ImportError as exc:  # pragma: no cover - dependency error path
    raise SystemExit("PyYAML is required (pip install pyyaml)") from exc


HEADER_SIZE = 10  # recordHandle(4) + PDRHeaderVersion(1) + PDRType(1) + recordChangeNumber(2) + dataLength(2)

TYPE_FMT = {
    "uint8": "<B",
    "int8": "<b",
    "sint8": "<b",
    "enum8": "<B",
    "bitfield8": "<B",
    "bool": "<?",
    "uint16": "<H",
    "int16": "<h",
    "sint16": "<h",
    "uint32": "<I",
    "int32": "<i",
    "sint32": "<i",
    "float": "<f",
    "uint64": "<Q",
    "int64": "<q",
    "sint64": "<q",
    "ver32": "<I",
    # BinaryFormat shorthands (single-char)
    "B": "<B",
    "H": "<H",
    "I": "<I",
    "b": "<b",
    "h": "<h",
    "i": "<i",
    "f": "<f",
    "Q": "<Q",
    "q": "<q",
}


TYPE_NAMES = {
    1: "terminus_locator",
    2: "numeric_sensor",
    3: "numeric_sensor_init",
    4: "state_sensor",
    5: "numeric_sensor_threshold",
    22: "redfish_resource",
}


def die(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def load_yaml(path: Path) -> Dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}
    except FileNotFoundError:
        die(f"YAML file not found: {path}")
    except yaml.YAMLError as exc:
        die(f"failed to parse {path}: {exc}")


def load_json(path: Path) -> Dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        die(f"schema file not found: {path}")
    except json.JSONDecodeError as exc:
        die(f"failed to parse schema {path}: {exc}")


def pack_scalar(value: Any, type_name: str) -> bytes:
    # Arrays encoded as e.g. uint8[]
    if type_name.endswith("[]"):
        base = type_name[:-2]
        if base not in TYPE_FMT:
            die(f"unsupported array base type '{base}'")
        fmt = TYPE_FMT[base]
        return b"".join(struct.pack(fmt, v) for v in value)
    if type_name in ("strUTF-8", "strASCII"):
        return str(value).encode("utf-8") + b"\x00"
    if type_name == "strUTF-16BE":
        return str(value).encode("utf-16-be") + b"\x00\x00"
    fmt = TYPE_FMT.get(type_name)
    if not fmt:
        die(f"unsupported type '{type_name}'")
    try:
        return struct.pack(fmt, value)
    except struct.error as exc:
        die(f"failed to pack value '{value}' as {type_name}: {exc}")


FMT_CHAR_TO_TYPE = {
    "B": "uint8",
    "H": "uint16",
    "I": "uint32",
    "b": "int8",
    "h": "int16",
    "i": "int32",
    "f": "float",
    "Q": "uint64",
    "q": "int64",
}


def infer_type_name(node: Any, schema: Dict[str, Any] | None) -> str | None:
    # Prefer schema; YAML "type" hints are ignored to avoid drift.
    if schema:
        fmt = schema.get("binaryFormat")
        if fmt in FMT_CHAR_TO_TYPE:
            return FMT_CHAR_TO_TYPE[fmt]
        if fmt == "ver32":
            return "ver32"
        if schema.get("type") == "string":
            return "strUTF-8"
        if schema.get("type") == "integer":
            maximum = schema.get("maximum", 0)
            if maximum <= 255:
                return "uint8"
            if maximum <= 65535:
                return "uint16"
            if maximum <= 4294967295:
                return "uint32"
            return "uint64"
        if schema.get("type") == "number":
            return "float"
    if isinstance(node, dict) and "type" in node:
        return node["type"]
    return None


def resolve_dynamic_type(schema: Dict[str, Any] | None, parsed: Dict[str, Any]) -> str | None:
    if not schema:
        return None
    dep_val = None
    for resolver_key in ("typeResolver", "formatResolver"):
        resolver = schema.get(resolver_key)
        if not resolver:
            continue
        dep = resolver.get("dependsOn")
        if dep and dep in parsed:
            dep_val = str(parsed[dep])
            mapping = resolver.get("mapping", {})
            if dep_val in mapping:
                mapped = mapping[dep_val]
                if resolver_key == "formatResolver" and mapped in FMT_CHAR_TO_TYPE:
                    return FMT_CHAR_TO_TYPE[mapped]
                return mapped
    return None


def pack_leaf(node: Any, schema: Dict[str, Any] | None, path: str, buf: bytearray, offsets: Dict[str, int], base_offset: int, parsed: Dict[str, Any]) -> Any:
    if isinstance(node, dict) and "value" in node:
        value = node["value"]
        tname = infer_type_name(node, schema) or "uint8"
    else:
        value = node
        tname = infer_type_name(node, schema)
    override = resolve_dynamic_type(schema, parsed)
    if override:
        tname = override
    # If value is a list and schema is not explicit, treat as array of uint8 by default.
    if isinstance(value, list) and (tname is None or not str(tname).endswith("[]")):
        tname = (tname or "uint8") + "[]"
    if not tname:
        die(f"unable to infer type for field '{path}'")
    offsets[path] = base_offset + len(buf)
    buf.extend(pack_scalar(value, tname))
    return value


def pack_with_schema(node: Any, schema: Dict[str, Any] | None, path: str, buf: bytearray, offsets: Dict[str, int], base_offset: int, parsed: Dict[str, Any]) -> Any:
    schema_type = schema.get("type") if schema else None

    # Treat dicts with explicit 'value' as leaf nodes unless schema forces composite.
    if isinstance(node, dict) and "value" in node and schema_type not in ("array", "object"):
        return pack_leaf(node, schema, path, buf, offsets, base_offset, parsed)

    if schema_type == "array" or (schema_type is None and isinstance(node, list)):
        if isinstance(node, dict) and "value" in node:
            node = node["value"]
        if not isinstance(node, list):
            die(f"expected list at '{path}'")
        item_schema = schema.get("items", {})
        vals = []
        for idx, val in enumerate(node):
            vals.append(pack_with_schema(val, item_schema, f"{path}[{idx}]", buf, offsets, base_offset, {}))
        return vals

    if schema_type == "object" or (schema_type is None and isinstance(node, dict)):
        props = schema.get("properties", {}) if schema else {}
        order = schema.get("binaryOrder") if schema and "binaryOrder" in schema else (list(node.keys()) if isinstance(node, dict) else [])
        obj_parsed: Dict[str, Any] = {}
        for key in order:
            if not isinstance(node, dict) or key not in node:
                die(f"missing field '{key}' in object at '{path}'")
            sub_schema = props.get(key, {})
            sub_path = f"{path}.{key}" if path else key
            obj_parsed[key] = pack_with_schema(node[key], sub_schema, sub_path, buf, offsets, base_offset, obj_parsed)
        if isinstance(node, dict):
            for key, val in node.items():
                if key in order:
                    continue
                sub_schema = props.get(key, {})
                sub_path = f"{path}.{key}" if path else key
                obj_parsed[key] = pack_with_schema(val, sub_schema, sub_path, buf, offsets, base_offset, obj_parsed)
        parsed.update(obj_parsed)
        return obj_parsed

    return pack_leaf(node, schema, path, buf, offsets, base_offset, parsed)


def pack_body(data: Dict[str, Any], schema: Dict[str, Any]) -> Tuple[bytes, Dict[str, int]]:
    buf = bytearray()
    offsets: Dict[str, int] = {}
    pack_with_schema(data, schema, "", buf, offsets, base_offset=HEADER_SIZE, parsed={})
    return bytes(buf), offsets


def pack_header(header: Dict[str, Any], body_len: int) -> Tuple[bytes, Dict[str, int], int, int]:
    required = ["recordHandle", "PDRHeaderVersion", "PDRType", "recordChangeNumber", "dataLength"]
    for field in required:
        if field not in header:
            die(f"pdrHeader missing required field '{field}'")
    hbuf = bytearray()
    offsets: Dict[str, int] = {}

    def add(name: str, node: Dict[str, Any], fmt_name: str) -> None:
        offsets[f"pdrHeader.{name}"] = len(hbuf)
        val = node.get("value")
        if val is None:
            die(f"pdrHeader.{name} missing 'value'")
        hbuf.extend(pack_scalar(val, fmt_name))

    add("recordHandle", header["recordHandle"], "uint32")
    add("PDRHeaderVersion", header["PDRHeaderVersion"], "uint8")
    add("PDRType", header["PDRType"], "uint8")
    add("recordChangeNumber", header["recordChangeNumber"], "uint16")
    # dataLength overridden with computed body length
    offsets["pdrHeader.dataLength"] = len(hbuf)
    hbuf.extend(pack_scalar(body_len, "uint16"))

    pdr_type = header["PDRType"]["value"]
    handle = header["recordHandle"]["value"]
    return bytes(hbuf), offsets, pdr_type, handle


def type_name_from_code(code: int) -> str:
    return TYPE_NAMES.get(code, f"type{code}")


@dataclass
class PdrItem:
    handle: int
    type_code: int
    type_name: str
    payload: bytes
    offsets: Dict[str, int]
    raw: Dict[str, Any]


def load_pdrs_from_dir(pdr_dir: Path, schema_dir: Path) -> List[PdrItem]:
    items: List[PdrItem] = []
    for path_str in sorted(glob.glob(str(pdr_dir / "*.yaml"))):
        path = Path(path_str)
        data = load_yaml(path)
        # Drop YAML-specified type hints to rely solely on schema-defined formats.
        def strip_types(obj: Any) -> Any:
            if isinstance(obj, dict):
                obj = {k: strip_types(v) for k, v in obj.items() if k != "type"}
            elif isinstance(obj, list):
                obj = [strip_types(v) for v in obj]
            return obj

        data = strip_types(data)
        if "pdrHeader" not in data:
            die(f"{path} missing pdrHeader")
        header = data["pdrHeader"]
        body = {k: v for k, v in data.items() if k != "pdrHeader"}

        pdr_type = header["PDRType"]["value"]
        schema_path = schema_dir / f"type_{pdr_type}.json"
        schema = load_json(schema_path)

        body_bytes, body_offsets = pack_body(body, schema)
        header_bytes, header_offsets, type_code, handle = pack_header(header, len(body_bytes))

        payload = header_bytes + body_bytes
        offsets = {}
        offsets.update(header_offsets)
        offsets.update(body_offsets)

        items.append(
            PdrItem(
                handle=handle,
                type_code=type_code,
                type_name=type_name_from_code(type_code),
                payload=payload,
                offsets=offsets,
                raw=body,
            )
        )
    if not items:
        die(f"no YAML files found in {pdr_dir}")
    # Ensure unique handles; auto-renumber duplicates by bumping beyond max.
    used = set()
    max_handle = max(it.handle for it in items)
    for it in items:
        if it.handle in used:
            max_handle += 1
            print(f"info: handle {it.handle} duplicated, remapping to {max_handle}")
            it.handle = max_handle
        used.add(it.handle)
    items.sort(key=lambda x: x.handle)
    return items


def compute_repo_offsets(items: List[PdrItem]) -> Dict[int, int]:
    offsets: Dict[int, int] = {}
    cursor = 0
    for it in items:
        offsets[it.handle] = cursor
        cursor += len(it.payload)
    return offsets


def get_by_path(data: Dict[str, Any], path: str) -> Any:
    parts = []
    tmp = ""
    i = 0
    while i < len(path):
        ch = path[i]
        if ch == "[":
            j = path.index("]", i)
            idx = int(path[i + 1 : j])
            parts.append(idx)
            i = j
        elif ch == ".":
            if tmp:
                parts.append(tmp)
                tmp = ""
        else:
            tmp += ch
        i += 1
    if tmp:
        parts.append(tmp)

    cur: Any = data
    for p in parts:
        if isinstance(p, int):
            cur = cur[p]
        else:
            cur = cur[p]
    return cur


def matches(item: PdrItem, criteria: Dict[str, Any]) -> bool:
    for key, expected in criteria.items():
        if key == "type":
            if item.type_name != expected and item.type_code != expected:
                return False
            continue
        try:
            val = get_by_path(item.raw, key)
        except Exception:
            return False
        if val != expected:
            return False
    return True


def resolve_handle_from_match(items: List[PdrItem], match: Dict[str, Any]) -> int:
    candidates = [it for it in items if matches(it, match)]
    if not candidates:
        die(f"no PDR matches criteria: {match}")
    if len(candidates) > 1:
        die(f"criteria {match} matched multiple PDRs")
    return candidates[0].handle


def emit_macros(items: List[PdrItem], offset_map: Dict[int, int], macro_cfg: Dict[str, Any]) -> List[str]:
    lines: List[str] = []

    def resolve_handle(entry: Dict[str, Any]) -> int:
        if "handle" in entry:
            return entry["handle"]
        if "match_handle" in entry:
            return entry["match_handle"]
        if "match" in entry:
            return resolve_handle_from_match(items, entry["match"])
        die(f"macro entry missing handle or match: {entry}")

    macros = macro_cfg.get("macros", {})

    # Handle macros
    for h in macros.get("handles", []):
        name = h["name"]
        handle = resolve_handle(h)
        lines.append(f"#define {name} {handle}u")
    if macros.get("handles"):
        lines.append("")

    # Offset macros
    for off in macros.get("offsets", []):
        name = off["name"]
        handle = resolve_handle(off)
        if handle not in offset_map:
            die(f"offset macro {name} refers to unknown handle {handle}")
        lines.append(f"#define {name} {offset_map[handle]}u")
    if macros.get("offsets"):
        lines.append("")

    # Field offset macros (relative to PDR start)
    for fld in macros.get("fields", []):
        name = fld["name"]
        field_path = fld.get("field")
        if not field_path:
            die(f"field macro {name} missing 'field'")
        handle = resolve_handle(fld)
        item = next((i for i in items if i.handle == handle), None)
        if item is None:
            die(f"field macro {name} refers to unknown handle {handle}")
        if field_path not in item.offsets:
            available = ", ".join(sorted(item.offsets.keys()))
            die(f"field '{field_path}' not found in PDR handle {handle}. Available: {available}")
        lines.append(f"#define {name} {item.offsets[field_path]}u")
    if macros.get("fields"):
        lines.append("")

    return lines


def emit_repo_definitions(items: List[PdrItem], array_name: str, bytes_per_line: int = 12) -> List[str]:
    lines: List[str] = []
    for it in items:
        lines.append(f"/* Handle {it.handle} (Type {it.type_code}, {it.type_name}) */")
        blob = it.payload
        for idx in range(0, len(blob), bytes_per_line):
            chunk = blob[idx : idx + bytes_per_line]
            lines.append("  " + ", ".join(f"0x{b:02X}" for b in chunk) + ",")
    if lines and lines[-1].endswith(","):
        lines[-1] = lines[-1].rstrip(",")
    return lines


def generate_header(items: List[PdrItem], macro_cfg: Dict[str, Any], out_path: Path, c_path: Path | None) -> None:
    offset_map = compute_repo_offsets(items)
    total_size = sum(len(i.payload) for i in items)
    header_lines = [
        "/* Auto-generated by generate_pdr_repo.py. Do not edit. */",
        "#pragma once",
        "#include <stdint.h>",
        "",
        f"#define PDR_REPOSITORY_SIZE {total_size}u",
        f"#define PDR_COUNT {len(items)}u",
        "",
        "typedef struct { uint16_t handle; uint32_t offset; } pdr_offset_t;",
        "",
    ]

    if c_path:
        header_lines.append("extern const uint8_t pdr_repository[PDR_REPOSITORY_SIZE];")
        header_lines.append("extern const pdr_offset_t pdr_offsets[PDR_COUNT];")
        header_lines.append("")
    else:
        header_lines.append("/* Binary PDR repository (header + body per record) */")
        header_lines.append(f"static const uint8_t pdr_repository[PDR_REPOSITORY_SIZE] = {{")
        header_lines.extend(emit_repo_definitions(items, "pdr_repository"))
        header_lines.append("};")
        header_lines.append("")
        header_lines.append("/* Handle->offset table */")
        header_lines.append(f"static const pdr_offset_t pdr_offsets[PDR_COUNT] = {{")
        for it in items:
            header_lines.append(f"  {{ {it.handle}u, {offset_map[it.handle]}u }},")
        header_lines.append("};")
        header_lines.append("")

    header_lines.extend(emit_macros(items, offset_map, macro_cfg))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(header_lines), encoding="ascii")

    if c_path:
        c_lines = [
            "/* Auto-generated by generate_pdr_repo.py. Do not edit. */",
            "#include \"pdr_repo.h\"",
            "",
            "const uint8_t pdr_repository[PDR_REPOSITORY_SIZE] = {",
        ]
        c_lines.extend(emit_repo_definitions(items, "pdr_repository"))
        c_lines.append("};")
        c_lines.append("")
        c_lines.append("const pdr_offset_t pdr_offsets[PDR_COUNT] = {")
        for it in items:
            c_lines.append(f"  {{ {it.handle}u, {offset_map[it.handle]}u }},")
        c_lines.append("};")
        c_lines.append("")
        c_path.parent.mkdir(parents=True, exist_ok=True)
        c_path.write_text("\n".join(c_lines), encoding="ascii")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate PLDM PDR C header from YAML directory.")
    parser.add_argument("--pdr-dir", required=True, type=Path, help="Directory containing PDR YAML files")
    parser.add_argument("--schema-dir", required=True, type=Path, help="Directory containing PLDM JSON schemas")
    parser.add_argument("--macro-defs", required=True, type=Path, help="macro_defs.yaml")
    parser.add_argument("--out", required=True, type=Path, help="Output header path")
    parser.add_argument("--c-out", type=Path, help="Optional C source output (emit externs in header)")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    items = load_pdrs_from_dir(args.pdr_dir, args.schema_dir)
    macro_cfg = load_yaml(args.macro_defs)
    generate_header(items, macro_cfg, args.out, args.c_out)
    print(
        f"Generated {len(items)} PDR(s) into {args.out} "
        f"(total {sum(len(i.payload) for i in items)} bytes) from {args.pdr_dir}"
    )


if __name__ == "__main__":
    main()
