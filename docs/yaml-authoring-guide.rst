.. _yaml-authoring-guide:

========================
YAML PDR Authoring Guide
========================

| How to create new PDR data files for the PLDM code generator
| *From blank file to generated C code in 5 steps*

.. contents:: Contents
   :depth: 2
   :local:


1. Big Picture
==============

Each YAML file in ``source/data/`` defines **one PDR record**. The code
generator reads it, validates it against the matching JSON schema, packs it
into binary, and emits C code.

::

    Your YAML file              JSON Schema
    source/data/my_pdr.yaml     source/schema/type_N.json
            \                      /
             \                    /
              v                  v
           code_gen.py  (validate + pack)
                |
                v
       output/pdr_generated.c/.h

The relationship is simple:

- Your YAML provides the **data** (field values)
- The JSON schema defines the **structure** (field names, types, binary layout)
- The generator reads both and produces C arrays with your data pre-packed

.. note::

   **Key rule:** The PDR type is determined by ``pdrHeader.PDRType`` in your
   YAML. The generator loads ``source/schema/type_N.json`` where ``N`` matches
   that value. The filename of your YAML does not matter.


2. Step-by-Step Walkthrough
============================

**Step 1: Choose your PDR type**
   Decide which PDR type you need (e.g., Type 1 = Terminus Locator, Type 2 =
   Numeric Sensor, Type 4 = State Sensor). Check ``source/schema/`` to see
   which types have schemas.

**Step 2: Open the JSON schema**
   Read ``source/schema/type_N.json`` for your chosen type. The ``binaryOrder``
   array at the top level tells you **every field you need**, in order. Each
   property's constraints tell you valid values.

**Step 3: Create your YAML file**
   Create a new file in ``source/data/`` (e.g., ``source/data/my_sensor.yaml``).
   Start with the ``pdrHeader``, then add every field listed in
   ``binaryOrder``, using the ``value:``/``comment:`` wrapper pattern.

**Step 4: Run the generator**

   .. code-block:: bash

      python3 source/code_gen.py source/data source/schema output/pdr_generated.c \
          --macros source/data/macro_defs.yaml

   Fix any validation errors until it succeeds.

**Step 5: Verify the output**
   Check ``output/pdr_generated.h`` for your new ``PDR_HANDLE_*``,
   ``PDR_OFFSET_*``, and ``PDR_SIZE_*`` macros. Check the ``.c`` blob array
   for your record's bytes.


3. The Value/Comment Pattern
=============================

Every field in a PDR YAML file uses a wrapper with ``value`` (required) and
``comment`` (optional):

**What you write:**

.. code-block:: yaml

   sensorID:
     value: 256
     comment: "Primary temperature sensor"

**What the generator sees:**

After cleaning: ``sensorID: 256``

Packed as: ``struct.pack('<H', 256)`` → ``0x00 0x01``

The generator's ``clean_for_validation()`` function strips the wrapper — it
extracts ``.value`` for validation and packing, and ``.comment`` for Sphinx
documentation.

Rules
-----

- ``value:`` is **required** — this is the actual data that gets packed into
  binary
- ``comment:`` is **optional** — used only for documentation, never affects
  binary output
- Works for all types: integers, floats, strings, booleans, arrays

Variations
----------

.. code-block:: yaml

   # Integer with comment
   entityType:
     value: 42
     comment: "Processor module"

   # Integer without comment (also valid)
   entityType:
     value: 42

   # Float value
   resolution:
     value: 1.0

   # String value
   sensorName:
     value: "Temp Sensor"

   # Array value
   deviceUID:
     value: [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15]
     comment: "16-byte UUID"

   # Boolean-like (0 or 1)
   validity:
     value: 1
     comment: "1 = valid"

.. warning::

   **Common mistake:** Writing ``sensorID: 256`` without the ``value:``
   wrapper. This will cause a validation error because the generator expects
   the wrapper structure.


4. The PDR Header
==================

Every YAML file **must** start with ``pdrHeader``. This 10-byte structure is
common to all PDR types:

.. code-block:: yaml

   pdrHeader:
     recordHandle:
       value: auto           # or an explicit integer like 42
     PDRHeaderVersion:
       value: 1              # Always 1 per DSP0248
     PDRType:
       value: N              # Must match your target type (1-127)
     recordChangeNumber:
       value: 0              # Usually 0 for initial records
     dataLength:
       value: auto           # Let the generator calculate this

