.. _design:

======================================
PLDM PDR Repository — Design Document
======================================

| **DSP0248 v1.3.0** — Platform Level Data Model for Platform Monitoring and Control
| Schema-driven code generation for embedded PDR management — zero-copy, no dynamic allocation

.. contents:: Contents
   :depth: 2
   :local:


1. Overview
===========

This project implements a **Platform Descriptor Record (PDR) repository** system
for embedded and BMC firmware targeting Zephyr RTOS, conforming to DMTF DSP0248
v1.3.0.

Key Characteristics
-------------------

- **Zero-Copy** — ``GetPDR`` and ``FindPDR`` return pointers directly into a
  contiguous blob; no memcpy on reads
- **No Dynamic Allocation** — All buffers are statically sized; safe for hard
  real-time environments
- **Schema-Driven** — JSON schemas define binary layout; YAML provides instance
  data; Python generates C code
- **Multi-Terminus** — Manager consolidates PDRs from up to 8 remote endpoints
  with handle remapping

Architecture Layers
-------------------

.. list-table::
   :header-rows: 1
   :widths: 20 30 50

   * - Layer
     - Files
     - Role
   * - Code Generation
     - ``code_gen.py``
     - YAML + JSON Schema → packed C arrays
   * - PDR Repository
     - ``pldm_pdr_repo.c/.h``
     - Single-instance blob+index storage, zero-copy access
   * - PDR Manager
     - ``pldm_pdr_mgr.c/.h``
     - Multi-terminus discovery, remote fetch, handle remapping
   * - Change Events
     - ``pldm_pdr_chg_event.c/.h``
     - Encode/decode/validate ``pldmPDRRepositoryChgEvent``
   * - Event Handler
     - ``pldm_pdr_chg_event_handler.c/.h``
     - Manager-side incremental update from incoming events


2. Pipeline
===========

::

    ┌───────────────────┐
    │  YAML Data        │
    │  source/data/     │──┐
    └───────────────────┘  │
    ┌───────────────────┐  │    ┌──────────────┐    ┌──────────────────┐    ┌─────────────────┐
    │  JSON Schemas     │──┼───>│ code_gen.py  │───>│ pdr_generated.h  │───>│ pldm_pdr_repo   │
    │  source/schema/   │  │    │ validate     │    │ pdr_generated.c  │    │ zero-copy init   │
    └───────────────────┘  │    │ pack → emit  │    │ blobs, populate  │    │ serve queries    │
    ┌───────────────────┐  │    └──────────────┘    └──────────────────┘    └─────────────────┘
    │  Macro Defs       │──┘
    │  macro_defs.yaml  │
    └───────────────────┘

.. note::

   The generator reads YAML PDR instances, validates them against JSON schemas,
   packs each field into little-endian binary per ``binaryOrder`` and
   ``binaryFormat``, then emits self-contained C files. At runtime, the
   pre-packed blob is indexed in-place — no parsing or copying required.


3. Project Structure
====================

::

    source/
      code_gen.py                  # Primary generator (YAML → C)
      pldm_pdr_repo.h/.c          # Core repository (blob + index)
      pldm_pdr_mgr.h/.c           # Multi-terminus manager
      pldm_pdr_chg_event.h/.c     # Change event codec
      pldm_pdr_chg_event_handler.h/.c  # Event handler
      pldm_pdr_repo_example.c     # Usage example
      conf.py                     # Sphinx configuration
      data/                       # YAML PDR definitions (recursive)
        terminus.yaml             # Terminus Locator (Type 1)
        type_2.yaml ... type_127.yaml  # Per-type instances
        macro_defs.yaml           # C macro bindings
      schema/                     # JSON schemas for binary packing
        type_1.json ... type_127.json  # 28 type schemas
      _extensions/                # Sphinx extensions
    output/
      pdr_generated.h             # Generated header (macros, stats)
      pdr_generated.c             # Generated impl (blobs, populate)
    docs/                         # Documentation
    generate_pdr_repo.py          # Alternate standalone generator
    pdr_repo_to_yaml.py           # Reverse decoder (C/binary → YAML)


