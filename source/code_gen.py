import yaml
import json
import struct
import argparse
import sys
import os
import glob
from datetime import datetime
from jsonschema import validate, ValidationError

DOC_META_KEYS = {"docHidden", "_doc_hidden", "_docHide", "_doc"}

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

def pack_field(field_schema, value, field_name):
    field_type = field_schema.get('type')
    bf = field_schema.get('binaryFormat', '')
    
    if bf and bf != 'variable':
        try:
            if field_type == 'array':
                return struct.pack(bf, *value)
            else:
                return struct.pack(bf, value)
        except struct.error as e:
            raise ValueError(f"Packing error for {field_name}: {e}")
    
    if field_type == 'object':
        packed = b''
        sub_order = field_schema.get('binaryOrder', list(value.keys()))
        sub_props = field_schema.get('properties', {})
        for sub_field in sub_order:
            sub_value = value.get(sub_field)
            sub_schema = sub_props.get(sub_field, {})
            packed += pack_field(sub_schema, sub_value, f"{field_name}.{sub_field}")
        return packed
    
    elif field_type == 'array':
        packed = b''
        item_schema = field_schema.get('items', {})
        for item in value:
            packed += pack_field(item_schema, item, field_name)
        return packed
    
    elif field_type == 'string':
        encoding = field_schema.get('pldmEncoding', 'utf-8')
        if encoding == 'utf-16be':
            encoded = value.encode('utf-16-be')
        else:
            encoded = value.encode(encoding)
        return encoded + b'\x00' if 'null-terminated' in field_schema.get('description', '').lower() else encoded
    
    elif bf == 'variable':
        if field_type == 'array':
            packed = b''
            item_schema = field_schema.get('items', {'binaryFormat': 'B'})
            for item in value:
                packed += pack_field(item_schema, item, field_name)
            return packed
        elif field_type == 'string':
            return pack_field(field_schema, value, field_name)  # Use string handling above
        else:
            raise ValueError(f"Unsupported variable field type {field_type} for {field_name}")
    
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
        body_buffer += pack_field(field_schema, value, field)
    
    # Update data_len in header
    data_len = len(body_buffer)
    header_buffer = header_buffer[:-2] + struct.pack('<H', data_len)
    
    return assigned_handle, header_buffer + body_buffer, filename.replace('.yaml', '')

def generate_all(yaml_dir, schema_dir, output_file):
    yaml_files = sorted(glob.glob(os.path.join(yaml_dir, 'type_*.yaml')))
    print(f"Found {len(yaml_files)} YAML files in {yaml_dir}")
    
    reserved_handles, duplicates = collect_reserved_handles(yaml_files)
    if duplicates:
        print(f"Warning: Duplicate recordHandle values detected (will auto-renumber later occurrences): {sorted(duplicates)}")
    
    next_handle_ref = [1]  # Mutable for ref
    pdr_data = []
    for yaml_file in yaml_files:
        handle, binary_data, var_name = process_single_yaml(yaml_file, schema_dir, reserved_handles, next_handle_ref)
        pdr_data.append((handle, binary_data, var_name))
    
    # Sort by handle for registry
    pdr_data.sort(key=lambda x: x[0])
    
    # Generate C code
    os.makedirs(os.path.dirname(output_file), exist_ok=True)
    with open(output_file, 'w') as out:
        out.write(f"// Generated by code_gen.py on {datetime.now().isoformat()}\n")
        out.write("#include <stdint.h>\n#include <stddef.h>\n\n")
        
        for _, binary_data, var_name in pdr_data:
            out.write(f"// Source: {var_name}.yaml\n")
            out.write(f"const uint8_t {var_name}[] = {{\n    ")
            for i, byte in enumerate(binary_data):
                out.write(f"0x{byte:02X}, ")
                if (i + 1) % 16 == 0:
                    out.write("\n    ")
            out.write("\n};\n")
            out.write(f"const size_t {var_name}_size = sizeof({var_name});\n\n")
        
        out.write("// PDR Registry\n")
        out.write("typedef struct {\n    const uint8_t* data;\n    size_t size;\n} pdr_entry_t;\n\n")
        out.write("const pdr_entry_t pdr_registry[] = {\n")
        for _, _, var_name in pdr_data:
            out.write(f"    {{ {var_name}, {var_name}_size }},\n")
        out.write("};\n")
        out.write(f"const size_t pdr_registry_count = {len(pdr_data)};\n")
    
    print(f"Generated {output_file} with {len(pdr_data)} PDRs.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate C code from PLDM PDR YAMLs.")
    parser.add_argument('yaml_dir', help="Directory with YAML PDR files")
    parser.add_argument('schema_dir', help="Directory with JSON schemas")
    parser.add_argument('out', help="Output C file path")
    args = parser.parse_args()
    generate_all(args.yaml_dir, args.schema_dir, args.out)
