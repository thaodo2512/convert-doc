/**
 * @file pldm_pdr_chg_event.h
 * @brief pldmPDRRepositoryChgEvent — DSP0248 §16.14 (endpoint / common)
 *
 * Standalone types and helpers for the PDR repository change event.
 * No dependency on pldm_pdr_mgr.h — safe to use on endpoints that
 * only have pldm_pdr_repo.
 *
 * Provides:
 *   - Data structures (enums, changeRecord, event)
 *   - Validation (V1–V5)
 *   - Encode / decode (wire ↔ struct)
 *   - Change tracker (terminus side: accumulate + build event)
 *
 * For the manager-side event handler, see pldm_pdr_chg_event_handler.h.
 */

#ifndef PLDM_PDR_CHG_EVENT_H
#define PLDM_PDR_CHG_EVENT_H

#include <stdint.h>
#include <stdbool.h>
#include <string.h>

/* ----------------------------------------------------------------
 * Configuration
 * ---------------------------------------------------------------- */
#define PDR_CHG_EVENT_MAX_ENTRIES       16   /* Max changeEntry per record */
#define PDR_CHG_EVENT_MAX_RECORDS       4    /* Max changeRecords per event */
#define PDR_CHG_EVENT_DEFAULT_MTU       64   /* MCTP baseline payload size */

/* ----------------------------------------------------------------
 * eventDataFormat (DSP0248 Table 23)
 * ---------------------------------------------------------------- */
typedef enum {
    PDR_CHG_FORMAT_REFRESH_ENTIRE = 0x00,
    PDR_CHG_FORMAT_PDR_TYPES      = 0x01,
    PDR_CHG_FORMAT_PDR_HANDLES    = 0x02,
} pdr_chg_event_format_t;

/* ----------------------------------------------------------------
 * eventDataOperation (DSP0248 Table 24)
 * ---------------------------------------------------------------- */
typedef enum {
    PDR_CHG_OP_REFRESH_ALL      = 0x00,  /* Only valid with PDR_TYPES */
    PDR_CHG_OP_RECORDS_DELETED  = 0x01,
    PDR_CHG_OP_RECORDS_ADDED    = 0x02,
    PDR_CHG_OP_RECORDS_MODIFIED = 0x03,
} pdr_chg_event_op_t;

/* ----------------------------------------------------------------
 * changeRecord (DSP0248 Table 24)
 * ---------------------------------------------------------------- */
typedef struct {
    uint8_t  event_data_operation;   /* pdr_chg_event_op_t                 */
    uint8_t  num_change_entries;     /* Number of entries that follow      */
    uint32_t change_entries[PDR_CHG_EVENT_MAX_ENTRIES];
} pdr_chg_record_t;

/* ----------------------------------------------------------------
 * pldmPDRRepositoryChgEvent (DSP0248 Table 23)
 * ---------------------------------------------------------------- */
typedef struct {
    uint8_t          event_data_format;   /* pdr_chg_event_format_t        */
    uint8_t          num_change_records;  /* 0 if refreshEntireRepository  */
    pdr_chg_record_t change_records[PDR_CHG_EVENT_MAX_RECORDS];
} pdr_chg_event_t;

/* ----------------------------------------------------------------
 * Change Tracker (terminus side)
 *
 * Accumulates PDR changes as they happen. When ready, call
 * pdr_chg_tracker_build_event() to compose the event message.
 * ---------------------------------------------------------------- */
typedef struct {
    pdr_chg_record_t deletes;        /* Pending recordsDeleted entries     */
    pdr_chg_record_t adds;           /* Pending recordsAdded entries       */
    pdr_chg_record_t modifies;       /* Pending recordsModified entries    */
    bool has_changes;
} pdr_chg_tracker_t;

/* ----------------------------------------------------------------
 * API: Validation (both sides)
 * ---------------------------------------------------------------- */

/**
 * @brief Validate a change event against DSP0248 rules V1-V5.
 * @return 0 if valid, -1 on constraint violation
 */
int pdr_chg_event_validate(const pdr_chg_event_t *event);

/* ----------------------------------------------------------------
 * API: Encoding (terminus side)
 * ---------------------------------------------------------------- */

/**
 * @brief Encode a change event into wire format (little-endian).
 *
 * Validates the event before encoding.
 *
 * @param event        Event to encode
 * @param buf          Output buffer
 * @param buf_size     Output buffer capacity
 * @param[out] encoded_len  Actual encoded length
 * @return 0 on success, -1 on validation error or buffer overflow
 */
int pdr_chg_event_encode(const pdr_chg_event_t *event,
                          uint8_t *buf, uint16_t buf_size,
                          uint16_t *encoded_len);

/* ----------------------------------------------------------------
 * API: Decoding (both sides)
 * ---------------------------------------------------------------- */

/**
 * @brief Decode wire-format event data into a pdr_chg_event_t.
 *
 * Validates the result after parsing.
 *
 * @param buf       Input buffer (received event data)
 * @param buf_len   Input buffer length
 * @param[out] event  Decoded event
 * @return 0 on success, -1 on parse error or validation failure
 */
int pdr_chg_event_decode(const uint8_t *buf, uint16_t buf_len,
                          pdr_chg_event_t *event);

/* ----------------------------------------------------------------
 * API: Change Tracker (terminus side)
 * ---------------------------------------------------------------- */

/** Initialize / reset the change tracker. */
void pdr_chg_tracker_init(pdr_chg_tracker_t *tracker);

/** Record a PDR addition (entry = handle or PDR type). */
int pdr_chg_tracker_record_add(pdr_chg_tracker_t *tracker, uint32_t entry);

/** Record a PDR deletion. */
int pdr_chg_tracker_record_delete(pdr_chg_tracker_t *tracker, uint32_t entry);

/** Record a PDR modification. */
int pdr_chg_tracker_record_modify(pdr_chg_tracker_t *tracker, uint32_t entry);

/**
 * @brief Build a change event from accumulated tracker state.
 *
 * Composes change records in the required order (deletes -> adds -> modifies).
 * If the encoded size would exceed max_msg_size, falls back to
 * refreshEntireRepository.
 *
 * @param tracker       Change tracker
 * @param[out] event    Composed event
 * @param format        Desired format (PDR_CHG_FORMAT_PDR_HANDLES or _TYPES)
 * @param max_msg_size  Max wire size (0 = no limit)
 * @return 0 on success
 */
int pdr_chg_tracker_build_event(const pdr_chg_tracker_t *tracker,
                                 pdr_chg_event_t *event,
                                 uint8_t format,
                                 uint16_t max_msg_size);

/** Clear all tracked changes (same as re-init). */
void pdr_chg_tracker_clear(pdr_chg_tracker_t *tracker);

#endif /* PLDM_PDR_CHG_EVENT_H */