4. Schema Design
================

Each PDR type has a JSON Schema file (``source/schema/type_N.json``) that
defines both validation constraints and binary serialization rules via custom
extensions.

Core Extensions
---------------

.. list-table::
   :header-rows: 1
   :widths: 25 35 40

   * - Extension
     - Purpose
     - Example
   * - ``binaryOrder``
     - Array specifying field serialization order
     - ``["pdrHeader", "sensorID", "entityType"]``
   * - ``binaryFormat``
     - Python ``struct`` pack code for the field
     - ``"I"`` (uint32), ``"H"`` (uint16), ``"B"`` (uint8)
   * - ``oneOf``
     - Polymorphic types selected by ``const`` discriminator
     - Terminus locator variants (UID, MCTP_EID, etc.)
   * - ``x-binary-encoding``
     - String encoding
     - ``"utf-8"``, ``"utf-16be"``, ``"us-ascii"``
   * - ``x-binary-terminator``
     - String null-termination value
     - ``"0x00"``, ``"0x0000"``
   * - ``x-binary-type-mapping``
     - Context-dependent format resolution
     - Maps sensor data size to corresponding pack format
   * - ``formatResolver``
     - Conditional format based on another field's value
     - ``dependsOn: "sensorDataSize"``

Format Codes
------------

.. list-table::
   :header-rows: 1
   :widths: 15 30 15

   * - Code
     - Type
     - Size
   * - ``B``
     - uint8
     - 1 byte
   * - ``H``
     - uint16 LE
     - 2 bytes
   * - ``I``
     - uint32 LE
     - 4 bytes
   * - ``Q``
     - uint64 LE
     - 8 bytes
   * - ``b`` / ``h`` / ``i`` / ``q``
     - signed int8/16/32/64
     - 1/2/4/8 bytes
   * - ``f`` / ``d``
     - float / double
     - 4/8 bytes

Schema Example (Type 1 — Terminus Locator)
-------------------------------------------

.. code-block:: json

   {
     "title": "Terminus Locator PDR (DSP0248 Type 1)",
     "binaryOrder": ["pdrHeader", "PLDMTerminusHandle", "validity",
                     "TID", "containerID", "locator"],
     "properties": {
       "pdrHeader": {
         "binaryOrder": ["recordHandle", "PDRHeaderVersion",
                         "PDRType", "recordChangeNumber", "dataLength"],
         "properties": {
           "recordHandle": { "binaryFormat": "I" },
           "PDRType":       { "const": 1, "binaryFormat": "B" }
         }
       },
       "locator": {
         // Polymorphic: type selected by terminusLocatorType const
         "oneOf": [
           { "title": "UID (Type 0)"      },
           { "title": "MCTP_EID (Type 1)" }
         ]
       }
     }
   }


5. YAML Data Files
===================

PDR instances live in ``source/data/`` (discovered recursively). Each file with
a ``pdrHeader`` key defines one PDR record. Files without ``pdrHeader`` (e.g.,
``macro_defs.yaml``) are skipped.

Value/Comment Pattern
---------------------

Every field uses a ``{value, comment}`` wrapper to carry documentation alongside
data:

.. code-block:: yaml

   sensorID:
     value: 256
     comment: "Primary temperature sensor"

   entityType:
     value: 42
     comment: "Processor module"

The generator extracts ``.value`` for packing and ``.comment`` for
documentation. Comments are optional.

Auto Handles
------------

.. code-block:: yaml

   pdrHeader:
     recordHandle:
       value: auto    # Generator assigns next available handle
     PDRType:
       value: 2

Handles set to ``auto`` are assigned monotonically, skipping any explicitly
reserved values.

Hidden Fields
-------------

.. code-block:: yaml

   _doc:
     hidden: true     # Excluded from Sphinx docs, still packed in binary

Macro Bindings (``macro_defs.yaml``)
-------------------------------------

Binds C ``#define`` names to specific field values in PDR files:

.. code-block:: yaml

   macros:
     - name: SENSOR_INIT_SENSOR_ID
       file: "type_2.yaml"
       field: "sensorID.value"

     - name: SENSOR_INIT_UPPER_WARN
       file: "type_3.yaml"
       field: "upperThresholdWarning.value"

Field paths support dot notation, array indices (``[0]``), and nested arrays.


6. Code Generator
=================

``source/code_gen.py`` (~670 lines) drives the full pipeline from YAML to C.

Execution Flow
--------------

.. code-block:: bash

   python3 source/code_gen.py source/data source/schema output/pdr_generated.c \
       --macros source/data/macro_defs.yaml

1. **discover_yaml_files(yaml_dir)** — Recursively scan for ``.yaml/.yml``
   files, filter to those containing ``pdrHeader``, sort parent-dir-first then
   alphabetically

2. **collect_reserved_handles()** — First pass: collect explicitly set
   ``recordHandle`` values to avoid collisions

3. **process_single_yaml()** — For each PDR file:

   a. Read YAML, extract ``pdrHeader.PDRType``
   b. Load ``type_N.json`` schema
   c. Validate with ``jsonschema``
   d. Assign or confirm handle via ``assign_handle()``
   e. Pack 10-byte common header
   f. Pack body fields recursively via ``pack_field()``
   g. Auto-compute ``dataLength`` from body size

4. **Sort** all records by handle

5. **Emit ``.h``** — Macros, statistics, per-record metadata, X-macro list,
   prototypes

6. **Emit ``.c``** — Blob arrays, ``pdr_repo_populate_ext()``,
   ``pdr_repo_populate()``

``pack_field()`` — Recursive Binary Packer
-------------------------------------------

Handles all JSON Schema types:

.. list-table::
   :header-rows: 1
   :widths: 25 75

   * - Type
     - Behavior
   * - Integer / Float / Bool
     - ``struct.pack(format, value)``
   * - Object
     - Recurse sub-fields in ``binaryOrder``
   * - Array
     - Pack each item via item schema
   * - String
     - Encode per ``x-binary-encoding`` with optional null-terminator
   * - Variable-length
     - Hex string or byte list
   * - ``oneOf``
     - Select variant by matching ``const`` discriminator field

Format Inference
----------------

If ``binaryFormat`` is absent, ``infer_format()`` derives it from the JSON
Schema type and bounds (e.g., ``maximum: 255`` → ``"B"``). For fields with
``formatResolver.dependsOn``, the format is looked up dynamically via
``x-binary-type-mapping``.


7. Generated Output
====================

Header File (``pdr_generated.h``)
----------------------------------

.. code-block:: c

   #ifndef PDR_GENERATED_H_
   #define PDR_GENERATED_H_
   #include "pldm_pdr_repo.h"

   /* ---- Bound Macros (from macro_defs.yaml) ---- */
   #define SENSOR_INIT_SENSOR_ID   256
   #define SENSOR_INIT_UPPER_WARN  100

   /* ---- Blob Configuration ---- */
   #define PDR_BLOB_CAPACITY       1480
   #define PDR_BLOB_DATA_SIZE      1184
   #define PDR_HEADER_SIZE         10

   /* ---- PDR Statistics ---- */
   #define PDR_COUNT               30
   #define PDR_TYPE_COUNT          28
   #define PDR_MAX_RECORD_SIZE     90
   #define PDR_MIN_RECORD_SIZE     19

   /* ---- Per-Record Macros ---- */
   #define PDR_HANDLE_TERMINUS     1
   #define PDR_OFFSET_TERMINUS     0
   #define PDR_SIZE_TERMINUS       35   /* body=25 */

   /* ---- X-Macro Type List ---- */
   #define PDR_TYPE_LIST \
       PDR_TYPE_ENTRY(1, 2) \
       PDR_TYPE_ENTRY(2, 1) \
       /* ... all 28 types */

   /* ---- Prototypes ---- */
   void pdr_repo_populate_ext(pdr_repo_t *repo, void *ctx);
   void pdr_repo_populate(pdr_repo_t *repo, void *ctx);

   #endif

