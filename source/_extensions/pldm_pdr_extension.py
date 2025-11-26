import yaml
import json
import os
from jsonschema import validate, ValidationError
from docutils import nodes
from docutils.statemachine import ViewList
from sphinx.util.docutils import SphinxDirective

class PldmPdrTableDirective(SphinxDirective):
    required_arguments = 2  # YAML file path, JSON schema file path
    has_content = False

    def run(self):
        env = self.state.document.settings.env
        
        # 1. Resolve absolute paths
        _, yaml_abs_path = env.relfn2path(self.arguments[0])
        _, schema_abs_path = env.relfn2path(self.arguments[1])

        # 2. Register dependencies (rebuild if these change)
        env.note_dependency(yaml_abs_path)
        env.note_dependency(schema_abs_path)

        # 3. Load files
        try:
            with open(yaml_abs_path, 'r') as f:
                raw_data = yaml.safe_load(f)
            with open(schema_abs_path, 'r') as f:
                schema = json.load(f)
        except Exception as e:
            raise self.error(f"Failed to load files: {e}")

        # 4. CLEAN DATA FOR VALIDATION
        # Recursively strip 'value'/'comment' wrappers to get raw values for schema validation
        def clean_for_validation(node):
            if isinstance(node, dict):
                if 'value' in node:
                    return clean_for_validation(node['value'])
                return {k: clean_for_validation(v) for k, v in node.items()}
            elif isinstance(node, list):
                return [clean_for_validation(i) for i in node]
            else:
                return node

        validation_data = clean_for_validation(raw_data)

        # 5. VALIDATE
        try:
            validate(instance=validation_data, schema=schema)
        except ValidationError as e:
            error_path = " -> ".join([str(p) for p in e.path])
            raise self.error(f"Schema Validation Failed at '{error_path}': {e.message}")

        # 6. FLATTEN RAW DATA FOR TABLE
        # We use raw_data here because we want the comments and explicit types
        rows = []
        def flatten(data, parent_key='', schema=schema):
            if isinstance(data, dict):
                # Check if this is a wrapper node (has 'value')
                if 'value' in data:
                    val = data['value']
                    comment = data.get('comment', '')
                    
                    # Determine Type
                    if 'type' in data:
                        field_type = data['type'] # Use YAML explicit type
                    else:
                        # Try to guess from schema (basic lookup)
                        key_part = parent_key.split('.')[-1] if parent_key else ''
                        key_schema = schema.get('properties', {}).get(key_part, {})
                        field_type = key_schema.get('type', 'unknown')

                    rows.append([field_type, parent_key, str(val), comment])
                else:
                    # Container node, recurse
                    for key, value in data.items():
                        full_key = f"{parent_key}.{key}" if parent_key else key
                        # Attempt to pass down subschema (simplified)
                        subschema = schema.get('properties', {}).get(key, {})
                        flatten(value, full_key, subschema)
            elif isinstance(data, list):
                for i, item in enumerate(data):
                    full_key = f"{parent_key}[{i}]"
                    flatten(item, full_key)

        flatten(raw_data)

        if not rows:
            raise self.error("No data found to generate table.")

        # 7. BUILD THE RST TABLE
        table = nodes.table()
        tgroup = nodes.tgroup(cols=4)
        for _ in range(4):
            tgroup += nodes.colspec(colwidth=1)
        table += tgroup

        # Header
        thead = nodes.thead()
        row = nodes.row()
        for header in ['Type', 'Field Name', 'Value', 'Comment']:
            entry = nodes.entry()
            entry += nodes.paragraph(text=header)
            row += entry
        thead += row
        tgroup += thead

        # Body
        tbody = nodes.tbody()
        for row_data in rows:
            row = nodes.row()
            for i, cell in enumerate(row_data):
                entry = nodes.entry()
                
                # Special handling for Comment Column (index 3)
                if i == 3 and cell:
                    # --- FIX IS HERE ---
                    # Create a ViewList to hold the RST lines
                    rst_content = ViewList()
                    
                    # Split the comment into lines
                    for line in str(cell).splitlines():
                        # append(line, source_file_path) 
                        # This ensures errors in comments point to the YAML file!
                        rst_content.append(line, yaml_abs_path)
                    
                    # Now nested_parse accepts the ViewList
                    self.state.nested_parse(rst_content, 0, entry)
                else:
                    entry += nodes.paragraph(text=cell)
                row += entry
            tbody += row
        tgroup += tbody

        return [table]

def setup(app):
    app.add_directive('pldm-pdr-table', PldmPdrTableDirective)
    return {'version': '0.3', 'parallel_read_safe': True}