Field Details
-------------

.. list-table::
   :header-rows: 1
   :widths: 20 15 65

   * - Field
     - Type
     - Advice
   * - ``recordHandle``
     - uint32 or ``"auto"``
     - Use ``auto`` unless you need a specific handle. Auto-assignment avoids
       collisions with other files.
   * - ``PDRHeaderVersion``
     - uint8
     - Always set to ``1``.
   * - ``PDRType``
     - uint8
     - This is the critical field. It tells the generator which
       ``type_N.json`` schema to use. Must match the schema's ``const`` value.
   * - ``recordChangeNumber``
     - uint16
     - Set to ``0`` for new records. Incremented at runtime when records are
       modified.
   * - ``dataLength``
     - uint16 or ``"auto"``
     - **Always use auto.** The generator computes the correct body length. If
       you set it manually and it's wrong, you'll get a warning.

.. tip::

   Always use ``auto`` for both ``recordHandle`` and ``dataLength`` unless you
   have a specific reason not to. This avoids the two most common sources of
   errors.


5. Reading a JSON Schema
==========================

The JSON schema is your reference for what fields to include and what values
are valid. Here's how to read one:

Anatomy of a Schema
--------------------

.. code-block:: json

   {
     "title": "State Sensor PDR (DSP0248 Type 4)",
     "type": "object",
     "binaryOrder": [
       "pdrHeader",
       "PLDMTerminusHandle",
       "sensorID",
       "entityType",
       "entityInstanceNumber",
       "containerID",
       "sensorInit",
       "sensorAuxiliaryNamesPDR",
       "compositeSensorCount",
       "stateSensors"
     ],
     "properties": {
       "sensorID": {
         "type": "integer",
         "minimum": 0,
         "maximum": 65535,
         "binaryFormat": "H"
       }
     }
   }

What to Look For
-----------------

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Schema Key
     - What It Tells You
   * - ``binaryOrder``
     - The **exact fields** your YAML must include, in serialization order.
       Include every field listed here.
   * - ``properties.X.type``
     - The JSON type: ``"integer"``, ``"number"`` (float), ``"string"``,
       ``"array"``, ``"object"``
   * - ``properties.X.binaryFormat``
     - The pack format: ``"B"``\=uint8, ``"H"``\=uint16, ``"I"``\=uint32,
       ``"f"``\=float32, etc.
   * - ``properties.X.minimum/maximum``
     - Valid value range. Violating this causes a validation error.
   * - ``properties.X.const``
     - The field must be exactly this value (used for type discriminators).
   * - ``properties.X.enum``
     - The field must be one of these values.
   * - ``properties.X.oneOf``
     - Polymorphic field — multiple possible structures. See
       :ref:`oneOf fields <field-oneOf>`.
   * - ``required``
     - Fields that must be present (after the value/comment wrapper is
       stripped).

Format Code Quick Reference
-----------------------------

.. list-table::
   :header-rows: 1
   :widths: 10 20 12 25

   * - Code
     - C Type
     - Size
     - YAML Example
   * - ``B``
     - uint8_t
     - 1 byte
     - ``value: 255``
   * - ``b``
     - int8_t
     - 1 byte
     - ``value: -10``
   * - ``H``
     - uint16_t
     - 2 bytes
     - ``value: 65535``
   * - ``h``
     - int16_t
     - 2 bytes
     - ``value: -100``
   * - ``I``
     - uint32_t
     - 4 bytes
     - ``value: 100000``
   * - ``i``
     - int32_t
     - 4 bytes
     - ``value: -50000``
   * - ``f``
     - float
     - 4 bytes
     - ``value: 1.0``
   * - ``d``
     - double
     - 8 bytes
     - ``value: 3.14159``
   * - ``Q``
     - uint64_t
     - 8 bytes
     - ``value: 0``


6. Field Type Cookbook
======================

Different schema field types require different YAML patterns. Here's how to
write each one.

Simple Integer
--------------

The majority of PDR fields are integers.

**Schema:**

.. code-block:: json

   "sensorID": {
     "type": "integer",
     "minimum": 0,
     "maximum": 65535,
     "binaryFormat": "H"
   }

**YAML:**

.. code-block:: yaml

   sensorID:
     value: 258
     comment: "Unique sensor ID"

