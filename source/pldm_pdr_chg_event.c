/**
 * @file pldm_pdr_chg_event.c
 * @brief pldmPDRRepositoryChgEvent — common + endpoint implementation
 *
 * Validation, encode/decode, and change tracker.
 * No dependency on pldm_pdr_mgr — can be compiled for endpoints.
 */

#include "pldm_pdr_chg_event.h"

/* ----------------------------------------------------------------
 * Validation (V1–V5 per DSP0248)
 * ---------------------------------------------------------------- */
int pdr_chg_event_validate(const pdr_chg_event_t *event)
{
    /* V1: refreshEntireRepository must have 0 change records */
    if (event->event_data_format == PDR_CHG_FORMAT_REFRESH_ENTIRE) {
        return (event->num_change_records == 0) ? 0 : -1;
    }

    /* V3 (implicit): format field is per-event, so types/handles
     * cannot be mixed.  Just validate the format value itself. */
    if (event->event_data_format != PDR_CHG_FORMAT_PDR_TYPES &&
        event->event_data_format != PDR_CHG_FORMAT_PDR_HANDLES) {
        return -1;
    }

    if (event->num_change_records > PDR_CHG_EVENT_MAX_RECORDS) {
        return -1;
    }

    uint8_t last_op = 0;

    for (uint8_t i = 0; i < event->num_change_records; i++) {
        const pdr_chg_record_t *rec = &event->change_records[i];

        /* V2: formatIsPDRHandles cannot use refreshAllRecords */
        if (event->event_data_format == PDR_CHG_FORMAT_PDR_HANDLES &&
            rec->event_data_operation == PDR_CHG_OP_REFRESH_ALL) {
            return -1;
        }

        /* Validate operation is in range */
        if (rec->event_data_operation > PDR_CHG_OP_RECORDS_MODIFIED) {
            return -1;
        }

        /* V4: ordering — each operation must be >= previous */
        if (i > 0 && rec->event_data_operation < last_op) {
            return -1;
        }
        last_op = rec->event_data_operation;

        /* V5: entry count must be within bounds */
        if (rec->num_change_entries > PDR_CHG_EVENT_MAX_ENTRIES) {
            return -1;
        }
    }

    return 0;
}

/* ----------------------------------------------------------------
 * Encoding (terminus side)
 *
 * Wire format:
 *   [eventDataFormat: 1]
 *   [numberOfChangeRecords: 1]
 *   for each changeRecord:
 *     [eventDataOperation: 1]
 *     [numberOfChangeEntries: 1]
 *     for each changeEntry:
 *       [uint32 LE: 4]
 * ---------------------------------------------------------------- */
int pdr_chg_event_encode(const pdr_chg_event_t *event,
                          uint8_t *buf, uint16_t buf_size,
                          uint16_t *encoded_len)
{
    if (pdr_chg_event_validate(event) != 0) {
        return -1;
    }

    uint16_t offset = 0;

    /* Header */
    if (offset + 2 > buf_size) {
        return -1;
    }
    buf[offset++] = event->event_data_format;
    buf[offset++] = event->num_change_records;

    /* Change records */
    for (uint8_t i = 0; i < event->num_change_records; i++) {
        const pdr_chg_record_t *rec = &event->change_records[i];

        if (offset + 2 > buf_size) {
            return -1;
        }
        buf[offset++] = rec->event_data_operation;
        buf[offset++] = rec->num_change_entries;

        for (uint8_t j = 0; j < rec->num_change_entries; j++) {
            if (offset + 4 > buf_size) {
                return -1;
            }
            uint32_t val = rec->change_entries[j];
            buf[offset++] = (uint8_t)(val & 0xFF);
            buf[offset++] = (uint8_t)((val >> 8) & 0xFF);
            buf[offset++] = (uint8_t)((val >> 16) & 0xFF);
            buf[offset++] = (uint8_t)((val >> 24) & 0xFF);
        }
    }

    *encoded_len = offset;
    return 0;
}

/* ----------------------------------------------------------------
 * Decoding
 * ---------------------------------------------------------------- */
