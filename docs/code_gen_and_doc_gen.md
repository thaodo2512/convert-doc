# Code Generation & Documentation Generation

This document describes the two generation pipelines in the PLDM PDR project: **code generation** (`code_gen.py` — YAML to C) and **documentation generation** (Sphinx + `pldm_pdr_extension.py` — YAML to HTML tables). Both pipelines share the same YAML data files and JSON schemas but serve different purposes.

## Shared Inputs

Both pipelines consume the same source files:

```
source/
  data/                        # YAML PDR definitions (one record per file)
    terminus.yaml              # Type 1 — Terminus Locator
    type_2.yaml ... type_127.yaml
    macro_defs.yaml            # C macro bindings (code gen only)
  schema/                      # JSON schemas (validation + binary layout)
    type_1.json ... type_127.json
```

### YAML Data Files (`source/data/*.yaml`)

Each file defines one PDR record using the **value/comment pattern**:

```yaml
sensorID:
  value: 256
  comment: "Primary temperature sensor"
```

- `value` — the actual data (used by both pipelines)
- `comment` — documentation text (used by doc gen, ignored by code gen)
- `type` — optional binary format override for multiple-type fields (used by both pipelines)

Files must have a `pdrHeader` key to be recognized as PDR files. Files without it (e.g., `macro_defs.yaml`) are skipped by both pipelines.

### JSON Schemas (`source/schema/type_N.json`)

Each schema defines both **validation constraints** and **binary serialization rules** via custom extensions:

| Extension | Purpose |
|-----------|---------|
| `binaryOrder` | Array specifying field serialization order |
| `binaryFormat` | Python `struct` pack code (`B`, `H`, `I`, `f`, etc.) |
| `x-binary-type-field` | Field name that controls this field's binary type |
| `x-binary-type-mapping` | Maps the controlling field's value to a pack format |
| `formatResolver` | Alternative dependency-based format resolution |
| `x-binary-encoding` | String encoding (`utf-8`, `utf-16be`, `us-ascii`) |
| `x-binary-terminator` | String null-terminator (`0x00`, `0x0000`) |
| `oneOf` | Polymorphic types selected by `const` discriminator |

---

## Pipeline 1: Code Generation (`code_gen.py`)

Converts YAML PDR definitions into C source files with pre-packed binary blobs.

### Overview

```
┌───────────────────┐
│  YAML Data        │
│  source/data/     │──┐
└───────────────────┘  │
┌───────────────────┐  │    ┌──────────────┐    ┌──────────────────┐
│  JSON Schemas     │──┼───>│ code_gen.py  │───>│ pdr_generated.h  │
│  source/schema/   │  │    │ validate     │    │ pdr_generated.c  │
└───────────────────┘  │    │ pack → emit  │    └──────────────────┘
┌───────────────────┐  │    └──────────────┘
│  Macro Defs       │──┘
│  macro_defs.yaml  │
└───────────────────┘
```

### Command

```bash
python3 source/code_gen.py source/data source/schema output/pdr_generated.c \
    --macros source/data/macro_defs.yaml
```

Produces both `output/pdr_generated.c` and `output/pdr_generated.h`.

### Execution Flow

1. **`discover_yaml_files()`** — Recursively scans `source/data/` for `.yaml/.yml` files, filters to those containing `pdrHeader`, sorts parent-dir-first then alphabetically.

2. **`collect_reserved_handles()`** — First pass: collects explicitly set `recordHandle` values to avoid collisions during auto-assignment.

3. **`process_single_yaml()`** — For each PDR file:
   - Reads YAML data, extracts `pdrHeader.PDRType`
   - Loads `source/schema/type_N.json`
   - Runs `clean_for_validation()` to strip `value`/`comment` wrappers
   - Validates cleaned data against schema using `jsonschema`
   - Assigns or confirms record handle via `assign_handle()`
   - Packs 10-byte common header (`recordHandle` + `version` + `type` + `changeNum` + `dataLength`)
   - Packs body fields recursively via `pack_field()`
   - Auto-computes `dataLength` from body size

