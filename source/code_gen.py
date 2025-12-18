import os
import glob
import argparse
import sys
from datetime import datetime
import json
import jsonschema
import struct
import yaml
from collections import Counter

HEADER_FIELD_TYPES = {
    'recordHandle': 'I',
    'PDRHeaderVersion': 'B',
    'PDRType': 'B',
    'recordChangeNumber': 'H',
    'dataLength': 'H'
}

def extract_value(value):
    if isinstance(value, dict):
        return value.get('value')
    return value

def is_auto_value(value):
    return isinstance(value, str) and value.lower() == 'auto'

def coerce_int(value, field, filename):
    try:
        return int(value)
    except ValueError:
        print(f"Error in {filename}: {field} must be an integer or convertible string")
        sys.exit(1)

def next_available_handle(used_handles, reserved_handles, start):
    h = start
    while h in used_handles or h in reserved_handles:
        h += 1
        if h > 0xFFFFFFFF:
            print("Error: No available recordHandle")
            sys.exit(1)
    return h

def collect_reserved_handles(yaml_files):
    reserved = set()
    duplicates = set()
    for yaml_file in yaml_files:
        with open(yaml_file, 'r') as f:
            data = yaml.safe_load(f)
        pdr_header = data.get('pdrHeader')
        if pdr_header:
            raw_handle = extract_value(pdr_header.get('recordHandle'))
            if not is_auto_value(raw_handle):
                h = coerce_int(raw_handle, 'recordHandle', yaml_file)
                if h in reserved:
                    duplicates.add(h)
                reserved.add(h)
    return reserved, duplicates

def process_single_yaml(yaml_file, schema_dir, used_handles, reserved_handles, next_handle):
    filename = os.path.basename(yaml_file)
    with open(yaml_file, 'r') as f:
        data = yaml.safe_load(f)
    pdr_header = data.get('pdrHeader')
    if not pdr_header:
        print(f"Error {filename}: Missing pdrHeader")
        sys.exit(1)
    pdr_type = coerce_int(extract_value(pdr_header.get('PDRType')), "PDRType", filename)
    schema_file = os.path.join(schema_dir, f"type_{pdr_type}.json")
    if not os.path.exists(schema_file):
        print(f"Error {filename}: Schema {schema_file} not found")
        sys.exit(1)
    with open(schema_file, 'r') as f:
        schema = json.load(f)
    try:
        jsonschema.validate(instance=data, schema=schema)
    except jsonschema.exceptions.ValidationError as err:
        print(f"Validation error in {filename}: {err}")
        sys.exit(1)
    var_name = os.path.splitext(filename)[0] + '_pdr'
    body_buffer = bytearray()
    def pack_field(field_schema, value, field_name=None):
        if value is None:
            value = field_schema.get('default')
            if value is None:
                print(f"Error {filename}: Missing value for {field_name}")
                sys.exit(1)
        field_type = field_schema['type']
        if field_type == 'integer':
            fmt = field_schema['binaryFormat']
            v = coerce_int(value, field_name, filename)
            return struct.pack(fmt, v)
        elif field_type == 'string':
            if not isinstance(value, str):
                print(f"Error {filename}: {field_name} must be string")
                sys.exit(1)
            charset = 'utf-16be'  # default
            content_media_type = field_schema.get('contentMediaType')
            if content_media_type:
                parts = content_media_type.split(';')
                for part in parts[1:]:
                    part = part.strip()
                    if part.startswith('charset='):
                        charset = part[8:]
                        break
            if 'utf-16' in charset.lower():
                terminator = b'\x00\x00'
            else:
                terminator = b'\x00'
            try:
                encoded = value.encode(charset)
            except UnicodeEncodeError:
                print(f"Error {filename}: {field_name} cannot be encoded in {charset}")
                sys.exit(1)
            return encoded + terminator
        elif field_type == 'object':
            sub_buffer = bytearray()
            order = field_schema.get('binaryOrder', list(field_schema['properties'].keys()))
            for sub_field in order:
                sub_schema = field_schema['properties'][sub_field]
                sub_value = value.get(sub_field)
                sub_buffer.extend(pack_field(sub_schema, sub_value, sub_field))
            return sub_buffer
        elif field_type == 'array':
            sub_buffer = bytearray()
            items_schema = field_schema['items']
            for idx, item in enumerate(value):
                sub_buffer.extend(pack_field(items_schema, item, f"{field_name}[{idx}]"))
            return sub_buffer
        else:
            print(f"Error {filename}: Unsupported type {field_type} for {field_name}")
            sys.exit(1)
    order = schema.get('binaryOrder', [])
    for field in order:
        field_schema = schema['properties'][field]
        value = data[field]
        body_buffer.extend(pack_field(field_schema, value, field))
    computed_data_length = len(body_buffer)
    if computed_data_length > 0xFFFF:
        print(f"Error {filename}: dataLength {computed_data_length} exceeds uint16 range")
        sys.exit(1)
    raw_handle = extract_value(pdr_header.get('recordHandle'))
    if is_auto_value(raw_handle):
        handle = next_available_handle(used_handles, reserved_handles, next_handle)
        used_handles.add(handle)
        next_handle = next_available_handle(used_handles, reserved_handles, handle + 1)
        print(f"  - Auto-assigned recordHandle {handle} for {filename}")
    else:
        handle = coerce_int(raw_handle, "recordHandle", filename)
        if handle in used_handles:
            new_handle = next_available_handle(used_handles, reserved_handles, max(next_handle, handle))
            print(f"  - recordHandle {handle} already used; reassigned to {new_handle} for {filename}")
            handle = new_handle
        used_handles.add(handle)
        next_handle = next_available_handle(used_handles, reserved_handles, max(next_handle, handle + 1))
    if handle < 0 or handle > 0xFFFFFFFF:
        print(f"Error {filename}: recordHandle {handle} is outside uint32 range")
        sys.exit(1)
    header_fields = {}
    for field in ['PDRHeaderVersion', 'PDRType', 'recordChangeNumber']:
        if field not in pdr_header:
            print(f"Error {filename}: Missing header field '{field}'")
            sys.exit(1)
        header_fields[field] = coerce_int(extract_value(pdr_header[field]), field, filename)
    provided_length = extract_value(pdr_header.get('dataLength'))
    if provided_length is not None and not is_auto_value(provided_length):
        provided_int = coerce_int(provided_length, "dataLength", filename)
        if provided_int != computed_data_length:
            print(f"  - Adjusted dataLength for {filename}: expected {computed_data_length} (was {provided_int})")
    header_fields['recordHandle'] = handle
    header_fields['dataLength'] = computed_data_length
    header_buffer = bytearray()
    for field in ['recordHandle', 'PDRHeaderVersion', 'PDRType', 'recordChangeNumber', 'dataLength']:
        header_buffer.extend(struct.pack(HEADER_FIELD_TYPES[field], header_fields[field]))
    byte_buffer = header_buffer + body_buffer
    print(f"  - Processed {filename} (Type {pdr_type}) -> {len(byte_buffer)} bytes (dataLength {computed_data_length})")
    return byte_buffer, var_name, next_handle, pdr_type

