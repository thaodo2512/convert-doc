import yaml
import json
import re
import struct
import argparse
import sys
import os
from datetime import datetime
from jsonschema import validate, ValidationError

DOC_META_KEYS = {"docHidden", "_doc_hidden", "_docHide", "_doc"}

FMT_MAP = {
    'ver32': 'I',
    # Add more special formats if needed, e.g., 'time64': 'Q'
}

# Tokenizes a field path like "pdrHeader.recordHandle.value" or
# "stateSensors[0].stateSetID.value" into a list of keys and int indices.
_PATH_TOKEN = re.compile(r'([^.\[\]]+)|\[(\d+)\]')

def is_hidden(node):
    if not isinstance(node, dict):
        return False
    meta = node.get("_doc")
    if isinstance(meta, dict) and meta.get("hidden"):
        return True
    return any(node.get(key) is True for key in DOC_META_KEYS if key != "_doc")

def clean_for_validation(node, schema_props=None):
    if schema_props is None:
        schema_props = {}
    if isinstance(node, dict):
        if 'value' in node:
            return clean_for_validation(node['value'], schema_props)
        cleaned = {}
        for k, v in node.items():
            if k in DOC_META_KEYS:
                continue
            sub_props = schema_props.get(k, {}).get('properties', {}) if schema_props.get(k, {}).get('type') == 'object' else {}
            cleaned[k] = clean_for_validation(v, sub_props)
        return cleaned
    elif isinstance(node, list):
        item_props = schema_props.get('items', {}).get('properties', {}) if schema_props.get('items') else {}
        return [clean_for_validation(i, item_props) for i in node]
    else:
        return node

def resolve_subschema(condition_data, root_schema, current_subschema, key):
    if 'allOf' in root_schema:
        for cond in root_schema['allOf']:
            if_cond = cond.get('if', {})
            matches = True
            for prop, cond_val in if_cond.get('properties', {}).items():
                data_val = condition_data.get(prop)
                if data_val != cond_val.get('const'):
                    matches = False
                    break
            if matches:
                cond_sub = cond.get('then', {}).get('properties', {}).get(key, {})
                if cond_sub:
                    return cond_sub
    return current_subschema

def coerce_int(value, field, filename):
    try:
        return int(value)
    except ValueError:
        print(f"Warning: Non-integer {field} in {filename}: '{value}' - treating as 0")
        return 0

def discover_yaml_files(yaml_dir):
    """Find all YAML files recursively, ordered parent-dir-first then alphabetically.

    Only files containing a 'pdrHeader' key are returned (non-PDR YAML files
    such as macro_defs.yaml are silently skipped).
    """
    all_yaml = []
    for dirpath, _dirnames, filenames in os.walk(yaml_dir):
        for fn in sorted(filenames):
            if fn.endswith(('.yaml', '.yml')):
                all_yaml.append(os.path.join(dirpath, fn))

    # Stable sort by directory depth (parent dirs first); os.walk already
    # visits parents before children, but sorting makes it explicit.
    all_yaml.sort(key=lambda p: (p.count(os.sep), p))

    # Filter to PDR files only (must have 'pdrHeader')
    pdr_files = []
    for path in all_yaml:
        with open(path, 'r') as f:
            data = yaml.safe_load(f)
        if isinstance(data, dict) and 'pdrHeader' in data:
            pdr_files.append(path)
        else:
            print(f"Skipping non-PDR file: {path}")
    return pdr_files


def collect_reserved_handles(yaml_files):
    reserved = {}
    duplicates = set()
    for yaml_file in yaml_files:
        with open(yaml_file, 'r') as f:
            data = yaml.safe_load(f)
        pdr_header = data.get('pdrHeader', {})
        raw_handle = pdr_header.get('recordHandle')
        if isinstance(raw_handle, dict):
            raw_handle = raw_handle.get('value')
        if raw_handle not in (None, 'auto', 'auto-gen'):
            h = coerce_int(raw_handle, 'recordHandle', yaml_file)
            if h in reserved:
                duplicates.add(h)
            reserved[h] = yaml_file
    return reserved, duplicates