4. **Sort** all records by handle.

5. **Emit `.h`** — Include guard, bound macros, blob capacity/size defines, PDR statistics, per-record handle/offset/size macros, X-macro type list, function prototypes.

6. **Emit `.c`** — Static blob arrays (`pdr_blob_data`, `pdr_blob_backup`), `pdr_repo_populate_ext()` (zero-copy init), `pdr_repo_populate()` (rebuild callback).

### `pack_field()` — Recursive Binary Packer

Handles all field types:

| Schema Type | Packing Behavior |
|-------------|-----------------|
| Integer / Float / Bool | `struct.pack('<format', value)` |
| Object | Recurse sub-fields in `binaryOrder` |
| Array | Pack each item using item schema |
| String | Encode per `x-binary-encoding` with optional null-terminator |
| Variable-length | Hex string or byte list |
| `oneOf` | Select variant by matching `const` discriminator field |

### Multiple-Type Fields

Some fields have variable binary width determined by another field's value. For example, in Type 2 (Numeric Sensor), `minReadable`'s format depends on `sensorDataSize`:

```json
"minReadable": {
  "binaryFormat": "variable",
  "x-binary-type-field": "sensorDataSize",
  "x-binary-type-mapping": {
    "0": "B",  "1": "b",  "2": "H",
    "3": "h",  "4": "I",  "5": "i",  "6": "f"
  }
}
```

When `sensorDataSize = 1`, `minReadable` is packed as `sint8` (format `b`).

#### YAML `type` Override

YAML data files can explicitly specify the binary type for multiple-type fields:

```yaml
minReadable:
  value: 0
  type: sint8
```

The `type` field maps to a struct format code:

| YAML `type` | Format | Size |
|-------------|--------|------|
| `uint8` / `sint8` | `B` / `b` | 1 byte |
| `uint16` / `sint16` | `H` / `h` | 2 bytes |
| `uint32` / `sint32` | `I` / `i` | 4 bytes |
| `uint64` / `sint64` | `Q` / `q` | 8 bytes |

#### Dependency Validation

`pack_field()` validates the YAML `type` override against the schema's dependency-resolved format. If they conflict, the build fails:

```
ValueError: Type mismatch for minReadable: YAML declares 'type: uint32'
but schema dependency resolves to 'sint8'
```

This catches data entry errors where the YAML `type` contradicts the controlling field's value (e.g., `sensorDataSize = 1` implies `sint8`, not `uint32`).

### Generated Output

#### `pdr_generated.h`

```c
#define SENSOR_INIT_SENSOR_ID   256      // From macro_defs.yaml
#define PDR_BLOB_CAPACITY       1480     // Blob size + 25% headroom
#define PDR_BLOB_DATA_SIZE      1184     // Actual packed data size
#define PDR_COUNT               30       // Total PDR records
#define PDR_HANDLE_TYPE_2       2        // Per-record handle
#define PDR_OFFSET_TYPE_2       70       // Per-record blob offset
#define PDR_SIZE_TYPE_2         69       // Per-record total size
#define PDR_TYPE_LIST \                  // X-macro for iteration
    PDR_TYPE_ENTRY(1, 2) \
    PDR_TYPE_ENTRY(2, 1) ...
```

#### `pdr_generated.c`

Contains two blob arrays:

| Array | Mutability | Content | Purpose |
|-------|-----------|---------|---------|
| `pdr_blob_data[]` | Mutable | Full records (header + body) + headroom | Primary runtime storage |
| `pdr_blob_backup[]` | `const` | Body only, compact | Rebuild source for `RunInitAgent` |

And two populate functions:

- **`pdr_repo_populate_ext()`** — Zero-copy fast init: indexes pre-packed records in the blob in-place
- **`pdr_repo_populate()`** — Rebuild callback: re-adds all records from backup (used after compaction)