C File (``pdr_generated.c``)
------------------------------

.. code-block:: c

   #include "pdr_generated.h"

   /* Mutable blob with 25% headroom for runtime adds */
   static uint8_t pdr_blob_data[PDR_BLOB_CAPACITY] = {
       /* [0] terminus.yaml  handle=1  type=1  total=35 */
       0x01, 0x00, 0x00, 0x00,   /* recordHandle    */
       0x01,                     /* headerVersion   */
       0x01,                     /* pdrType         */
       0x00, 0x00,               /* recordChangeNum */
       0x19, 0x00,               /* dataLength = 25 */
       /* ... body bytes ... */
   };

   /* Const body-only backup for rebuild */
   static const uint8_t pdr_blob_backup[] = { /* ... */ };

   /* Zero-copy fast init: index pre-packed records in-place */
   void pdr_repo_populate_ext(pdr_repo_t *repo, void *ctx) {
       (void)ctx;
       pdr_repo_init_ext(repo, pdr_blob_data, PDR_BLOB_CAPACITY);
       repo->blob_used = PDR_BLOB_DATA_SIZE;
       pdr_repo_index_record(repo, 0);     /* terminus  */
       pdr_repo_index_record(repo, 35);    /* type_1    */
       /* ... index all records ... */
       pdr_repo_update_info(repo);
   }

   /* Rebuild callback (used by RunInitAgent) */
   void pdr_repo_populate(pdr_repo_t *repo, void *ctx) {
       (void)ctx;
       pdr_repo_add_record(repo, 1, &pdr_blob_backup[0], 25, NULL);
       /* ... re-add all records ... */
   }

Two Blob Arrays
----------------

.. list-table::
   :header-rows: 1
   :widths: 22 12 33 33

   * - Array
     - Mutability
     - Content
     - Purpose
   * - ``pdr_blob_data[]``
     - Mutable
     - Full records (header + body) + headroom
     - Primary runtime storage; used by ``populate_ext``
   * - ``pdr_blob_backup[]``
     - ``const``
     - Body only, compact
     - Rebuild source for ``populate`` / ``RunInitAgent``


8. Runtime Architecture
========================

PDR Common Header (10 bytes)
-----------------------------

::

    0                   1                   2                   3
    0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1
   +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
   |                       recordHandle (u32)                      |
   +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
   |   version(u8) |   pdrType(u8) |     recordChangeNum (u16)     |
   +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
   |         dataLength (u16)      |
   +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+

Repository Data Structures
---------------------------

.. code-block:: c

   typedef struct {
       uint8_t  *blob;                /* Contiguous PDR storage           */
       uint32_t blob_capacity;         /* Total allocated size             */
       uint32_t blob_used;             /* Bytes currently occupied         */
       pdr_index_entry_t index[64];   /* Metadata index (max 64 records)  */
       uint16_t count;                 /* Active record count              */
       pdr_repo_info_t info;           /* GetPDRRepositoryInfo response    */
       uint32_t signature;             /* Cached CRC32                     */
       bool     signature_valid;       /* Cache invalidation flag          */
       uint32_t next_record_handle;    /* Auto-assign counter              */
   } pdr_repo_t;

   typedef struct {
       uint32_t record_handle;         /* Opaque PDR identifier            */
       uint32_t offset;                /* Byte offset in blob              */
       uint16_t size;                  /* Total size (header + body)       */
       uint8_t  pdr_type;              /* Type code (1-127)                */
       uint8_t  flags;                 /* Bit 0: tombstone                 */
   } pdr_index_entry_t;

Key Constants
-------------

