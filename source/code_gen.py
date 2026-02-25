import yaml
import json
import re
import struct
import argparse
import sys
import os
import glob
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
        encoding = field_schema.get('pldmEncoding', 'utf-8')
        if encoding == 'utf-16be':
            encoded = value.encode('utf-16-be')
        else:
            encoded = value.encode(encoding)
        return encoded + b'\x00' if 'null-terminated' in field_schema.get('description', '').lower() else encoded
    
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
    
    # Infer PDR type from filename (e.g., type_1.yaml -> 1)
    try:
        pdr_type = int(filename.split('_')[1].split('.')[0])
    except ValueError:
        raise ValueError(f"Invalid filename format for {filename}; expected type_<num>.yaml")
    
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
    
    return assigned_handle, pdr_type, body_buffer, filename.replace('.yaml', '')

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


def generate_all(yaml_dir, schema_dir, output_file, macro_yaml=None):
    yaml_files = sorted(glob.glob(os.path.join(yaml_dir, 'type_*.yaml')))
    print(f"Found {len(yaml_files)} YAML files in {yaml_dir}")

    reserved_handles, duplicates = collect_reserved_handles(yaml_files)
    if duplicates:
        print(f"Warning: Duplicate recordHandle values detected (will auto-renumber later occurrences): {sorted(duplicates)}")

    next_handle_ref = [1]  # Mutable for ref
    pdr_data = []
    for yaml_file in yaml_files:
        handle, pdr_type, body_data, var_name = process_single_yaml(
            yaml_file, schema_dir, reserved_handles, next_handle_ref)
        pdr_data.append((handle, pdr_type, body_data, var_name))

    # Sort by handle
    pdr_data.sort(key=lambda x: x[0])

    # Build single contiguous blob of all PDR body data (headers excluded —
    # pdr_repo_add_record() writes its own header at runtime).
    blob = b''
    init_entries = []  # (offset, size, pdr_type, var_name)
    for handle, pdr_type, body_data, var_name in pdr_data:
        offset = len(blob)
        blob += body_data
        init_entries.append((offset, len(body_data), pdr_type, var_name))

    # Generate C code
    out_dir = os.path.dirname(output_file)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(output_file, 'w') as out:
        out.write(f"// Generated by code_gen.py on {datetime.now().isoformat()}\n")
        out.write("#include <stdint.h>\n#include <stddef.h>\n")
        out.write('#include "pldm_pdr_repo.h"\n\n')

        if macro_yaml:
            macros_block = generate_bound_macros(macro_yaml, yaml_dir)
            if macros_block:
                out.write("// --- Bound Macros ---\n")
                out.write(macros_block + "\n\n")

        # Single contiguous blob with all PDR body data
        out.write("// Contiguous blob: all PDR body data (headers excluded)\n")
        out.write("static const uint8_t pdr_blob_data[] = {\n")
        for entry_idx, (offset, size, pdr_type, var_name) in enumerate(init_entries):
            out.write(f"    /* [{entry_idx}] {var_name}.yaml  "
                      f"type={pdr_type}  offset={offset}  size={size} */\n    ")
            for i in range(size):
                out.write(f"0x{blob[offset + i]:02X}, ")
                if (i + 1) % 16 == 0 and i + 1 < size:
                    out.write("\n    ")
            out.write("\n")
        out.write("};\n\n")

        # Init callback with direct calls
        out.write("// Init callback for pdr_repo_run_init_agent()\n")
        out.write("void pdr_repo_populate(pdr_repo_t *repo, void *ctx)\n{\n")
        out.write("    (void)ctx;\n")
        for offset, size, pdr_type, var_name in init_entries:
            out.write(f"    pdr_repo_add_record(repo, {pdr_type}, "
                      f"&pdr_blob_data[{offset}], {size}, NULL);  /* {var_name} */\n")
        out.write("}\n")

    print(f"Generated {output_file} with {len(pdr_data)} PDRs in single blob.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate C code from PLDM PDR YAMLs.")
    parser.add_argument('yaml_dir', help="Directory with YAML PDR files")
    parser.add_argument('schema_dir', help="Directory with JSON schemas")
    parser.add_argument('out', help="Output C file path")
    parser.add_argument('--macros', default=None,
                        help="Optional macro binding YAML file (e.g., macro_defs.yaml)")
    args = parser.parse_args()
    generate_all(args.yaml_dir, args.schema_dir, args.out, macro_yaml=args.macros)