---

## Pipeline 2: Documentation Generation (Sphinx)

Converts YAML PDR definitions into HTML documentation tables using a custom Sphinx directive.

### Overview

```
┌───────────────────┐
│  YAML Data        │
│  source/data/     │──┐
└───────────────────┘  │    ┌─────────────────────────┐    ┌──────────────┐
                       ├───>│ pldm_pdr_extension.py   │───>│ HTML tables   │
┌───────────────────┐  │    │ (Sphinx directive)       │    │ (in docs)     │
│  JSON Schemas     │──┘    │ validate + flatten       │    └──────────────┘
│  source/schema/   │       └─────────────────────────┘
└───────────────────┘
```

### Components

#### Sphinx Configuration (`source/conf.py`)

```python
extensions = ['pldm_pdr_extension']
```

Registers the custom extension from `source/_extensions/`.

#### Custom Directive (`source/_extensions/pldm_pdr_extension.py`)

Provides the `pldm-pdr-table` RST directive that generates a 4-column table (Type, Field Name, Value, Comment) from a YAML + schema pair.

#### Usage in RST (`source/index.rst`)

```rst
.. pldm-pdr-table:: data/type_2.yaml schema/type_2.json
```

Each directive instance renders one PDR record as a formatted table. Options:
- `:caption:` — Table caption for numbering
- `:name:` — Label for cross-referencing

### Build Command

```bash
make html
# or: sphinx-build -b html source build
```

### Execution Flow

For each `pldm-pdr-table` directive encountered during the Sphinx build:

1. **Resolve paths** — Converts relative YAML/schema paths to absolute paths, registers them as Sphinx dependencies (triggers rebuild on change).

2. **Load data** — Reads YAML data file and JSON schema.

3. **Clean data** — Strips `value`/`comment` wrappers via `clean_for_validation()` for schema validation. Same logic as `code_gen.py`.

4. **Validate** — Runs `jsonschema.validate()` against the schema. On failure, reports the error path and message as a Sphinx error.

5. **Flatten data** — Recursively walks the YAML structure and produces a flat list of table rows. For each leaf node (`value` present):
   - Extracts `value` and `comment`
   - Validates value range against `binaryFormat` (and YAML `type` override if present)
   - Infers the display type from schema or YAML `type` field
   - Applies hidden field filtering (`_doc: {hidden: true}`)

6. **Build table** — Constructs a docutils table node with 4 columns:

   | Column | Source |
   |--------|--------|
   | Type | Inferred from schema `binaryFormat` or YAML `type` |
   | Field Name | YAML key name |
   | Value | `data['value']` |
   | Comment | `data['comment']` (parsed as RST for inline markup) |

7. **Range validation** — After flattening, if any values are out of range for their binary format, the build fails with a detailed error listing all violations.

### Type Inference

When the YAML doesn't specify an explicit `type`, the extension infers the display type from the schema:

| Schema `binaryFormat` | Inferred Display Type |
|-----------------------|----------------------|
| `B` | `uint8` |
| `H` | `uint16` |
| `I` | `uint32` |
| `b` | `sint8` |
| `h` | `sint16` |
| `f` | `real32` |
| `variable` | `variable` (or from YAML `type`) |
| (with `enum` in schema) | `enum8`, `enum16`, etc. |
| (with `bitfield` in description) | `bitfield8`, `bitfield16`, etc. |

### Validation

The doc gen performs the same validations as code gen, plus additional checks:

| Check | Code Gen | Doc Gen |
|-------|----------|---------|
| Schema validation (`jsonschema`) | Yes | Yes |
| Value range vs `binaryFormat` | Yes (via `struct.pack` error) | Yes (explicit check) |
| YAML `type` override range | Yes | Yes |
| YAML `type` vs dependency mismatch | Yes (raises `ValueError`) | Yes (reported as warning) |
| Hidden field filtering | No (packs everything) | Yes (hides from table) |

---