def assign_handle(next_handle, reserved_handles, yaml_file, handle):
    if handle in (None, 'auto', 'auto-gen'):
        while next_handle in reserved_handles:
            next_handle += 1
        print(f"Auto-assigned recordHandle {next_handle} for {yaml_file}")
        return next_handle
    else:
        return handle

def infer_format(field_schema, field_type):
    if field_type == 'integer':
        min_val = field_schema.get('minimum', 0)
        max_val = field_schema.get('maximum', 4294967295)
        signed = min_val < 0 or 'signed' in field_schema.get('description', '').lower()
        if signed:
            if max_val <= 127:
                return 'b'
            elif max_val <= 32767:
                return 'h'
            elif max_val <= 2147483647:
                return 'i'
            else:
                return 'q'
        else:
            if max_val <= 255:
                return 'B'
            elif max_val <= 65535:
                return 'H'
            elif max_val <= 4294967295:
                return 'I'
            else:
                return 'Q'
    elif field_type == 'number':
        return 'd' if 'double' in field_schema.get('description', '').lower() else 'f'
    elif field_type == 'boolean':
        return 'B'
    raise ValueError(f"Cannot infer format for type {field_type}")

def pack_field(field_schema, value, field_name, full_data=None):
    field_type = field_schema.get('type')
    bf = field_schema.get('binaryFormat', '')
    
    if full_data is not None and 'x-binary-type-field' in field_schema:
        type_field = field_schema['x-binary-type-field']
        if type_field in full_data:
            key = str(full_data[type_field])
            bf = field_schema.get('x-binary-type-mapping', {}).get(key, bf)

    if full_data is not None and 'formatResolver' in field_schema:
        resolver = field_schema['formatResolver']
        depends_on = resolver.get('dependsOn')
        if depends_on in full_data:
            key = str(full_data[depends_on])
            bf = resolver['mapping'].get(key, bf)
    
    bf = FMT_MAP.get(bf, bf)
    
    if bf and bf != 'variable':
        try:
            if field_type == 'array' and bf.isdigit() or any(c.isdigit() for c in bf):
                # For '16B' etc., unpack value as list
                return struct.pack('<' + bf, *value)
            else:
                return struct.pack('<' + bf, value)
        except struct.error as e:
            raise ValueError(f"Packing error for {field_name}: {e}")
    
    if field_type == 'object':
        packed = b''
        effective_schema = field_schema
        if 'oneOf' in field_schema:
            for variant in field_schema['oneOf']:
                variant_props = variant.get('properties', {})
                if all(value.get(p) == s['const'] for p, s in variant_props.items() if 'const' in s):
                    effective_schema = variant
                    break
        sub_order = effective_schema.get('binaryOrder', list(value.keys()))
        sub_props = effective_schema.get('properties', {})
        for sub_field in sub_order:
            sub_value = value.get(sub_field)
            sub_schema = sub_props.get(sub_field, {})
            packed += pack_field(sub_schema, sub_value, f"{field_name}.{sub_field}", full_data=full_data)
        return packed
    
    elif field_type == 'array':
        packed = b''
        item_schema = field_schema.get('items', {})
        for item in value:
            packed += pack_field(item_schema, item, field_name, full_data=full_data)
        return packed
    
    elif field_type == 'string':
        encoding = field_schema.get('x-binary-encoding',
                                    field_schema.get('pldmEncoding', 'utf-8'))
        if encoding == 'utf-16be':
            encoded = value.encode('utf-16-be')
        elif encoding == 'us-ascii':
            encoded = value.encode('ascii')
        else:
            encoded = value.encode(encoding)
        terminator = field_schema.get('x-binary-terminator', '')
        if terminator == '0x0000':
            return encoded + b'\x00\x00'
        elif terminator == '0x00' or 'null-terminated' in field_schema.get('description', '').lower():
            return encoded + b'\x00'
        return encoded
    
    elif bf == 'variable':
        bytes_list = None
        if isinstance(value, list):
            bytes_list = value
        elif isinstance(value, bytes):
            return value
        elif isinstance(value, str):
            try:
                if '0x' in value:
                    bytes_list = [int(b, 16) for b in value.split()]
                else:
                    bytes_list = [int(value[i:i+2], 16) for i in range(0, len(value), 2)]
            except ValueError:
                raise ValueError(f"Invalid hex string for {field_name}")
        elif isinstance(value, int):
            if value == 0:
                return b''
            byte_length = (value.bit_length() + 7) // 8
            return value.to_bytes(byte_length, 'little', signed=value < 0)
        else:
            raise ValueError(f"Unsupported type {type(value)} for variable field {field_name}: expected list of ints, bytes, or hex str")
        return struct.pack(f'<{len(bytes_list)}B', *bytes_list)
    
    elif field_type in ('integer', 'number', 'boolean'):
        fmt = infer_format(field_schema, field_type)
        try:
            if field_type == 'boolean':
                value = 1 if value else 0
            return struct.pack('<' + fmt, value)
        except struct.error as e:
            raise ValueError(f"Packing error for inferred {fmt} in {field_name}: {e}")
    
    else:
        raise ValueError(f"No binaryFormat or unsupported type {field_type} for {field_name}")

