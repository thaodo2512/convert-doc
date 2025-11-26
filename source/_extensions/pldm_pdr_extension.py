import yaml
import json
from jsonschema import validate, ValidationError
from docutils import nodes
from docutils.parsers.rst import Directive
from sphinx.util.docutils import SphinxDirective

class PldmPdrTableDirective(SphinxDirective):
    required_arguments = 2  # YAML file path, JSON schema file path
    has_content = False

    def run(self):
        yaml_path = self.arguments[0]
        schema_path = self.arguments[1]

        # Load YAML data
        try:
            with open(yaml_path, 'r') as f:
                data = yaml.safe_load(f)
        except Exception as e:
            raise self.error(f"Failed to load YAML file '{yaml_path}': {e}")

        # Load JSON schema
        try:
            with open(schema_path, 'r') as f:
                schema = json.load(f)
        except Exception as e:
            raise self.error(f"Failed to load JSON schema '{schema_path}': {e}")

        # Validate YAML against schema
        try:
            validate(instance=data, schema=schema)
        except ValidationError as e:
            raise self.error(f"Validation failed for '{yaml_path}' against '{schema_path}': {e}")

        # Flatten the data into table rows
        rows = []
        def flatten(data, parent_key='', schema=schema):
            if isinstance(data, dict):
                for key, value in data.items():
                    full_key = f"{parent_key}.{key}" if parent_key else key
                    # Get subschema for this key
                    key_schema = schema.get('properties', {}).get(key, {})
                    # Get type: handle anyOf specially
                    if 'anyOf' in key_schema:
                        types = [sub.get('description', sub.get('type', 'unknown')) for sub in key_schema['anyOf']]
                        field_type = ' | '.join(types)
                    else:
                        field_type = key_schema.get('type', 'unknown')
                        if isinstance(field_type, list):
                            field_type = '/'.join(field_type)
                    # Get fallback description
                    fallback_comment = key_schema.get('description', '')
                    # Handle sub-dict structure
                    if isinstance(value, dict) and 'value' in value:
                        comment = value.get('comment', fallback_comment)  # Fallback if missing/empty
                        if comment is None or comment == '':
                            comment = fallback_comment
                        rows.append([field_type, full_key, str(value['value']), comment])
                    # Handle direct scalar values (no sub-dict)
                    elif not isinstance(value, (dict, list)):
                        rows.append([field_type, full_key, str(value), fallback_comment])
                    else:
                        # Recurse for nested dicts/lists
                        subschema = schema.get('properties', {}).get(key, {})
                        flatten(value, full_key, subschema)
            elif isinstance(data, list):
                for i, item in enumerate(data):
                    full_key = f"{parent_key}[{i}]"
                    # Assume array items have a schema under 'items'
                    subschema = schema.get('items', {})
                    flatten(item, full_key, subschema)

        flatten(data)

        if not rows:
            raise self.error("No data found to generate table.")

        # Generate RST table
        table = nodes.table()
        tgroup = nodes.tgroup(cols=4)
        for _ in range(4):
            tgroup += nodes.colspec(colwidth=1)
        table += tgroup

        # Header row
        thead = nodes.thead()
        row = nodes.row()
        for header in ['Type', 'Field Name', 'Value', 'Comment']:
            entry = nodes.entry()
            entry += nodes.paragraph(text=header)
            row += entry
        thead += row
        tgroup += thead

        # Body rows
        tbody = nodes.tbody()
        for row_data in rows:
            row = nodes.row()
            for i, cell in enumerate(row_data):
                entry = nodes.entry()
                if i == 3:  # Comment column (index 3)
                    # Parse comment as RST (handles long text, links, directives)
                    self.state.nested_parse(cell.splitlines(), 0, entry)
                else:
                    # Other columns as plain text
                    entry += nodes.paragraph(text=cell)
                row += entry
            tbody += row
        tgroup += tbody

        return [table]

def setup(app):
    app.add_directive('pldm-pdr-table', PldmPdrTableDirective)
    return {'version': '0.1', 'parallel_read_safe': True}
