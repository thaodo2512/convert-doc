# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

PLDM PDR (Platform Descriptor Record) repository system for embedded/BMC firmware, targeting Zephyr RTOS. Implements DSP0248 v1.3.0 spec with zero-copy storage, multi-terminus management, and change event support. No dynamic allocation — all static buffers.

## Key Commands

### Code Generation (YAML → C)
```bash
python3 source/code_gen.py source/data source/schema output/pdr_generated.c \
  --macros source/data/macro_defs.yaml
```
Produces both `output/pdr_generated.c` and `output/pdr_generated.h`.

### Compile Example
```bash
gcc -o pdr_example source/pldm_pdr_repo.c output/pdr_generated.c \
  source/pldm_pdr_repo_example.c -Isource -Wall
./pdr_example
```

### Reverse-Decode (C/binary → YAML)
```bash
python3 pdr_repo_to_yaml.py <input> <schema_dir> [--output out.yaml]
```

### Documentation (Sphinx)
```bash
make html    # or: sphinx-build -b html source build
```

### Python Dependencies
```bash
pip install pyyaml jsonschema
```

## Architecture

### Layers (bottom → top)
1. **Code Generation** (`source/code_gen.py`) — Reads YAML PDR data + JSON schemas, packs binary blobs, emits `.c` and `.h` files
2. **PDR Repository** (`pldm_pdr_repo.c/.h`) — Single-instance blob+index storage with zero-copy access, tombstone-based deletion
3. **PDR Manager** (`pldm_pdr_mgr.c/.h`) — Multi-terminus discovery, remote PDR fetching, handle remapping into consolidated repo
4. **Change Events** (`pldm_pdr_chg_event.c/.h`) — Encode/decode/validate `pldmPDRRepositoryChgEvent` (DSP0248 §16.14)
5. **Change Event Handler** (`pldm_pdr_chg_event_handler.c/.h`) — Manager-side incremental update from incoming events

### Code Generation Pipeline

**Inputs:**
- `source/data/*.yaml` — PDR definitions (recursive discovery, parent-dir-first order). Files must have `pdrHeader` to be processed; others (e.g., `macro_defs.yaml`) are skipped.
- `source/schema/type_N.json` — JSON schemas define `binaryOrder` (serialization order) and `binaryFormat` (pack format). PDR type is read from `pdrHeader.PDRType` in the YAML, not from the filename.
- `source/data/macro_defs.yaml` — Binds C `#define` names to specific YAML field paths (e.g., `sensorID.value`)

**Outputs:**
- `.h` — Include guard, bound macros, blob capacity/size defines, PDR statistics (counts, min/max sizes, handle range), per-record handle/offset/size macros, X-macro type list, function prototypes
- `.c` — Static blob arrays (`pdr_blob_data`, `pdr_blob_backup`), `pdr_repo_populate_ext()` (zero-copy init), `pdr_repo_populate()` (rebuild callback)

### PDR Repository Design
- **10-byte common header:** `recordHandle(u32) + version(u8) + type(u8) + changeNum(u16) + dataLength(u16)`
- **Zero-copy:** GetPDR/FindPDR return pointers into contiguous blob
- **Tombstone deletion:** `pdr_repo_remove_record()` is O(1); compaction happens on `pdr_repo_run_init_agent()`
- **Limits:** `PDR_REPO_MAX_SIZE` = 8KB blob, `PDR_MAX_RECORD_COUNT` = 64 records

### Manager Handle Remapping
Each remote terminus gets a dedicated handle range:
- terminus_idx 0 → `0x10001–0x1FFFF`
- terminus_idx 1 → `0x20001–0x2FFFF`
- Up to 8 termini

## File Conventions

- YAML PDR files go in `source/data/` (or subdirectories — processed recursively)
- JSON schemas go in `source/schema/` named `type_N.json` matching the PDR type number
- All packing is little-endian per DSP0248
- `recordHandle: auto` in YAML triggers auto-assignment
- `dataLength` is always auto-computed from the packed body
- `_doc: { hidden: true }` hides fields from Sphinx docs but keeps them in binary output

## Documentation

- `docs/design.html` / `docs/design.rst` — Architecture deep-dive: pipeline, runtime, manager, change events, all 28 PDR types
- `docs/yaml-authoring-guide.html` / `docs/yaml-authoring-guide.rst` — Step-by-step guide for creating new YAML PDR data files (value/comment pattern, reading schemas, field type cookbook, complete examples, common errors)

HTML files are self-contained (inline CSS, no external dependencies). RST files integrate with Sphinx — add to a `toctree` directive to include in built docs.

## Alternate Generator

`generate_pdr_repo.py` is a standalone generator (not Sphinx-integrated) that produces a different output format (`pdr_repository[]` + `pdr_offsets[]`). The primary generator is `source/code_gen.py`.