def process_single_yaml(yaml_file, schema_dir, reserved_handles, next_handle_ref):
    filename = os.path.basename(yaml_file)
    with open(yaml_file, 'r') as f:
        raw_data = yaml.safe_load(f)
    
    # Read PDR type from the YAML data (pdrHeader.PDRType)
    pdr_header_raw = raw_data.get('pdrHeader', {})
    pdr_type_val = pdr_header_raw.get('PDRType')
    if isinstance(pdr_type_val, dict):
        pdr_type_val = pdr_type_val.get('value')
    if pdr_type_val is None:
        raise ValueError(f"Missing pdrHeader.PDRType in {filename}")
    pdr_type = coerce_int(pdr_type_val, 'PDRType', filename)
    
    schema_file = os.path.join(schema_dir, f"type_{pdr_type}.json")
    if not os.path.exists(schema_file):
        raise FileNotFoundError(f"Schema not found: {schema_file}")
    
    with open(schema_file, 'r') as f:
        schema = json.load(f)
    
    schema_props = schema.get('properties', {})
    cleaned_data = clean_for_validation(raw_data, schema_props)
    
    # Validate
    try:
        validate(instance=cleaned_data, schema=schema)
    except ValidationError as e:
        print(f"Validation error in {filename}: {e}")
        sys.exit(1)
    
    pdr_header = cleaned_data.get('pdrHeader', {})
    handle = pdr_header.get('recordHandle')
    if handle not in (None, 'auto', 'auto-gen'):
        handle = coerce_int(handle, 'recordHandle', filename)
    assigned_handle = assign_handle(next_handle_ref[0], reserved_handles, filename, handle)
    if assigned_handle != handle:
        pdr_header['recordHandle'] = assigned_handle  # Update for packing
    next_handle_ref[0] = max(next_handle_ref[0], assigned_handle) + 1
    
    # Pack header (fixed format: uint32 handle, uint8 version, uint8 type, uint16 change_num, uint16 data_len)
    header_buffer = struct.pack('<IBBHH', assigned_handle, pdr_header['PDRHeaderVersion'], pdr_type, pdr_header['recordChangeNumber'], 0)  # Placeholder data_len
    
    # Pack body in binaryOrder
    body_buffer = b''
    order = schema.get('binaryOrder', list(cleaned_data.keys()))
    root_schema = schema
    condition_data = cleaned_data
    for field in order:
        if field == 'pdrHeader':
            continue
        value = cleaned_data.get(field)
        field_schema = schema_props.get(field, {})
        field_schema = resolve_subschema(condition_data, root_schema, field_schema, field)
        # Include even if hidden, since for binary
        body_buffer += pack_field(field_schema, value, field, full_data=condition_data)
    
    # Update data_len in header (auto-calculate; warn if YAML stated a different value)
    data_len = len(body_buffer)
    stated_len = pdr_header.get('dataLength')
    if stated_len not in (None, 'auto', 'auto-gen') and isinstance(stated_len, int) and stated_len != data_len:
        print(f"Warning: {filename}: stated dataLength {stated_len} != calculated {data_len}; using calculated value.")
    header_buffer = header_buffer[:-2] + struct.pack('<H', data_len)
    
    return assigned_handle, pdr_type, header_buffer, body_buffer, filename.replace('.yaml', '')

