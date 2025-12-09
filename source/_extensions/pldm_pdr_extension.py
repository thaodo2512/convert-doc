import yaml
import json
from jsonschema import validate, ValidationError
from docutils import nodes
from docutils.statemachine import ViewList
from sphinx.util.docutils import SphinxDirective
import docutils.parsers.rst.directives as directives

DOC_META_KEYS = {"docHidden", "_doc_hidden", "_docHide", "_doc"}

def is_hidden(node):
    if not isinstance(node, dict):
        return False
    meta = node.get("_doc")
    if isinstance(meta, dict) and meta.get("hidden"):
        return True
    return any(node.get(key) is True for key in DOC_META_KEYS if key != "_doc")

class PldmPdrTableDirective(SphinxDirective):
    required_arguments = 2  # YAML file path, JSON schema file path
    has_content = False
    option_spec = {
        'caption': directives.unchanged,
        'name': directives.unchanged,
    }

    def run(self):
        env = self.state.document.settings.env
        
        # 1. Resolve paths
        _, yaml_abs_path = env.relfn2path(self.arguments[0])
        _, schema_abs_path = env.relfn2path(self.arguments[1])
        env.note_dependency(yaml_abs_path)
        env.note_dependency(schema_abs_path)

        # 2. Load Data
        try:
            with open(yaml_abs_path, 'r') as f:
                raw_data = yaml.safe_load(f)
            with open(schema_abs_path, 'r') as f:
                schema = json.load(f)
        except Exception as e:
            raise self.error(f"Failed to load files: {e}")

        # 3. Clean Data (for validation)
        def clean_for_validation(node):
            if isinstance(node, dict):
                if 'value' in node:
                    return clean_for_validation(node['value'])
                return {
                    k: clean_for_validation(v)
                    for k, v in node.items()
                    if k not in DOC_META_KEYS
                }
            elif isinstance(node, list):
                return [clean_for_validation(i) for i in node]
            else:
                return node

        validation_data = clean_for_validation(raw_data)

        # 4. Validate
        try:
            validate(instance=validation_data, schema=schema)
        except ValidationError as e:
            error_path = " -> ".join([str(p) for p in e.path])
            raise self.error(f"Schema Validation Failed at '{error_path}': {e.message}")

        # 5. Flatten Data (for table)
        rows = []
        def flatten(data, parent_key='', schema=schema, hidden=False):
            if hidden or is_hidden(data):
                return
            if isinstance(data, dict):
                if 'value' in data:
                    # Leaf Node
                    val = data['value']
                    comment = data.get('comment', '')
                    
                    if 'type' in data:
                        field_type = data['type']
                    else:
                        key_schema = schema
                        
                        # Improved type inference from schema
                        bf = key_schema.get('binaryFormat', '')
                        desc = key_schema.get('description', '').lower()
                        format_to_bits = {'B': 8, 'b': 8, 'H': 16, 'h': 16, 'I': 32, 'i': 32, 'Q': 64, 'q': 64, 'f': 32}
                        bits = format_to_bits.get(bf, '')
                        
                        if 'enum' in key_schema:
                            field_type = f"enum{bits}"
                        elif 'bitfield' in desc:
                            field_type = f"bitfield{bits}"
                        elif 'bool' in desc:
                            field_type = f"bool{bits}"
                        elif bf == 'variable':
                            field_type = 'variable'  # Override in YAML for specific type like uint32
                        elif bf in ['B', 'H', 'I', 'Q']:
                            field_type = f"uint{bits}"
                        elif bf in ['b', 'h', 'i', 'q']:
                            field_type = f"sint{bits}"
                        elif bf == 'f':
                            field_type = 'real32'
                        else:
                            # Fallback: parse from description
                            if desc:
                                type_part = desc.split(';')[0].split(':')[0].strip()
                                if type_part:
                                    field_type = type_part
                                else:
                                    field_type = 'unknown'
                            else:
                                field_type = 'unknown'

                    if parent_key:
                        display_name = parent_key.split('.')[-1].split('[')[0]  # Strips index if array
                    else:
                        display_name = ""

                    rows.append([field_type, display_name, str(val), comment])
                else:
                    # Container Node
                    for key, value in data.items():
                        if key in DOC_META_KEYS:
                            continue
                        full_key = f"{parent_key}.{key}" if parent_key else key
                        subschema = schema.get('properties', {}).get(key, {})
                        flatten(value, full_key, subschema, hidden=False)
            elif isinstance(data, list):
                subschema = schema if schema.get('type') != 'array' else schema.get('items', {})
                for i, item in enumerate(data):
                    full_key = f"{parent_key}[{i}]"
                    flatten(item, full_key, subschema, hidden=hidden or is_hidden(item))

        flatten(raw_data)

        if not rows:
            raise self.error("No data found to generate table.")

        # --- BUILD TABLE ---
        table = nodes.table()
        table['classes'] += ['colwidths-auto', 'tight-table']
        
        tgroup = nodes.tgroup(cols=4)
        table += tgroup

        for _ in range(4):
            tgroup += nodes.colspec(colwidth=1)

        # --- HEADER ---
        thead = nodes.thead()
        tgroup += thead
        
        row = nodes.row()
        for header in ['Type', 'Field Name', 'Value', 'Comment']:
            entry = nodes.entry()
            entry += nodes.paragraph(text=header)
            row += entry
        
        thead += row

        # --- BODY ---
        tbody = nodes.tbody()
        tgroup += tbody
        
        for row_data in rows:
            row = nodes.row()
            for i, cell in enumerate(row_data):
                entry = nodes.entry()
                if i == 3 and cell:
                    rst_content = ViewList()
                    for line in str(cell).splitlines():
                        rst_content.append(line, yaml_abs_path)
                    try:
                        self.state.nested_parse(rst_content, 0, entry, match_titles=False)
                    except Exception as e:
                        entry += nodes.paragraph(text=str(cell))
                        self.warning(f"Failed to parse RST in comment: {e}")
                else:
                    entry += nodes.paragraph(text=cell)
                row += entry
            
            tbody += row

        # --- ADD CAPTION FOR NUMBERING (if provided) ---
        if 'caption' in self.options:
            title = nodes.title('', self.options['caption'])
            table.insert(0, title)

        # --- ADD NAME FOR IMPLICIT LABEL (if provided) ---
        if 'name' in self.options:
            self.add_name(table)

        return [table]

def setup(app):
    app.add_directive('pldm-pdr-table', PldmPdrTableDirective)
    return {'version': '0.8', 'parallel_read_safe': True}