.. list-table::
   :header-rows: 1
   :widths: 35 15 50

   * - Constant
     - Value
     - Purpose
   * - ``PDR_REPO_MAX_SIZE``
     - 8 KB
     - Maximum blob capacity
   * - ``PDR_MAX_RECORD_COUNT``
     - 64
     - Maximum records per repository
   * - ``PDR_TRANSFER_CHUNK_SIZE``
     - 128 B
     - Multi-part GetPDR chunk size
   * - ``PDR_HEADER_SIZE``
     - 10 B
     - Common header size (fixed)

Core Operations
---------------

**Initialization:**

.. code-block:: c

   pdr_repo_t repo;
   pdr_repo_populate_ext(&repo, NULL);  /* Zero-copy: index pre-packed blob */

**Zero-Copy Read:**

.. code-block:: c

   const uint8_t *data;  uint16_t len;
   pdr_repo_get_pdr(&repo, handle, 0,
       &next_handle, &next_xfer, &xfer_flag,
       &data, &len);
   /* data points directly into blob — no copy */

**Tombstone Deletion:**

.. code-block:: c

   pdr_repo_remove_record(&repo, handle);  /* O(1): sets tombstone flag */
   /* Space reclaimed on next pdr_repo_run_init_agent() */

**Signature:**

.. code-block:: c

   uint32_t sig = pdr_repo_get_signature(&repo);
   /* Lazy CRC32 — cached until mutation invalidates */

Repository API Summary
-----------------------

.. list-table::
   :header-rows: 1
   :widths: 35 15 50

   * - Function
     - Complexity
     - Description
   * - ``pdr_repo_init_ext()``
     - O(1)
     - Bind external blob buffer
   * - ``pdr_repo_index_record()``
     - O(1)
     - Index a pre-filled record at offset
   * - ``pdr_repo_add_record()``
     - O(1)
     - Append record, auto-assign handle
   * - ``pdr_repo_remove_record()``
     - O(n)
     - Find and tombstone by handle
   * - ``pdr_repo_get_pdr()``
     - O(n)
     - Fetch by handle, multi-part support
   * - ``pdr_repo_find_pdr()``
     - O(n)
     - Search by PDR type from start handle
   * - ``pdr_repo_get_signature()``
     - O(n)*
     - CRC32, cached after first computation
   * - ``pdr_repo_run_init_agent()``
     - O(n)
     - Full rebuild via callback (compacts tombstones)


9. PDR Types
=============

The system supports **28 PDR types** defined by DSP0248. Each has a
corresponding JSON schema and can have one or more YAML instances.

.. list-table::
   :header-rows: 1
   :widths: 8 45 22

   * - Type
     - Name
     - Category
   * - 1
     - Terminus Locator
     - Platform
   * - 2
     - Numeric Sensor
     - Sensor
   * - 3
     - Numeric Sensor Initialization
     - Sensor
   * - 4
     - State Sensor
     - Sensor
   * - 5
     - State Sensor Initialization
     - Sensor
   * - 6
     - Sensor Auxiliary Names
     - Sensor
   * - 7
     - OEM Unit
     - Platform
   * - 8
     - OEM State Set
     - Platform
   * - 9
     - Numeric Effecter
     - Effecter
   * - 10
     - Numeric Effecter Initialization
     - Effecter
   * - 11
     - State Effecter
     - Effecter
   * - 12
     - Entity Association
     - Platform
   * - 13
     - Effecter Auxiliary Names
     - Effecter
   * - 14
     - OEM Entity ID
     - Platform
   * - 15
     - Interrupt Association
     - Platform
   * - 16
     - Event Log
     - Event
   * - 17
     - FRU Record Set Identifier
     - Platform
   * - 18
     - Compact Numeric Sensor
     - Sensor
   * - 19
     - Large Compact Numeric Sensor
     - Sensor
   * - 20
     - OEM Device
     - Platform
   * - 21
     - OEM PDR
     - OEM
   * - 22
     - Redfish Resource
     - Platform
   * - 23
     - Redfish Entity Association
     - Platform
   * - 24
     - Redfish Action
     - Platform
   * - 25
     - Compact Numeric Sensor (Signed)
     - Sensor
   * - 30
     - Timing PDR
     - Platform
   * - 126
     - OEM (Vendor Range Start)
     - OEM
   * - 127
     - OEM (Vendor Range End)
     - OEM