def resolve_field_path(data, field_path):
    """Traverse a loaded YAML dict using a dot/bracket path string.

    Supports:
      "pdrHeader.recordHandle.value"          -> dict keys
      "stateSensors[0].stateSetID.value"      -> dict key + list index + dict keys
      "possibleStates[0][1]"                  -> nested list indices

    Returns the resolved value, or None (with a warning) on any failure.
    """
    tokens = []
    for m in _PATH_TOKEN.finditer(field_path):
        if m.group(1) is not None:
            tokens.append(m.group(1))
        else:
            tokens.append(int(m.group(2)))

    current = data
    for token in tokens:
        if isinstance(token, int):
            if not isinstance(current, list):
                print(f"Warning: path '{field_path}': expected list at index [{token}], "
                      f"got {type(current).__name__}")
                return None
            if token >= len(current):
                print(f"Warning: path '{field_path}': index [{token}] out of range "
                      f"(list length {len(current)})")
                return None
            current = current[token]
        else:
            if not isinstance(current, dict):
                print(f"Warning: path '{field_path}': expected dict at key '{token}', "
                      f"got {type(current).__name__}")
                return None
            if token not in current:
                print(f"Warning: path '{field_path}': key '{token}' not found "
                      f"(available: {list(current.keys())})")
                return None
            current = current[token]

    return current


def _format_c_value(value):
    """Format a Python value as a C macro literal."""
    if isinstance(value, bool):
        return '1' if value else '0'
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return f'{value}f'
    if isinstance(value, str):
        return f'"{value}"'
    return str(value)


def generate_bound_macros(macro_yaml_path, data_folder):
    """Load macro_defs.yaml and emit a block of C #define lines.

    Each entry in the binding file specifies:
      name:  C macro identifier
      file:  data YAML filename (relative to data_folder)
      field: dot/bracket path into the loaded YAML

    Files are cached so each data YAML is read from disk only once.
    Returns a string ready to write into the C output file, or '' if
    the binding file is absent or contains no valid entries.
    """
    if not os.path.exists(macro_yaml_path):
        print(f"Warning: macro binding file '{macro_yaml_path}' not found; skipping macros.")
        return ''

    with open(macro_yaml_path, 'r') as f:
        macro_defs = yaml.safe_load(f)

    if not macro_defs or 'macros' not in macro_defs:
        print(f"Warning: '{macro_yaml_path}' has no 'macros' key; skipping macros.")
        return ''

    file_cache = {}   # filename -> loaded dict (or None on load error)
    lines = []

    for defn in macro_defs['macros']:
        name       = defn.get('name')
        file_name  = defn.get('file')
        field_path = defn.get('field')

        if not all([name, file_name, field_path]):
            print(f"Warning: incomplete macro entry {defn}; skipping.")
            continue

        if file_name not in file_cache:
            full_path = os.path.join(data_folder, file_name)
            try:
                with open(full_path, 'r') as f:
                    file_cache[file_name] = yaml.safe_load(f)
            except FileNotFoundError:
                print(f"Warning: data file '{full_path}' not found for macro '{name}'.")
                file_cache[file_name] = None

        data = file_cache[file_name]
        if data is None:
            continue

        value = resolve_field_path(data, field_path)
        if value is None:
            print(f"Warning: macro '{name}': could not resolve '{field_path}' in '{file_name}'.")
            continue

        lines.append(f"#define {name} {_format_c_value(value)}")

    return '\n'.join(lines)