int pdr_chg_event_decode(const uint8_t *buf, uint16_t buf_len,
                          pdr_chg_event_t *event)
{
    memset(event, 0, sizeof(*event));

    if (buf_len < 2) {
        return -1;
    }

    uint16_t offset = 0;

    event->event_data_format  = buf[offset++];
    event->num_change_records = buf[offset++];

    /* refreshEntireRepository — no change records expected */
    if (event->event_data_format == PDR_CHG_FORMAT_REFRESH_ENTIRE) {
        return (event->num_change_records == 0) ? 0 : -1;
    }

    if (event->num_change_records > PDR_CHG_EVENT_MAX_RECORDS) {
        return -1;
    }

    for (uint8_t i = 0; i < event->num_change_records; i++) {
        if (offset + 2 > buf_len) {
            return -1;
        }

        pdr_chg_record_t *rec = &event->change_records[i];
        rec->event_data_operation = buf[offset++];
        rec->num_change_entries   = buf[offset++];

        if (rec->num_change_entries > PDR_CHG_EVENT_MAX_ENTRIES) {
            return -1;
        }

        uint16_t entries_bytes = (uint16_t)rec->num_change_entries * 4;
        if (offset + entries_bytes > buf_len) {
            return -1;
        }

        for (uint8_t j = 0; j < rec->num_change_entries; j++) {
            rec->change_entries[j] =
                (uint32_t)buf[offset]             |
                ((uint32_t)buf[offset + 1] << 8)  |
                ((uint32_t)buf[offset + 2] << 16) |
                ((uint32_t)buf[offset + 3] << 24);
            offset += 4;
        }
    }

    return pdr_chg_event_validate(event);
}

/* ----------------------------------------------------------------
 * Change Tracker — terminus side
 * ---------------------------------------------------------------- */
void pdr_chg_tracker_init(pdr_chg_tracker_t *tracker)
{
    memset(tracker, 0, sizeof(*tracker));
    tracker->deletes.event_data_operation  = PDR_CHG_OP_RECORDS_DELETED;
    tracker->adds.event_data_operation     = PDR_CHG_OP_RECORDS_ADDED;
    tracker->modifies.event_data_operation = PDR_CHG_OP_RECORDS_MODIFIED;
}

int pdr_chg_tracker_record_add(pdr_chg_tracker_t *tracker, uint32_t entry)
{
    if (tracker->adds.num_change_entries >= PDR_CHG_EVENT_MAX_ENTRIES) {
        return -1;
    }
    tracker->adds.change_entries[tracker->adds.num_change_entries++] = entry;
    tracker->has_changes = true;
    return 0;
}

int pdr_chg_tracker_record_delete(pdr_chg_tracker_t *tracker, uint32_t entry)
{
    if (tracker->deletes.num_change_entries >= PDR_CHG_EVENT_MAX_ENTRIES) {
        return -1;
    }
    tracker->deletes.change_entries[tracker->deletes.num_change_entries++] = entry;
    tracker->has_changes = true;
    return 0;
}

int pdr_chg_tracker_record_modify(pdr_chg_tracker_t *tracker, uint32_t entry)
{
    if (tracker->modifies.num_change_entries >= PDR_CHG_EVENT_MAX_ENTRIES) {
        return -1;
    }
    tracker->modifies.change_entries[tracker->modifies.num_change_entries++] = entry;
    tracker->has_changes = true;
    return 0;
}

/** Calculate the wire-encoded size of a change event. */
static uint16_t calc_encoded_size(const pdr_chg_event_t *event)
{
    uint16_t size = 2; /* format + num_records */
    for (uint8_t i = 0; i < event->num_change_records; i++) {
        size += 2; /* operation + num_entries */
        size += (uint16_t)event->change_records[i].num_change_entries * 4;
    }
    return size;
}

int pdr_chg_tracker_build_event(const pdr_chg_tracker_t *tracker,
                                 pdr_chg_event_t *event,
                                 uint8_t format,
                                 uint16_t max_msg_size)
{
    memset(event, 0, sizeof(*event));

    if (!tracker->has_changes) {
        event->event_data_format = PDR_CHG_FORMAT_REFRESH_ENTIRE;
        return 0;
    }

    event->event_data_format = format;

    /* Compose change records in required order (V4):
     * deletes -> adds -> modifies */
    if (tracker->deletes.num_change_entries > 0) {
        if (event->num_change_records >= PDR_CHG_EVENT_MAX_RECORDS) {
            goto fallback;
        }
        event->change_records[event->num_change_records++] = tracker->deletes;
    }

    if (tracker->adds.num_change_entries > 0) {
        if (event->num_change_records >= PDR_CHG_EVENT_MAX_RECORDS) {
            goto fallback;
        }
        event->change_records[event->num_change_records++] = tracker->adds;
    }

    if (tracker->modifies.num_change_entries > 0) {
        if (event->num_change_records >= PDR_CHG_EVENT_MAX_RECORDS) {
            goto fallback;
        }
        event->change_records[event->num_change_records++] = tracker->modifies;
    }

    /* V6: size check — fall back if exceeds MTU */
    if (max_msg_size > 0 && calc_encoded_size(event) > max_msg_size) {
        goto fallback;
    }

    return 0;

fallback:
    memset(event, 0, sizeof(*event));
    event->event_data_format  = PDR_CHG_FORMAT_REFRESH_ENTIRE;
    event->num_change_records = 0;
    return 0;
}

void pdr_chg_tracker_clear(pdr_chg_tracker_t *tracker)
{
    pdr_chg_tracker_init(tracker);
}