Floating Point
--------------

**Schema:**

.. code-block:: json

   "resolution": {
     "type": "number",
     "binaryFormat": "f"
   }

**YAML:**

.. code-block:: yaml

   resolution:
     value: 1.0

.. note::

   Integers are accepted for float fields (e.g., ``value: 100`` is
   auto-converted to ``100.0``).

Array of Integers
-----------------

Used for byte arrays (UUIDs, bitfields, raw data).

**Schema:**

.. code-block:: json

   "deviceUID": {
     "type": "array",
     "minItems": 16,
     "maxItems": 16,
     "items": {
       "type": "integer",
       "binaryFormat": "B"
     }
   }

**YAML:**

.. code-block:: yaml

   deviceUID:
     value: [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15]
     comment: "16-byte UUID"

Array of Objects
----------------

Used for composite sensors/effecters, entity lists, etc.

**Schema:**

.. code-block:: json

   "stateSensors": {
     "type": "array",
     "items": {
       "type": "object",
       "binaryOrder": ["stateSetID", "possibleStatesSize", "possibleStates"],
       "properties": { "..." }
     }
   }

**YAML:**

.. code-block:: yaml

   stateSensors:
     - stateSetID:
         value: 1
       possibleStatesSize:
         value: 1
       possibleStates:
         value: [3]
     - stateSetID:
         value: 2
       possibleStatesSize:
         value: 1
       possibleStates:
         value: [12]

.. warning::

   Each array element must include **every field** listed in the item's
   ``binaryOrder``. The count field (e.g., ``compositeSensorCount``) must
   match the actual array length.

Bitfield Arrays (possibleStates)
---------------------------------

State sensors and effecters use byte arrays where individual bits represent
supported states:

.. code-block:: yaml

   # Each byte = 8 states. Bit N set = state N supported.
   possibleStates:
     value: [3]       # 0b00000011 = supports states 0 and 1

   possibleStates:
     value: [12]      # 0b00001100 = supports states 2 and 3

   possibleStates:
     value: [255, 3]  # 0xFF 0x03 = supports states 0-9

String Fields
-------------

Used in auxiliary names, OEM state sets, and similar types.

**Schema:**

.. code-block:: json

   "sensorName": {
     "type": "string",
     "binaryFormat": "variable",
     "x-binary-encoding": "utf-16be",
     "x-binary-terminator": "0x0000"
   }

**YAML:**

.. code-block:: yaml

   sensorName:
     value: "Temp Sensor"

The encoding and terminator are handled automatically. You just write the
plain string.

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Schema Encoding
     - What Happens
   * - ``utf-8`` (default)
     - UTF-8 bytes + ``0x00`` terminator
   * - ``utf-16be``
     - UTF-16 big-endian bytes + ``0x00 0x00`` terminator
   * - ``us-ascii``
     - ASCII bytes + ``0x00`` terminator

.. _field-oneOf:

Polymorphic Fields (oneOf)
--------------------------

Some fields have multiple possible structures, selected by a discriminator
field with a ``const`` value.

Example: The ``locator`` field in Type 1 (Terminus Locator) can be a UID,
MCTP_EID, SMBus, etc. The ``terminusLocatorType`` field selects which variant:

.. code-block:: yaml

   # Variant 0: UID locator
   locator:
     terminusLocatorType:
       value: 0                # const: 0 in schema → selects UID variant
     terminusLocatorValueSize:
       value: 17
     terminusInstance:
       value: 1
     deviceUID:
       value: [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15]

   # Variant 1: MCTP_EID locator
   locator:
     terminusLocatorType:
       value: 1                # const: 1 in schema → selects MCTP_EID variant
     terminusLocatorValueSize:
       value: 1
     EID:
       value: 10

.. note::

   **How it works:** The generator checks each ``oneOf`` variant in the schema.
   It matches the variant where all ``const``-constrained fields match your
   YAML values. The matched variant's ``binaryOrder`` and field definitions are
   then used for packing.

Variable-Length Type-Mapped Fields
-----------------------------------

Some fields change their binary size depending on another field's value.

Example: In Type 2 (Numeric Sensor), ``hysteresis``, thresholds, and range
values all depend on ``sensorDataSize``:

**Schema:**