.. note::

   PDR type is determined from the YAML file's ``pdrHeader.PDRType`` value, not
   from the filename. The generator loads the corresponding ``type_N.json``
   schema dynamically.


10. PDR Manager
================

The manager (``pldm_pdr_mgr.c/.h``) consolidates PDRs from multiple remote
PLDM termini into a single local repository with handle remapping.

Handle Remapping Scheme
-----------------------

::

    Terminus Index    Local Handle Range
    ─────────────     ─────────────────────
    idx 0             0x10001 – 0x1FFFF
    idx 1             0x20001 – 0x2FFFF
    idx 2             0x30001 – 0x3FFFF
    ...
    idx 7             0x80001 – 0x8FFFF

Remote handles are remapped into non-overlapping ranges. The original remote
handle is preserved in the ``handle_map`` for reverse lookup.

Terminus State Machine
-----------------------

::

    UNUSED ──> DISCOVERED ──> SYNCING ──> SYNCED
                   │              │           │
                   │              v           v
                   │           ERROR       STALE
                   └──────────────────────────┘

.. list-table::
   :header-rows: 1
   :widths: 20 80

   * - State
     - Description
   * - ``UNUSED``
     - Slot is free
   * - ``DISCOVERED``
     - Endpoint registered, not yet synced
   * - ``SYNCING``
     - Fetch in progress
   * - ``SYNCED``
     - PDRs fetched and merged
   * - ``STALE``
     - Signature changed, needs re-sync
   * - ``ERROR``
     - Fetch failed

Manager Constants
------------------

.. list-table::
   :header-rows: 1
   :widths: 45 15

   * - Constant
     - Value
   * - ``PDR_MGR_MAX_TERMINI``
     - 8
   * - ``PDR_MGR_REASSEMBLY_BUF_SIZE``
     - 256 B
   * - ``PDR_MGR_HANDLE_RANGE_SHIFT``
     - 16

Transport Abstraction
----------------------

.. code-block:: c

   typedef int (*pdr_mgr_send_recv_fn)(
       uint8_t        eid,        /* MCTP endpoint ID     */
       uint8_t        pldm_type,  /* 0x02 = Platform M&C  */
       uint8_t        command,    /* GetPDR, etc.         */
       const uint8_t *req_data,
       uint16_t       req_len,
       uint8_t       *resp_data,
       uint16_t      *resp_len,
       void          *ctx
   );

The manager is transport-agnostic. Callers provide a send/receive callback for
MCTP communication.

Key Manager API
----------------

.. list-table::
   :header-rows: 1
   :widths: 40 60

   * - Function
     - Description
   * - ``pdr_mgr_init()``
     - Initialize consolidated repo + transport binding
   * - ``pdr_mgr_add_terminus()``
     - Register remote endpoint (EID, TID, handle)
   * - ``pdr_mgr_sync_terminus()``
     - Fetch all PDRs from a terminus, remap handles
   * - ``pdr_mgr_sync_all()``
     - Sync all discovered termini
   * - ``pdr_mgr_check_for_changes()``
     - Compare signature to detect remote changes
   * - ``pdr_mgr_lookup_origin()``
     - Map local handle back to terminus + remote handle


11. Change Events
==================

Implements ``pldmPDRRepositoryChgEvent`` per DSP0248 §16.14, enabling
incremental PDR updates without full re-sync.

Event Formats
-------------

.. list-table::
   :header-rows: 1
   :widths: 30 10 60

   * - Format
     - Value
     - Behavior
   * - ``refreshEntireRepository``
     - 0x00
     - Full re-sync; zero change records
   * - ``formatIsPDRTypes``
     - 0x01
     - Change records carry PDR type codes
   * - ``formatIsPDRHandles``
     - 0x02
     - Change records carry specific handles

Operations
----------