def generate_all(yaml_dir, schema_dir, output_file, macro_yaml=None,
                  headroom_pct=25):
    yaml_files = discover_yaml_files(yaml_dir)
    print(f"Found {len(yaml_files)} PDR YAML files in {yaml_dir}")

    reserved_handles, duplicates = collect_reserved_handles(yaml_files)
    if duplicates:
        print(f"Warning: Duplicate recordHandle values detected (will auto-renumber later occurrences): {sorted(duplicates)}")

    next_handle_ref = [1]  # Mutable for ref
    pdr_data = []
    for yaml_file in yaml_files:
        handle, pdr_type, header_data, body_data, var_name = process_single_yaml(
            yaml_file, schema_dir, reserved_handles, next_handle_ref)
        pdr_data.append((handle, pdr_type, header_data, body_data, var_name))

    # Sort by handle
    pdr_data.sort(key=lambda x: x[0])

    # Build full-record blob (header + body) for zero-copy init
    full_blob = b''
    # Build body-only backup blob for RunInitAgent rebuild
    backup_blob = b''
    full_entries = []   # (handle, offset, total_size, body_size, pdr_type, var_name)
    backup_entries = [] # (offset, body_size, pdr_type, var_name)
    for handle, pdr_type, header_data, body_data, var_name in pdr_data:
        full_offset = len(full_blob)
        full_blob += header_data + body_data
        total_size = len(header_data) + len(body_data)
        full_entries.append((handle, full_offset, total_size, len(body_data), pdr_type, var_name))

        backup_offset = len(backup_blob)
        backup_blob += body_data
        backup_entries.append((backup_offset, len(body_data), pdr_type, var_name))

    # Capacity with headroom
    data_size = len(full_blob)
    capacity = int(data_size * (1 + headroom_pct / 100.0))
    # Align to 4 bytes
    capacity = (capacity + 3) & ~3

    # --- Compute statistics ---
    total_count = len(full_entries)
    handles = [h for h, _, _, _, _, _ in full_entries]
    record_sizes = [total_size for _, _, total_size, _, _, _ in full_entries]
    body_sizes = [body_size for _, _, _, body_size, _, _ in full_entries]
    max_record_size = max(record_sizes) if record_sizes else 0
    min_record_size = min(record_sizes) if record_sizes else 0
    max_body_size = max(body_sizes) if body_sizes else 0
    min_body_size = min(body_sizes) if body_sizes else 0
    max_handle = max(handles) if handles else 0
    min_handle = min(handles) if handles else 0

    # Count records per PDR type
    type_counts = {}
    for _, _, _, _, pdr_type, _ in full_entries:
        type_counts[pdr_type] = type_counts.get(pdr_type, 0) + 1

    # Derive .h path from output .c path
    out_dir = os.path.dirname(output_file)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    base_name = os.path.splitext(os.path.basename(output_file))[0]
    header_file = os.path.join(out_dir, base_name + '.h') if out_dir else base_name + '.h'
    header_basename = os.path.basename(header_file)
    guard = header_basename.upper().replace('.', '_').replace('-', '_') + '_'
    timestamp = datetime.now().isoformat()

    # Helper: var_name -> C macro suffix (uppercase, non-alnum -> _)
    def to_macro_id(var_name):
        return re.sub(r'[^A-Za-z0-9]', '_', var_name).upper()

    # --- Generate header (.h) ---
    with open(header_file, 'w') as hdr:
        hdr.write(f"// Generated by code_gen.py on {timestamp}\n")
        hdr.write(f"#ifndef {guard}\n#define {guard}\n\n")
        hdr.write("#include <stdint.h>\n#include <stddef.h>\n")
        hdr.write('#include "pldm_pdr_repo.h"\n\n')

        if macro_yaml:
            macros_block = generate_bound_macros(macro_yaml, yaml_dir)
            if macros_block:
                hdr.write("// --- Bound Macros ---\n")
                hdr.write(macros_block + "\n\n")

        hdr.write(f"#define PDR_BLOB_CAPACITY      {capacity}\n")
        hdr.write(f"#define PDR_BLOB_DATA_SIZE     {data_size}\n")
        hdr.write(f"#define PDR_HEADER_SIZE        10  // common PDR header: 4+1+1+2+2\n\n")

        # --- PDR Statistics ---
        hdr.write("// --- PDR Statistics ---\n")
        hdr.write(f"#define PDR_COUNT              {total_count}\n")
        hdr.write(f"#define PDR_TYPE_COUNT         {len(type_counts)}\n")
        hdr.write(f"#define PDR_MAX_RECORD_SIZE    {max_record_size}  // header + body\n")
        hdr.write(f"#define PDR_MIN_RECORD_SIZE    {min_record_size}\n")
        hdr.write(f"#define PDR_MAX_BODY_SIZE      {max_body_size}  // body only\n")
        hdr.write(f"#define PDR_MIN_BODY_SIZE      {min_body_size}\n")
        hdr.write(f"#define PDR_MAX_HANDLE         {max_handle}\n")
        hdr.write(f"#define PDR_MIN_HANDLE         {min_handle}\n\n")

        # Per-type record counts
        hdr.write("// --- Per-Type Record Counts ---\n")
        for pdr_type in sorted(type_counts):
            hdr.write(f"#define PDR_TYPE{pdr_type}_COUNT       {type_counts[pdr_type]}\n")
        hdr.write("\n")

        # Per-record handle, offset, size
        hdr.write("// --- Per-Record Handle / Offset / Size ---\n")
        for handle, offset, total_size, body_size, pdr_type, var_name in full_entries:
            mid = to_macro_id(var_name)
            hdr.write(f"#define PDR_HANDLE_{mid}    {handle}\n")
            hdr.write(f"#define PDR_OFFSET_{mid}    {offset}\n")
            hdr.write(f"#define PDR_SIZE_{mid}      {total_size}  // body={body_size}\n")
        hdr.write("\n")

        # X-macro type list
        hdr.write("// --- X-Macro: list of all PDR types present ---\n")
        hdr.write("//\n")
        hdr.write("// PDR_TYPE_ENTRY(type, count)\n")
        hdr.write("//   type  - PDR type number (e.g., 1 = Terminus Locator, 2 = Numeric Sensor)\n")
        hdr.write("//   count - number of records of that type in this repository\n")
        hdr.write("//\n")
        hdr.write("// Example 1: Build a lookup table\n")
        hdr.write("//\n")
        hdr.write("//   static const struct { uint8_t type; uint8_t count; } pdr_types[] = {\n")
        hdr.write("//   #define PDR_TYPE_ENTRY(type, count) { type, count },\n")
        hdr.write("//       PDR_TYPE_LIST\n")
        hdr.write("//   #undef PDR_TYPE_ENTRY\n")
        hdr.write("//   };\n")
        hdr.write("//\n")
        hdr.write("// Example 2: Check if a PDR type exists\n")
        hdr.write("//\n")
        hdr.write("//   bool is_known_pdr_type(uint8_t t) {\n")
        hdr.write("//   #define PDR_TYPE_ENTRY(type, count) if (t == type) return true;\n")
        hdr.write("//       PDR_TYPE_LIST\n")
        hdr.write("//   #undef PDR_TYPE_ENTRY\n")
        hdr.write("//       return false;\n")
        hdr.write("//   }\n")
        hdr.write("//\n")
        hdr.write("// Example 3: Get record count for a type in a switch\n")
        hdr.write("//\n")
        hdr.write("//   uint8_t pdr_type_record_count(uint8_t t) {\n")
        hdr.write("//       switch (t) {\n")
        hdr.write("//   #define PDR_TYPE_ENTRY(type, count) case type: return count;\n")
        hdr.write("//       PDR_TYPE_LIST\n")
        hdr.write("//   #undef PDR_TYPE_ENTRY\n")
        hdr.write("//       default: return 0;\n")
        hdr.write("//       }\n")
        hdr.write("//   }\n")
        hdr.write("//\n")
        hdr.write("#define PDR_TYPE_LIST \\\n")
        sorted_types = sorted(type_counts.items())
        for i, (pdr_type, count) in enumerate(sorted_types):
            trailing = " \\\n" if i < len(sorted_types) - 1 else "\n"
            hdr.write(f"    PDR_TYPE_ENTRY({pdr_type}, {count}){trailing}")
        hdr.write("\n")

        hdr.write("void pdr_repo_populate_ext(pdr_repo_t *repo, void *ctx);\n")
        hdr.write("void pdr_repo_populate(pdr_repo_t *repo, void *ctx);\n\n")

        hdr.write(f"#endif // {guard}\n")

    # --- Generate source (.c) ---
    with open(output_file, 'w') as out:
        out.write(f"// Generated by code_gen.py on {timestamp}\n")
        out.write(f'#include "{header_basename}"\n\n')

        # --- Mutable full-record blob (header + body, with headroom) ---
        out.write("// Mutable blob: full records (10-byte header + body), with headroom for runtime adds\n")
        out.write(f"static uint8_t pdr_blob_data[PDR_BLOB_CAPACITY] = {{\n")
        for entry_idx, (handle, offset, total_size, body_size, pdr_type, var_name) in enumerate(full_entries):
            out.write(f"    /* [{entry_idx}] {var_name}.yaml  "
                      f"handle={handle}  type={pdr_type}  offset={offset}  total={total_size} */\n    ")
            for i in range(total_size):
                out.write(f"0x{full_blob[offset + i]:02X}, ")
                if (i + 1) % 16 == 0 and i + 1 < total_size:
                    out.write("\n    ")
            out.write("\n")
        out.write("};\n\n")

        # --- Const body-only backup blob (for RunInitAgent rebuild) ---
        out.write("// Const backup blob: body-only data for RunInitAgent rebuild\n")
        out.write("static const uint8_t pdr_blob_backup[] = {\n")
        for entry_idx, (offset, size, pdr_type, var_name) in enumerate(backup_entries):
            out.write(f"    /* [{entry_idx}] {var_name}.yaml  "
                      f"type={pdr_type}  offset={offset}  size={size} */\n    ")
            for i in range(size):
                out.write(f"0x{backup_blob[offset + i]:02X}, ")
                if (i + 1) % 16 == 0 and i + 1 < size:
                    out.write("\n    ")
            out.write("\n")
        out.write("};\n\n")

        # --- pdr_repo_populate_ext(): zero-copy fast init ---
        out.write("// Fast zero-copy init: indexes pre-filled records in pdr_blob_data\n")
        out.write("void pdr_repo_populate_ext(pdr_repo_t *repo, void *ctx)\n{\n")
        out.write("    (void)ctx;\n")
        out.write(f"    pdr_repo_init_ext(repo, pdr_blob_data, PDR_BLOB_CAPACITY);\n")
        out.write(f"    repo->blob_used = PDR_BLOB_DATA_SIZE;\n")
        for handle, offset, total_size, body_size, pdr_type, var_name in full_entries:
            out.write(f"    pdr_repo_index_record(repo, {offset});  /* {var_name} */\n")
        out.write("    pdr_repo_update_info(repo);\n")
        out.write("}\n\n")

        # --- pdr_repo_populate(): rebuild callback for RunInitAgent ---
        out.write("// Rebuild callback for pdr_repo_run_init_agent()\n")
        out.write("void pdr_repo_populate(pdr_repo_t *repo, void *ctx)\n{\n")
        out.write("    (void)ctx;\n")
        for offset, size, pdr_type, var_name in backup_entries:
            out.write(f"    pdr_repo_add_record(repo, {pdr_type}, "
                      f"&pdr_blob_backup[{offset}], {size}, NULL);  /* {var_name} */\n")
        out.write("}\n")

    print(f"Generated {output_file} and {header_file} with {len(pdr_data)} PDRs "
          f"(blob={data_size}B, capacity={capacity}B, headroom={headroom_pct}%).")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate C code from PLDM PDR YAMLs.")
    parser.add_argument('yaml_dir', help="Directory with YAML PDR files")
    parser.add_argument('schema_dir', help="Directory with JSON schemas")
    parser.add_argument('out', help="Output C file path")
    parser.add_argument('--macros', default=None,
                        help="Optional macro binding YAML file (e.g., macro_defs.yaml)")
    parser.add_argument('--headroom-pct', type=int, default=25,
                        help="Percent headroom in mutable blob for runtime adds (default: 25)")
    args = parser.parse_args()
    generate_all(args.yaml_dir, args.schema_dir, args.out,
                 macro_yaml=args.macros, headroom_pct=args.headroom_pct)