.. code-block:: json

   "hysteresis": {
     "binaryFormat": "variable",
     "x-binary-type-field": "sensorDataSize",
     "x-binary-type-mapping": {
       "0": "B",
       "1": "b",
       "2": "H",
       "3": "h",
       "4": "I",
       "5": "i",
       "6": "f"
     }
   }

**YAML:**

.. code-block:: yaml

   # sensorDataSize controls the type
   sensorDataSize:
     value: 1
     comment: "1 = sint8"

   # So hysteresis is packed as sint8
   hysteresis:
     value: 2

   # And thresholds are also sint8
   upperThresholdCritical:
     value: 100

.. tip::

   You don't need to worry about the binary format yourself. Just make sure the
   **controlling field** (e.g., ``sensorDataSize``) is set correctly, and the
   generator handles the rest. Ensure your values fit in the chosen type's
   range.


7. Complete Examples
=====================

Example A: Terminus Locator (Type 1) — Simple
-----------------------------------------------

The simplest PDR type. Fixed fields + a polymorphic locator.

.. code-block:: yaml

   # Terminus Locator PDR (Type 1)
   pdrHeader:
     recordHandle:
       value: auto
     PDRHeaderVersion:
       value: 1
     PDRType:
       value: 1
     recordChangeNumber:
       value: 0
     dataLength:
       value: auto

   PLDMTerminusHandle:
     value: 256
     comment: "Handle for this terminus"

   validity:
     value: 1
     comment: "1 = valid"

   TID:
     value: 5
     comment: "Terminus ID"

   containerID:
     value: 1

   locator:
     terminusLocatorType:
       value: 0
       comment: "0 = UID locator"
     terminusLocatorValueSize:
       value: 17
     terminusInstance:
       value: 1
     deviceUID:
       value: [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15]
       comment: "16-byte device UUID"

Example B: State Sensor (Type 4) — Arrays
-------------------------------------------

Demonstrates the array-of-objects pattern for composite sensors.

.. code-block:: yaml

   # State Sensor PDR (Type 4)
   pdrHeader:
     recordHandle:
       value: auto
     PDRHeaderVersion:
       value: 1
     PDRType:
       value: 4
     recordChangeNumber:
       value: 0
     dataLength:
       value: auto

   PLDMTerminusHandle:
     value: 256

   sensorID:
     value: 258

   entityType:
     value: 42

   entityInstanceNumber:
     value: 1

   containerID:
     value: 0

   sensorInit:
     value: 1

   sensorAuxiliaryNamesPDR:
     value: 0

   compositeSensorCount:
     value: 2             # ← Must match array length below!

   stateSensors:           # ← Array of 2 entries
     - stateSetID:
         value: 1
       possibleStatesSize:
         value: 1
       possibleStates:
         value: [3]
         comment: "States 0 and 1"
     - stateSetID:
         value: 2
       possibleStatesSize:
         value: 1
       possibleStates:
         value: [12]
         comment: "States 2 and 3"

Example C: Sensor Auxiliary Names (Type 6) — Strings
------------------------------------------------------

Demonstrates nested arrays with string fields.

.. code-block:: yaml

   # Sensor Auxiliary Names PDR (Type 6)
   pdrHeader:
     recordHandle:
       value: auto
     PDRHeaderVersion:
       value: 1
     PDRType:
       value: 6
     recordChangeNumber:
       value: 0
     dataLength:
       value: auto

   PLDMTerminusHandle:
     value: 256

   sensorID:
     value: 260

   sensorCount:
     value: 1

   auxNames:
     - nameStringCount:
         value: 1
       names:
         - nameLanguageTag:
             value: "en"           # ASCII null-terminated
           sensorName:
             value: "Temp Sensor"  # UTF-16BE null-terminated

Example D: OEM State Set (Type 8) — Deep Nesting
--------------------------------------------------

Demonstrates multi-level nesting with arrays of objects containing arrays of
objects.