.. list-table::
   :header-rows: 1
   :widths: 30 10

   * - Operation
     - Value
   * - ``refreshAllRecords``
     - 0x00
   * - ``recordsDeleted``
     - 0x01
   * - ``recordsAdded``
     - 0x02
   * - ``recordsModified``
     - 0x03

Wire Format
-----------

::

    ┌──────────────────────┐
    │ eventDataFormat  (1B)│
    │ numChangeRecords (1B)│
    ├──────────────────────┤
    │ Change Record 0:     │
    │  operation    (1B)   │
    │  numEntries   (1B)   │
    │  entry[0]     (4B)   │
    │  entry[1]     (4B)   │
    │  ...                 │
    ├──────────────────────┤
    │ Change Record 1:     │
    │  ...                 │
    └──────────────────────┘
    All multi-byte fields: little-endian

Validation Rules (DSP0248)
---------------------------

.. list-table::
   :header-rows: 1
   :widths: 10 90

   * - Rule
     - Constraint
   * - V1
     - ``refreshEntireRepository`` must have 0 change records
   * - V2
     - ``formatIsPDRHandles`` cannot use ``refreshAllRecords`` operation
   * - V3
     - Format is per-event (cannot mix types and handles)
   * - V4
     - Operations ordered: deletes ≤ adds ≤ modifies
   * - V5
     - Entry count ≤ ``PDR_CHG_EVENT_MAX_ENTRIES`` (16)

Tracker API (Terminus Side)
----------------------------

.. code-block:: c

   pdr_chg_tracker_t tracker;
   pdr_chg_tracker_init(&tracker);

   /* Record changes as they happen */
   pdr_chg_tracker_record_add(&tracker, new_handle);
   pdr_chg_tracker_record_delete(&tracker, old_handle);
   pdr_chg_tracker_record_modify(&tracker, changed_handle);

   /* Build event for transmission */
   pdr_chg_event_t event;
   pdr_chg_tracker_build_event(&tracker, &event,
       PDR_CHG_FORMAT_PDR_HANDLES, PDR_CHG_EVENT_DEFAULT_MTU);

Event Handler (Manager Side)
------------------------------

.. code-block:: c

   /* Incoming event from remote terminus */
   pdr_chg_event_handle(&mgr, eid, event_data, event_data_len);
   /* - refreshEntireRepository → full pdr_mgr_sync_terminus()
      - formatIsPDRTypes        → full re-sync
      - formatIsPDRHandles      → incremental add/delete/modify */


12. Quick Start
================

Prerequisites
-------------

.. code-block:: bash

   pip install pyyaml jsonschema

Generate C Code from YAML
---------------------------

.. code-block:: bash

   python3 source/code_gen.py source/data source/schema output/pdr_generated.c \
       --macros source/data/macro_defs.yaml

Produces both ``output/pdr_generated.c`` and ``output/pdr_generated.h``.

Compile and Run Example
-------------------------

.. code-block:: bash

   gcc -o pdr_example \
       source/pldm_pdr_repo.c \
       output/pdr_generated.c \
       source/pldm_pdr_repo_example.c \
       -Isource -Wall

   ./pdr_example

Reverse Decode (C/Binary to YAML)
-----------------------------------

.. code-block:: bash

   python3 pdr_repo_to_yaml.py <input> <schema_dir> [--output out.yaml]

Build Sphinx Documentation
----------------------------

.. code-block:: bash

   make html
   # or: sphinx-build -b html source build

Minimal Integration
--------------------

.. code-block:: c

   #include "pdr_generated.h"

   int main(void) {
       pdr_repo_t repo;

       /* Option A: Zero-copy init (fastest) */
       pdr_repo_populate_ext(&repo, NULL);

       /* Read a PDR by handle */
       const uint8_t *data;
       uint16_t len;
       uint32_t next_h, next_xfer;
       uint8_t  xfer_flag;
       pdr_repo_get_pdr(&repo, PDR_HANDLE_TERMINUS, 0,
           &next_h, &next_xfer, &xfer_flag, &data, &len);

       /* data now points directly into the blob */
       return 0;
   }