def generate_all(yaml_dir, schema_dir, output_file):
    if not os.path.exists(yaml_dir):
        print(f"YAML Directory not found: {yaml_dir}")
        sys.exit(1)
    yaml_files = glob.glob(os.path.join(yaml_dir, "*.yaml"))
    if not yaml_files:
        print("No .yaml files found in directory.")
        sys.exit(0)
    print(f"Found {len(yaml_files)} YAML files in {yaml_dir}")
    reserved_handles, duplicate_handles = collect_reserved_handles(yaml_files)
    if duplicate_handles:
        print(f"Warning: Duplicate recordHandle values detected (will auto-renumber later occurrences): {sorted(duplicate_handles)}")
    used_handles = set()
    next_handle = next_available_handle(used_handles, reserved_handles, 1)
    generated_arrays = []  # List of (var_name, size)
    pdr_counts = Counter()
    c_source = [
        "/*",
        f" * Generated by code_gen_dir.py",
        f" * Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        " */",
        "",
        "#include <stdint.h>",
        "#include <stddef.h>",
        ""
    ]
    for yaml_file in sorted(yaml_files):
        byte_data, var_name, next_handle, pdr_type = process_single_yaml(
            yaml_file, schema_dir, used_handles, reserved_handles, next_handle
        )
        pdr_counts[pdr_type] += 1
        if byte_data:
            c_source.append(f"// Source: {os.path.basename(yaml_file)}")
            c_source.append(f"const uint8_t {var_name}[] = {{")
            hex_lines = []
            for i in range(0, len(byte_data), 12):
                chunk = byte_data[i:i + 12]
                hex_lines.append("    " + ", ".join(f"0x{b:02X}" for b in chunk) + ",")
            if hex_lines:
                hex_lines[-1] = hex_lines[-1].rstrip(',')
            c_source.extend(hex_lines)
            c_source.append("};")
            c_source.append(f"const size_t {var_name}_size = sizeof({var_name});")
            c_source.append("")
            generated_arrays.append(var_name)
    c_source.append("/* --- PDR Registry --- */")
    c_source.append("typedef struct {")
    c_source.append("    const uint8_t* data;")
    c_source.append("    size_t size;")
    c_source.append("} pdr_entry_t;")
    c_source.append("")
    c_source.append("const pdr_entry_t pdr_registry[] = {")
    for name in generated_arrays:
        c_source.append(f"    {{ {name}, {name}_size }},")
    c_source.append("};")
    c_source.append("")
    c_source.append(f"const size_t pdr_registry_count = {len(generated_arrays)};")
    output_dir = os.path.dirname(output_file)
    if output_dir and not os.path.exists(output_dir):
        os.makedirs(output_dir)
    with open(output_file, 'w') as f:
        f.write("\n".join(c_source))
    print(f"Success! Generated {output_file} containing {len(generated_arrays)} PDRs.")
    print("\nPDR Type Counts:")
    for typ, cnt in sorted(pdr_counts.items()):
        print(f"Type {typ}: {cnt} PDRs")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate C code from directory of PLDM YAMLs")
    parser.add_argument("yaml_dir", help="Directory containing .yaml files")
    parser.add_argument("schema_dir", help="Directory containing .json schema files")
    parser.add_argument("out", help="Output .c file path")
    args = parser.parse_args()
    generate_all(args.yaml_dir, args.schema_dir, args.out)