.. code-block:: yaml

   # OEM State Set PDR (Type 8)
   pdrHeader:
     recordHandle:
       value: auto
     PDRHeaderVersion:
       value: 1
     PDRType:
       value: 8
     recordChangeNumber:
       value: 0
     dataLength:
       value: auto

   PLDMTerminusHandle:
     value: 256

   OEMStateSetIDHandle:
     value: 32968

   vendorIANA:
     value: 12345
     comment: "Vendor IANA Enterprise Number"

   OEMStateSetID:
     value: 32968

   unspecifiedValueHint:
     value: 0

   stateCount:
     value: 2

   stateValueRecords:                # Level 1: array of state records
     - minStateValue:
         value: 0
       maxStateValue:
         value: 0
       stringCount:
         value: 1
       stateNames:                    # Level 2: array of name pairs
         - stateLanguageTag:
             value: "en"              # ASCII
           stateName:
             value: "Idle"            # UTF-16BE
     - minStateValue:
         value: 1
       maxStateValue:
         value: 1
       stringCount:
         value: 1
       stateNames:
         - stateLanguageTag:
             value: "en"
           stateName:
             value: "Active"


8. Adding Macro Bindings
=========================

Macro bindings let you expose specific YAML field values as C ``#define``
constants in the generated header. This is optional but useful when firmware
code needs compile-time access to PDR values.

Where to Add
------------

Edit ``source/data/macro_defs.yaml``:

.. code-block:: yaml

   macros:
     # Existing entries...

     # Add yours here:
     - name: MY_SENSOR_ID         # C macro name
       file: "my_sensor.yaml"     # YAML filename (relative to data/)
       field: "sensorID.value"    # Field path to extract

Generated Output
-----------------

The above produces this line in ``pdr_generated.h``:

.. code-block:: c

   #define MY_SENSOR_ID  258

Field Path Syntax
------------------

.. list-table::
   :header-rows: 1
   :widths: 50 50

   * - Path
     - Resolves To
   * - ``sensorID.value``
     - ``data["sensorID"]["value"]`` → the integer
   * - ``pdrHeader.recordHandle.value``
     - The resolved record handle
   * - ``stateSensors[0].stateSetID.value``
     - First array element's stateSetID
   * - ``stateSensors[0].possibleStates.value[0]``
     - First byte of first sensor's bitfield

Rules
-----

- ``name`` must be a valid C identifier (letters, digits, underscores; no
  leading digit)
- ``file`` is relative to ``source/data/``
- ``field`` uses dot notation for keys and ``[N]`` for array indices
- Always include ``.value`` at the end to reach through the value/comment
  wrapper

.. warning::

   **Gotcha:** If the field path is wrong (typo, missing ``.value``, wrong
   array index), the generator prints a warning but continues. Your
   ``#define`` will be missing from the output. Always check the generated
   ``.h`` after adding macros.


9. Hidden Fields & Doc Metadata
================================

You can exclude specific array elements from Sphinx documentation while keeping
them in the binary output. This is useful for deprecated or internal-only
entries.

Usage
-----

.. code-block:: yaml

   containedEntities:
     - _doc:
         hidden: true           # Hidden from docs, still packed!
       containedEntityType:
         value: 10
         comment: "Internal use only"
       containedEntityInstanceNumber:
         value: 3
       containedEntityContainerID:
         value: 200
     - containedEntityType:          # No _doc → visible in docs
         value: 42
       containedEntityInstanceNumber:
         value: 1
       containedEntityContainerID:
         value: 123

.. important::

   ``_doc: {hidden: true}`` only affects Sphinx documentation rendering. The
   data is **always included** in the binary output. This is not a way to
   conditionally exclude data.


10. Validating Your File
=========================

Run the Generator
-----------------

.. code-block:: bash

   python3 source/code_gen.py source/data source/schema output/pdr_generated.c \
       --macros source/data/macro_defs.yaml

If your file is valid, you'll see it listed in the output alongside other PDRs.
If not, you'll get a clear error with the file path and field that failed.

What Gets Validated
--------------------

1. **Schema presence** — Does ``type_N.json`` exist for your ``PDRType``?
2. **Required fields** — Are all required fields present?
3. **Type constraints** — Is each value the correct JSON type (integer, string,
   array, etc.)?
4. **Range constraints** — Are values within ``minimum``/``maximum``?
5. **Const constraints** — Do discriminator fields match their expected
   ``const`` value?
6. **Array sizes** — Do arrays satisfy ``minItems``/``maxItems``?
7. **Handle uniqueness** — No duplicate explicit handles across files
8. **dataLength consistency** — Warning if stated value differs from calculated
   (auto-corrected)

Quick Sanity Check
-------------------

After generation, verify your record appears in the ``.h`` file:

.. code-block:: bash

   # Check for your PDR's macros
   grep MY_PDR output/pdr_generated.h

   # Check total count increased
   grep PDR_COUNT output/pdr_generated.h