## Key Differences Between Pipelines

| Aspect | Code Gen (`code_gen.py`) | Doc Gen (Sphinx extension) |
|--------|-------------------------|---------------------------|
| **Purpose** | Produce C source with packed binary blobs | Produce HTML documentation tables |
| **Output** | `pdr_generated.c` + `.h` | HTML pages with per-PDR tables |
| **Processes all files at once** | Yes (discovers and sorts) | No (one directive per file in RST) |
| **Binary packing** | Yes (`struct.pack`) | No (display only) |
| **Comment field** | Ignored | Rendered as RST in table |
| **Hidden fields** | Still packed in binary | Excluded from table |
| **Macro bindings** | Yes (`macro_defs.yaml`) | No |
| **Auto handle assignment** | Yes | No (display as-is) |
| **`dataLength` auto-calc** | Yes | No |
| **Error behavior** | Exits with error | Sphinx build error |

---

## Shared Code Patterns

Both pipelines implement the same core patterns independently (not shared code):

### `clean_for_validation()`

Strips the `value`/`comment` wrapper from YAML data, producing clean data for `jsonschema` validation:

```python
# Input:  {"sensorID": {"value": 256, "comment": "..."}}
# Output: {"sensorID": 256}
```

**Important:** This function discards the YAML `type` field. For code gen, the `type` override is extracted separately from `raw_data` before cleaning.

### `resolve_subschema()`

Handles conditional schemas (`allOf` with `if`/`then`). When a field's schema depends on another field's value (via `const` matching), this function resolves the correct sub-schema.

### `is_hidden()`

Checks for `_doc: {hidden: true}` metadata. Used by doc gen to exclude entries from tables. Code gen ignores this — hidden data is always packed.

---

## Multiple-Type Field Validation Flow

Both pipelines validate YAML `type` overrides against schema dependencies using the same logic:

```
1. Read YAML field:  {value: 0, type: sint8}

2. Resolve dependency from schema:
   x-binary-type-field: "sensorDataSize"
   sensorDataSize = 1  →  x-binary-type-mapping["1"] = "b"  →  sint8

3. Compare:
   YAML type override:     sint8  →  format "b"
   Schema dependency:      sint8  →  format "b"
   Result: Match ✓

4. If mismatch (e.g., type: uint32 vs dependency sint8):
   Code gen:  ValueError (build fails)
   Doc gen:   Sphinx error (build fails)
```

---

## Adding a New PDR Type

When adding a new PDR type, both pipelines are affected:

1. **Create the JSON schema** — `source/schema/type_N.json` with `binaryOrder`, `binaryFormat`, and validation constraints.

2. **Create the YAML data file** — `source/data/type_N.yaml` with `pdrHeader.PDRType.value: N` and all fields from `binaryOrder`.

3. **Run code gen** — Verify the new record appears in the generated output:
   ```bash
   python3 source/code_gen.py source/data source/schema output/pdr_generated.c \
       --macros source/data/macro_defs.yaml
   ```

4. **Add the Sphinx directive** — Add to `source/index.rst`:
   ```rst
   .. pldm-pdr-table:: data/type_N.yaml schema/type_N.json
   ```

5. **Build docs** — Verify the table renders correctly:
   ```bash
   make html
   ```

6. **(Optional) Add macro bindings** — Edit `source/data/macro_defs.yaml` to expose field values as C `#define` constants.

---

## Zephyr RTOS Integration

This section describes how to integrate the code generation pipeline into a Zephyr application so that PDR blobs are generated at build time and linked into the firmware image.

### Prerequisites

- Zephyr SDK with west and CMake
- Python 3 with `pyyaml` and `jsonschema` (available in the build environment)

### Project Layout

Place the PDR source files alongside your Zephyr application:

```
my_zephyr_app/
  CMakeLists.txt
  prj.conf
  src/
    main.c                       # Application entry point
    pldm_pdr_repo.c              # PDR repository implementation
    pldm_pdr_repo.h              # PDR repository header
  pdr/
    code_gen.py                  # Code generator script
    data/                        # YAML PDR definitions
      terminus.yaml
      type_2.yaml
      macro_defs.yaml
    schema/                      # JSON schemas
      type_1.json
      type_2.json
```

### CMakeLists.txt

Add a custom command that runs `code_gen.py` at build time, before compilation:

```cmake
cmake_minimum_required(VERSION 3.20.0)
find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})
project(my_pldm_app)

# --- PDR Code Generation ---
find_package(Python3 REQUIRED COMPONENTS Interpreter)

set(PDR_GEN     ${CMAKE_CURRENT_SOURCE_DIR}/pdr/code_gen.py)
set(PDR_DIR     ${CMAKE_CURRENT_SOURCE_DIR}/pdr/data)
set(PDR_SCHEMA  ${CMAKE_CURRENT_SOURCE_DIR}/pdr/schema)
set(PDR_MACROS  ${CMAKE_CURRENT_SOURCE_DIR}/pdr/data/macro_defs.yaml)
set(PDR_OUT_C   ${CMAKE_CURRENT_BINARY_DIR}/generated/pdr_generated.c)
set(PDR_OUT_H   ${CMAKE_CURRENT_BINARY_DIR}/generated/pdr_generated.h)

# Collect all YAML and JSON files as dependencies for rebuild tracking
file(GLOB PDR_YAML_FILES ${PDR_DIR}/*.yaml ${PDR_DIR}/**/*.yaml)
file(GLOB PDR_SCHEMA_FILES ${PDR_SCHEMA}/*.json)

add_custom_command(
  OUTPUT ${PDR_OUT_C} ${PDR_OUT_H}
  COMMAND ${Python3_EXECUTABLE} ${PDR_GEN}
          ${PDR_DIR} ${PDR_SCHEMA} ${PDR_OUT_C}
          --macros ${PDR_MACROS}
  DEPENDS ${PDR_GEN} ${PDR_YAML_FILES} ${PDR_SCHEMA_FILES} ${PDR_MACROS}
  COMMENT "Generating PLDM PDR repository from YAML"
)

add_custom_target(gen_pdr DEPENDS ${PDR_OUT_C} ${PDR_OUT_H})

# --- Application ---
target_sources(app PRIVATE
  src/main.c
  src/pldm_pdr_repo.c
  ${PDR_OUT_C}
)

target_include_directories(app PRIVATE
  src/
  ${CMAKE_CURRENT_BINARY_DIR}/generated   # For pdr_generated.h
)

add_dependencies(app gen_pdr)
```

**Key points:**
- `add_custom_command` runs `code_gen.py` only when YAML/schema files change
- Both `.c` and `.h` are generated into `build/generated/`
- `DEPENDS` lists all input files so CMake rebuilds when any YAML or schema changes
- `add_dependencies(app gen_pdr)` ensures generation runs before compilation

### Kconfig (Optional)

Add a Kconfig option to enable/disable PDR generation:

```kconfig
# Kconfig
config PLDM_PDR_GENERATED
    bool "Include generated PLDM PDR repository"
    default y
    help
      Enable code-generated PDR repository from YAML definitions.
      Requires Python 3 with pyyaml and jsonschema at build time.

config PLDM_PDR_MAX_RECORDS
    int "Maximum number of PDR records"
    default 64
    help
      Maximum number of PDR records the repository can hold.
      Must be >= the number of generated PDRs.
```

Then guard the CMake generation:

```cmake
if(CONFIG_PLDM_PDR_GENERATED)
  # ... add_custom_command block above ...
endif()
```

### Application Code (main.c)

