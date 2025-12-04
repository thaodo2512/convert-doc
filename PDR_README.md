# PLDM PDR Generation & Reverse-Decode Guide

This repo provides two utilities to keep PLDM Platform Descriptor Records (PDRs) schema-driven and code-generated:

- `generate_pdr_repo.py`: packs PDR YAMLs into a C header/implementation (binary repo + offsets + macros).
- `pdr_repo_to_yaml.py`: reconstructs YAML PDRs from a generated C array or raw binary blob.

The YAML inputs in `source/data/` no longer carry `type` keys; all typing, ordering, and widths come from the PLDM JSON schemas in `source/schema/` (DSP0248 v1.3.0 layouts).

## Generate PDR repository (header + C file)

```bash
# From repo root
python3 generate_pdr_repo.py \
  --pdr-dir source/data \
  --schema-dir source/schema \
  --macro-defs macro_defs.yaml \
  --out build/pdr_repo.h \
  --c-out build/pdr_repo.c
```

Outputs:
- `build/pdr_repo.h`: `extern` declarations for `pdr_repository[]`, `pdr_offsets[]`, and macros from `macro_defs.yaml` (handles, repo offsets, field offsets).
- `build/pdr_repo.c`: definitions for `pdr_repository[]` (contiguous binary PDRs, header+body) and `pdr_offsets[]` (handle->offset table).

Notes:
- Packing is little-endian per DSP0248 Clause 28.1. `dataLength` is always computed from the packed body size (header is 10 bytes); stale YAML values are corrected automatically.
- `recordHandle` can be omitted or set to `auto` to let the generator assign the next free handle; duplicates are auto-renumbered upward with a notice, skipping any user-reserved handles.
- Strings: UTF-8/ASCII and UTF-16BE are supported; numeric widths/ranges come from schema `binaryFormat` or bounds.

Doc-only hiding:
- Add `docHidden: true` or `_doc: { hidden: true }` to any field/object in YAML to omit it from the Sphinx tables while keeping it in the generated binary/C output. See `source/data/type_15_doc_hidden.yaml` for an example.

## Reconstruct YAMLs from generated C/binary

```bash
# From repo root
python3 pdr_repo_to_yaml.py \
  --schema-dir source/schema \
  --in-c build/pdr_repo.c \
  --out-dir build/reconstructed
```

Options:
- Use `--in-bin <blob>` instead of `--in-c` to decode a raw repository blob.
- Add `--include-type` if you want `type` keys in the reconstructed YAML (default: omit).

Heuristics:
- Variable-length arrays use length/count fields (`*Size`, `*Length*`, `*Count`) when present. If length is unknown, remaining bytes are emitted as a byte array.
- UTF-16BE strings are decoded with `utf-16-be`; other strings default to UTF-8.

## Zephyr/CMake integration (example)

```cmake
set(PDR_GEN ${CMAKE_CURRENT_SOURCE_DIR}/generate_pdr_repo.py)
set(PDR_DIR ${CMAKE_CURRENT_SOURCE_DIR}/source/data)
set(PDR_SCHEMA ${CMAKE_CURRENT_SOURCE_DIR}/source/schema)
set(PDR_MACROS ${CMAKE_CURRENT_SOURCE_DIR}/macro_defs.yaml)
set(PDR_OUT_H ${CMAKE_CURRENT_BINARY_DIR}/generated/pdr_repo.h)
set(PDR_OUT_C ${CMAKE_CURRENT_BINARY_DIR}/generated/pdr_repo.c)

add_custom_command(
  OUTPUT ${PDR_OUT_H} ${PDR_OUT_C}
  COMMAND ${Python3_EXECUTABLE} ${PDR_GEN}
          --pdr-dir ${PDR_DIR}
          --schema-dir ${PDR_SCHEMA}
          --macro-defs ${PDR_MACROS}
          --out ${PDR_OUT_H}
          --c-out ${PDR_OUT_C}
  DEPENDS ${PDR_GEN} ${PDR_DIR} ${PDR_SCHEMA} ${PDR_MACROS}
  COMMENT "Generating PLDM PDR repository"
)

add_custom_target(gen_pdr DEPENDS ${PDR_OUT_H} ${PDR_OUT_C})
add_dependencies(app gen_pdr)
target_sources(app PRIVATE ${PDR_OUT_C})
target_include_directories(app PRIVATE ${CMAKE_CURRENT_BINARY_DIR}/generated)
```

## Quick usage in C

```c
#include "pdr_repo.h"
#include <string.h>

const uint8_t *pdr_by_handle(uint16_t handle, uint16_t *len_out) {
    for (size_t i = 0; i < PDR_COUNT; ++i) {
        if (pdr_offsets[i].handle == handle) {
            uint32_t off = pdr_offsets[i].offset;
            uint16_t data_len = (uint16_t)(pdr_repository[off + 8] | (pdr_repository[off + 9] << 8));
            *len_out = (uint16_t)(data_len + 10); // header (10) + body
            return &pdr_repository[off];
        }
    }
    return NULL;
}

Handle and length auto-fill (generator details):
- `recordHandle` is encoded as uint32 in the C output. If missing or set to `auto`/`auto-gen`, the generator assigns the next unused handle (respecting any handles explicitly present in other YAMLs and renumbering duplicates with a warning). The assigned handle is what appears in `pdr_offsets[]` and the C macros, so downstream C code always sees a consistent mapping.
- `dataLength` is encoded as uint16 and always recomputed from the packed body, so header bytes and `pdr_repository[]` stay consistent even if YAML contained a placeholder.
```

## Troubleshooting
- “Unsupported type …”: ensure the schema defines `binaryFormat` or ranges; extend `TYPE_FMT`/`FMT_CHAR_TO_TYPE` in the generator if you introduce new formats.
- Duplicate handles: generator will renumber later duplicates; adjust YAMLs if a stable handle map is required.
- Reverse decode ambiguity: variable-length fields without explicit length/count may fall back to raw byte arrays; add lengths to the original YAML/schema to improve fidelity.
