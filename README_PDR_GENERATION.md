# PLDM PDR Header Generation (Zephyr-focused)

This prototype keeps Platform Descriptor Records (PDRs) configurable via YAML files in a directory (e.g., `source/data`), while the C side stays auto-generated for zero runtime parsing. It follows DSP0248 v1.3.0, Clause 28.x layouts and the repository model in Clause 8.

## Inputs
- `source/data/*.yaml` – PDR inputs (one YAML per PDR, already present in this repo).
- `source/schema/*.json` – PLDM schemas with `binaryOrder` and types (used for packing and offsets).
- `macro_defs.yaml` – macro names for handles, repository offsets, and field-relative offsets.

## Generator
Generate a header directly from the PDR directory:

```bash
python3 generate_pdr_repo.py \
  --pdr-dir source/data \
  --schema-dir source/schema \
  --macro-defs macro_defs.yaml \
  --out build/pdr_repo.h
```

What it emits:
- `pdr_repository[]`: contiguous PDR blobs (header+body) packed little-endian, PDR header per Clause 28.1.
- `pdr_offsets[]`: `{handle, offset}` pairs for O(1) lookup by handle.
- `PDR_HANDLE_*`, `PDR_REPO_OFFSET_*`, and `PDR_FIELD_*` macros from `macro_defs.yaml`.

Supported PDRs are derived from the YAMLs in `source/data` (Types 1..127). Schema binary formats drive packing, so any PDR with a JSON schema in `source/schema` can be emitted.

## Zephyr/CMake integration
```cmake
set(PDR_GEN ${CMAKE_CURRENT_SOURCE_DIR}/generate_pdr_repo.py)
set(PDR_MACROS ${CMAKE_CURRENT_SOURCE_DIR}/macro_defs.yaml)
set(PDR_DIR ${CMAKE_CURRENT_SOURCE_DIR}/source/data)
set(PDR_SCHEMA ${CMAKE_CURRENT_SOURCE_DIR}/source/schema)
set(PDR_OUT ${CMAKE_CURRENT_BINARY_DIR}/generated/pdr_repo.h)

add_custom_command(
  OUTPUT ${PDR_OUT}
  COMMAND ${Python3_EXECUTABLE} ${PDR_GEN}
          --pdr-dir ${PDR_DIR}
          --schema-dir ${PDR_SCHEMA}
          --macro-defs ${PDR_MACROS}
          --out ${PDR_OUT}
  DEPENDS ${PDR_GEN} ${PDR_DIR} ${PDR_SCHEMA} ${PDR_MACROS}
  COMMENT "Generating PLDM PDR repository"
)

add_custom_target(gen_pdr DEPENDS ${PDR_OUT})
add_dependencies(app gen_pdr)
target_include_directories(app PRIVATE ${CMAKE_CURRENT_BINARY_DIR}/generated)
```

## C usage example
```c
#include "pdr_repo.h"
#include <string.h>

static const uint8_t *pdr_by_handle(uint16_t handle, uint16_t *len_out)
{
    for (size_t i = 0; i < PDR_COUNT; ++i) {
        if (pdr_offsets[i].handle == handle) {
            uint32_t off = pdr_offsets[i].offset;
            uint16_t data_len = (uint16_t)(pdr_repository[off + 8] | (pdr_repository[off + 9] << 8));
            *len_out = data_len + 10; /* header (10 bytes) + body */
            return &pdr_repository[off];
        }
    }
    return NULL;
}

int handle_get_pdr(uint16_t handle, uint8_t *dst, size_t dst_len, uint16_t *resp_len)
{
    uint16_t len;
    const uint8_t *pdr = pdr_by_handle(handle, &len);
    if (!pdr || dst_len < len) {
        return -EINVAL;
    }
    memcpy(dst, pdr, len);
    *resp_len = len;
    return 0;
}

void demo(void)
{
    uint8_t buf[128];
    uint16_t len;
    if (handle_get_pdr(PDR_HANDLE_SENSOR_CPU0, buf, sizeof(buf), &len) == 0) {
        /* buf now holds the Numeric Sensor PDR bytes */
        uint16_t sensor_id = (uint16_t)(buf[PDR_FIELD_SENSOR_CPU0_SENSOR_ID_OFF] |
                                        (buf[PDR_FIELD_SENSOR_CPU0_SENSOR_ID_OFF + 1] << 8));
        (void)sensor_id;
    }
}
```

## Notes and assumptions
- Little-endian packing, per PLDM baseline.
- `dataLength` in the header is computed from body length; header is always 10 bytes.
- Handles validated unique; if duplicates exist across YAMLs, later duplicates are auto-renumbered upward with a notice.
- Field offsets are relative to the start of each PDR blob (header included).
- Fast enough for <100 PDRs (single pass, pure Python/struct).