11. Common Errors & Fixes
===========================

Missing ``value:`` wrapper
---------------------------

.. code-block:: yaml

   # WRONG
   sensorID: 256

   # RIGHT
   sensorID:
     value: 256

**Why:** The generator expects every field to be a dict with a ``value`` key. A
bare value causes a validation error or silent data corruption.

No matching schema
-------------------

::

   FileNotFoundError: source/schema/type_99.json

**Fix:** Check that ``pdrHeader.PDRType.value`` matches an existing schema. The
available types are the ``type_N.json`` files in ``source/schema/``.

Missing required field
-----------------------

::

   ValidationError: 'entityType' is a required property

**Fix:** Check the schema's ``binaryOrder`` and ``required`` arrays. Every
field listed must be present in your YAML.

Value out of range
-------------------

::

   ValidationError: 300 is greater than the maximum of 255

**Fix:** Check the schema's ``minimum``/``maximum`` for the field. For a
``"B"`` (uint8) field, values must be 0–255.

Array count mismatch
---------------------

.. code-block:: yaml

   # WRONG: compositeSensorCount says 1, but array has 2 entries
   compositeSensorCount:
     value: 1
   stateSensors:
     - stateSetID: { value: 1 }
       ...
     - stateSetID: { value: 2 }
       ...

**Fix:** The count field and the actual array length must agree. Set
``compositeSensorCount: {value: 2}``.

Duplicate record handle
------------------------

::

   WARNING: Duplicate recordHandle 5 in type_new.yaml (already used by type_3.yaml)

**Fix:** Use ``value: auto`` for ``recordHandle``, or pick a unique explicit
value.

oneOf variant not matched
--------------------------

If no ``oneOf`` variant matches your data's ``const`` fields, the generator
falls back to the first variant, which may silently produce wrong binary output.

**Fix:** Check that your discriminator field (e.g., ``terminusLocatorType``)
matches one of the ``const`` values defined in the schema's ``oneOf`` variants.

Wrong YAML indentation for arrays
-----------------------------------

.. code-block:: yaml

   # WRONG: value/comment inside list item misaligned
   stateSensors:
     - stateSetID:
       value: 1       # Parsed as sibling, not child!

   # RIGHT: value indented under stateSetID
   stateSensors:
     - stateSetID:
         value: 1     # Two more spaces → child of stateSetID

**Fix:** In YAML, indentation is significant. Inside an array element (``-``),
field wrappers must be indented further than the field name.

Macro field path not found
---------------------------

::

   WARNING: Cannot resolve field path 'sensorID' in type_2.yaml

**Fix:** Remember to include ``.value`` at the end of the path:
``"sensorID.value"``, not ``"sensorID"``.


12. Pre-Commit Checklist
==========================

Before committing a new YAML file, verify each item:

.. list-table::
   :header-rows: 1
   :widths: 5 40 55

   * - #
     - Check
     - How
   * - 1
     - File is in ``source/data/``
     - Any name is fine; ``type_N.yaml`` is convention
   * - 2
     - ``pdrHeader`` is present and complete
     - All 5 fields: recordHandle, PDRHeaderVersion, PDRType,
       recordChangeNumber, dataLength
   * - 3
     - ``PDRType`` matches an existing schema
     - Check ``source/schema/type_N.json`` exists
   * - 4
     - Every field uses ``value:`` wrapper
     - No bare values like ``sensorID: 256``
   * - 5
     - All ``binaryOrder`` fields are present
     - Compare your YAML against the schema's ``binaryOrder`` list
   * - 6
     - Array counts match actual arrays
     - ``compositeSensorCount`` == len(``stateSensors``)
   * - 7
     - Values are in range
     - Check ``minimum``/``maximum`` in schema
   * - 8
     - Generator runs without errors
     - ``python3 source/code_gen.py ...``
   * - 9
     - Record appears in generated ``.h``
     - ``PDR_HANDLE_*``, ``PDR_OFFSET_*``, ``PDR_SIZE_*``
   * - 10
     - Macro bindings resolve (if added)
     - Check ``#define`` lines in ``.h``

.. tip::

   **Quick template:** The fastest way to start is to copy an existing YAML
   file of the same type, change the field values, and set ``recordHandle``
   and ``dataLength`` to ``auto``.