```c
#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include "pldm_pdr_repo.h"
#include "pdr_generated.h"

LOG_MODULE_REGISTER(pldm_pdr, LOG_LEVEL_INF);

static pdr_repo_t pdr_repo;

void pdr_init(void)
{
    /* Zero-copy init: index pre-packed blob in-place.
     * This is the fastest option — no memcpy, no parsing.
     * The blob is already in the .data section from pdr_generated.c. */
    pdr_repo_populate_ext(&pdr_repo, NULL);

    const pdr_repo_info_t *info = pdr_repo_get_info(&pdr_repo);
    LOG_INF("PDR repository initialized: %u records, %u bytes",
            info->record_count, info->repository_size);
}

int pdr_handle_get_pdr(uint32_t record_handle, uint32_t data_transfer_handle,
                       uint32_t *next_record, uint32_t *next_data,
                       uint8_t *transfer_flag,
                       const uint8_t **data, uint16_t *data_len)
{
    return pdr_repo_get_pdr(&pdr_repo, record_handle, data_transfer_handle,
                            next_record, next_data, transfer_flag,
                            data, data_len);
}

int pdr_handle_find_pdr(uint8_t pdr_type, uint32_t start_handle,
                        uint32_t *found_handle, uint32_t *next_handle,
                        const uint8_t **data, uint16_t *data_len)
{
    return pdr_repo_find_pdr(&pdr_repo, pdr_type, start_handle,
                             found_handle, next_handle, data, data_len);
}

int main(void)
{
    pdr_init();

    /* Access generated macros at compile time */
    LOG_INF("Total PDRs: %d, Blob size: %d bytes",
            PDR_COUNT, PDR_BLOB_DATA_SIZE);

    /* Application main loop ... */
    return 0;
}
```

### Build and Flash

```bash
# Build
west build -b <your_board> my_zephyr_app

# Flash
west flash
```

The build output will show:

```
[  1%] Generating PLDM PDR repository from YAML
Generated pdr_generated.c and pdr_generated.h with 30 PDRs (blob=1184B)
...
```

### Memory Considerations

The generated PDR data resides in two sections:

| Array | Section | Mutability | Typical Size |
|-------|---------|-----------|-------------|
| `pdr_blob_data[]` | `.data` (RAM) | Mutable | `PDR_BLOB_CAPACITY` (data + 25% headroom) |
| `pdr_blob_backup[]` | `.rodata` (Flash) | `const` | `PDR_BLOB_DATA_SIZE` (body only, compact) |

For memory-constrained targets:

- **Reduce headroom** — The generator adds 25% headroom to `pdr_blob_data[]` for runtime record additions. If you don't add records at runtime, you can modify `code_gen.py` to reduce or eliminate headroom.
- **Use `populate_ext()` only** — If you don't need `RunInitAgent` rebuild, the backup blob can be removed to save flash.
- **Tune `PDR_MAX_RECORD_COUNT`** — Each index entry is 12 bytes. Setting this to your actual PDR count saves RAM.

### Runtime Record Management

The repository supports runtime modifications on Zephyr:

```c
/* Add a record at runtime (fits in headroom) */
uint8_t sensor_body[] = { /* ... packed PDR body ... */ };
uint32_t new_handle;
int rc = pdr_repo_add_record(&pdr_repo, PDR_TYPE_NUMERIC_SENSOR,
                              sensor_body, sizeof(sensor_body),
                              &new_handle);

/* Remove a record (tombstone, O(1)) */
pdr_repo_remove_record(&pdr_repo, old_handle);

/* Rebuild (compacts tombstones, restores from backup) */
pdr_repo_run_init_agent(&pdr_repo, pdr_repo_populate, NULL);
```

### CI Integration

Add a build check to your CI pipeline to catch YAML/schema errors early:

```yaml
# .github/workflows/build.yml (excerpt)
- name: Validate PDR generation
  run: |
    pip install pyyaml jsonschema
    python3 pdr/code_gen.py pdr/data pdr/schema /tmp/pdr_generated.c \
        --macros pdr/data/macro_defs.yaml
```

This runs the generator without building the full Zephyr image, catching validation errors (schema mismatches, value out of range, type dependency conflicts) before the firmware build.
