/**
 * @file pldm_pdr_chg_event_handler.c
 * @brief pldmPDRRepositoryChgEvent — manager-side event handler
 *
 * Processes decoded change events and applies incremental updates
 * to the PDR manager's consolidated repository. On any error during
 * incremental processing, falls back to a full re-sync.
 */

#include "pldm_pdr_chg_event_handler.h"

/* ----------------------------------------------------------------
 * Internal: process recordsDeleted (handle-based)
 *
 * For each remote handle in the change record, look up the
 * corresponding local (remapped) handle and remove it from
 * the consolidated repo.
 * ---------------------------------------------------------------- */
static int handle_deletes(pdr_mgr_t *mgr, pdr_mgr_terminus_t *term,
                           const pdr_chg_record_t *rec)
{
    for (uint8_t i = 0; i < rec->num_change_entries; i++) {
        uint32_t remote_handle = rec->change_entries[i];
        uint32_t local_handle;

        if (pdr_mgr_find_handle_mapping(term, remote_handle,
                                         &local_handle) != 0) {
            continue; /* Unknown remote handle — skip */
        }

        pdr_repo_remove_record(&mgr->repo, local_handle);
        pdr_mgr_remove_handle_mapping(term, remote_handle);

        if (term->local_record_count > 0) {
            term->local_record_count--;
        }
    }

    return 0;
}

/* ----------------------------------------------------------------
 * Internal: process recordsAdded (handle-based)
 *
 * For each remote handle, fetch the PDR from the terminus,
 * assign a new remapped handle, and add to the consolidated repo.
 * ---------------------------------------------------------------- */
static int handle_adds(pdr_mgr_t *mgr, pdr_mgr_terminus_t *term,
                        uint8_t terminus_idx,
                        const pdr_chg_record_t *rec)
{
    for (uint8_t i = 0; i < rec->num_change_entries; i++) {
        uint32_t remote_handle = rec->change_entries[i];

        /* Fetch the specific PDR from the remote terminus */
        if (pdr_mgr_fetch_pdr_by_handle(mgr, term, remote_handle) != 0) {
            return -1;
        }

        if (term->fetch_ctx.reassembly_len < sizeof(pldm_pdr_hdr_t)) {
            return -1;
        }

        const pldm_pdr_hdr_t *pdr_hdr =
            (const pldm_pdr_hdr_t *)term->fetch_ctx.reassembly_buf;

        /* Allocate a new remapped handle */
        uint32_t remapped = pdr_mgr_remap_handle(terminus_idx,
                                                   term->local_handle_seq++);

        int rc = pdr_mgr_add_remapped_pdr(
            mgr, remapped, pdr_hdr->pdr_type,
            term->fetch_ctx.reassembly_buf + sizeof(pldm_pdr_hdr_t),
            pdr_hdr->data_length);
        if (rc != 0) {
            return -1;
        }

        pdr_mgr_add_handle_mapping(term, remote_handle, remapped);
        term->local_record_count++;
    }

    return 0;
}

/* ----------------------------------------------------------------
 * Internal: process recordsModified (handle-based)
 *
 * Remove old record -> fetch updated PDR -> re-add with the
 * same local handle so the mapping stays consistent.
 * ---------------------------------------------------------------- */
static int handle_modifies(pdr_mgr_t *mgr, pdr_mgr_terminus_t *term,
                            const pdr_chg_record_t *rec)
{
    for (uint8_t i = 0; i < rec->num_change_entries; i++) {
        uint32_t remote_handle = rec->change_entries[i];
        uint32_t local_handle;

        if (pdr_mgr_find_handle_mapping(term, remote_handle,
                                         &local_handle) != 0) {
            continue; /* Unknown remote handle — skip */
        }

        /* Remove old record from consolidated repo */
        pdr_repo_remove_record(&mgr->repo, local_handle);

        /* Fetch updated PDR from terminus */
        if (pdr_mgr_fetch_pdr_by_handle(mgr, term, remote_handle) != 0) {
            pdr_mgr_remove_handle_mapping(term, remote_handle);
            if (term->local_record_count > 0) {
                term->local_record_count--;
            }
            return -1;
        }

        if (term->fetch_ctx.reassembly_len < sizeof(pldm_pdr_hdr_t)) {
            pdr_mgr_remove_handle_mapping(term, remote_handle);
            if (term->local_record_count > 0) {
                term->local_record_count--;
            }
            return -1;
        }

        const pldm_pdr_hdr_t *pdr_hdr =
            (const pldm_pdr_hdr_t *)term->fetch_ctx.reassembly_buf;

        /* Re-add with the SAME local handle to preserve the mapping */
        int rc = pdr_mgr_add_remapped_pdr(
            mgr, local_handle, pdr_hdr->pdr_type,
            term->fetch_ctx.reassembly_buf + sizeof(pldm_pdr_hdr_t),
            pdr_hdr->data_length);
        if (rc != 0) {
            pdr_mgr_remove_handle_mapping(term, remote_handle);
            if (term->local_record_count > 0) {
                term->local_record_count--;
            }
            return -1;
        }

        /* Handle map entry unchanged — same remote & local handles */
    }

    return 0;
}

/* ----------------------------------------------------------------
 * Main Event Handler
 * ---------------------------------------------------------------- */
int pdr_chg_event_handle(pdr_mgr_t *mgr, uint8_t eid,
                          const uint8_t *event_data, uint16_t event_data_len)
{
    pdr_chg_event_t event;
    int rc;

    rc = pdr_chg_event_decode(event_data, event_data_len, &event);
    if (rc != 0) {
        return -1;
    }

    /* refreshEntireRepository or type-based: full re-sync */
    if (event.event_data_format == PDR_CHG_FORMAT_REFRESH_ENTIRE ||
        event.event_data_format == PDR_CHG_FORMAT_PDR_TYPES) {
        return pdr_mgr_sync_terminus(mgr, eid);
    }

    /* Handle-based incremental update */
    pdr_mgr_terminus_t *term = pdr_mgr_find_terminus(mgr, eid);
    if (!term) {
        return -1;
    }

    /* Compute terminus index from pointer offset into the array */
    uint8_t terminus_idx = (uint8_t)(term - mgr->termini);

    for (uint8_t i = 0; i < event.num_change_records; i++) {
        const pdr_chg_record_t *rec = &event.change_records[i];

        switch (rec->event_data_operation) {
        case PDR_CHG_OP_RECORDS_DELETED:
            rc = handle_deletes(mgr, term, rec);
            break;

        case PDR_CHG_OP_RECORDS_ADDED:
            rc = handle_adds(mgr, term, terminus_idx, rec);
            break;

        case PDR_CHG_OP_RECORDS_MODIFIED:
            rc = handle_modifies(mgr, term, rec);
            break;

        default:
            /* refreshAllRecords with handles should not pass validation */
            rc = -1;
            break;
        }

        if (rc != 0) {
            /* Incremental update failed — fall back to full re-sync */
            return pdr_mgr_sync_terminus(mgr, eid);
        }
    }

    return 0;
}
