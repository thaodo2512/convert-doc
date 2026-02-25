/**
 * @file pldm_pdr_mgr.c
 * @brief PLDM PDR Manager Implementation
 *
 * Manager role: discovers remote termini, fetches their PDRs,
 * remaps handles into non-overlapping ranges, and builds a
 * consolidated PDR repository.
 */

#include "pldm_pdr_mgr.h"

/* ----------------------------------------------------------------
 * Internal: find terminus slot index by EID
 * ---------------------------------------------------------------- */
static int find_terminus_idx(const pdr_mgr_t *mgr, uint8_t eid)
{
    for (int i = 0; i < PDR_MGR_MAX_TERMINI; i++) {
        if (mgr->termini[i].state != PDR_MGR_TERMINUS_UNUSED &&
            mgr->termini[i].eid == eid) {
            return i;
        }
    }
    return -1;
}

/* ----------------------------------------------------------------
 * Internal: transport send/receive wrapper
 * ---------------------------------------------------------------- */
static int mgr_send_recv(pdr_mgr_t *mgr, uint8_t eid, uint8_t command,
                          const uint8_t *req, uint16_t req_len,
                          uint8_t *resp, uint16_t *resp_len)
{
    if (!mgr->transport.send_recv) {
        return -1;
    }
    return mgr->transport.send_recv(eid, PLDM_TYPE_PLATFORM, command,
                                     req, req_len, resp, resp_len,
                                     mgr->transport.ctx);
}

/* ----------------------------------------------------------------
 * Initialization
 * ---------------------------------------------------------------- */
void pdr_mgr_init(pdr_mgr_t *mgr, const pdr_mgr_transport_t *transport)
{
    memset(mgr, 0, sizeof(*mgr));
    pdr_repo_init_ext(&mgr->repo, mgr->repo_blob, sizeof(mgr->repo_blob));
    if (transport) {
        mgr->transport = *transport;
    }
}

/* ----------------------------------------------------------------
 * Handle Remapping
 *
 * terminus_idx 0 -> handles 0x10001, 0x10002, ...
 * terminus_idx 1 -> handles 0x20001, 0x20002, ...
 * ---------------------------------------------------------------- */
uint32_t pdr_mgr_remap_handle(uint8_t terminus_idx, uint16_t seq)
{
    return ((uint32_t)(terminus_idx + 1) << PDR_MGR_HANDLE_RANGE_SHIFT) |
           (uint32_t)(seq & PDR_MGR_HANDLE_SUB_MASK);
}

/* ----------------------------------------------------------------
 * Terminus Management
 * ---------------------------------------------------------------- */
int pdr_mgr_add_terminus(pdr_mgr_t *mgr, uint8_t eid,
                          uint16_t terminus_handle, uint8_t tid,
                          uint8_t *index_out)
{
    /* Reject duplicates */
    if (find_terminus_idx(mgr, eid) >= 0) {
        return -1;
    }

    /* Find a free slot */
    for (int i = 0; i < PDR_MGR_MAX_TERMINI; i++) {
        if (mgr->termini[i].state == PDR_MGR_TERMINUS_UNUSED) {
            memset(&mgr->termini[i], 0, sizeof(pdr_mgr_terminus_t));
            mgr->termini[i].state           = PDR_MGR_TERMINUS_DISCOVERED;
            mgr->termini[i].eid             = eid;
            mgr->termini[i].tid             = tid;
            mgr->termini[i].terminus_handle  = terminus_handle;
            mgr->termini[i].local_handle_seq = 1;
            if (index_out) {
                *index_out = (uint8_t)i;
            }
            return 0;
        }
    }

    return -1; /* No free slot */
}

int pdr_mgr_remove_terminus(pdr_mgr_t *mgr, uint8_t eid)
{
    int idx = find_terminus_idx(mgr, eid);
    if (idx < 0) {
        return -1;
    }

    pdr_mgr_purge_terminus_pdrs(mgr, (uint8_t)idx);
    mgr->termini[idx].state = PDR_MGR_TERMINUS_UNUSED;
    return 0;
}

pdr_mgr_terminus_t *pdr_mgr_find_terminus(pdr_mgr_t *mgr, uint8_t eid)
{
    int idx = find_terminus_idx(mgr, eid);
    if (idx < 0) {
        return NULL;
    }
    return &mgr->termini[idx];
}

int pdr_mgr_get_terminus_state(const pdr_mgr_t *mgr, uint8_t eid,
                                pdr_mgr_terminus_state_t *state)
{
    int idx = find_terminus_idx(mgr, eid);
    if (idx < 0) {
        return -1;
    }
    *state = mgr->termini[idx].state;
    return 0;
}

/* ----------------------------------------------------------------
 * Fetch Repository Info (0x50 + 0x53)
 *
 * Sends GetPDRRepositoryInfo to get record_count and repo_size,
 * then attempts GetPDRRepositorySignature. Falls back to a
 * pseudo-signature if the endpoint doesn't support 0x53.
 * ---------------------------------------------------------------- */
int pdr_mgr_fetch_repo_info(pdr_mgr_t *mgr, pdr_mgr_terminus_t *term)
{
    uint8_t resp_buf[64];
    uint16_t resp_len;
    int rc;

    /* --- GetPDRRepositoryInfo (0x50) — no request payload --- */
    resp_len = sizeof(resp_buf);
    rc = mgr_send_recv(mgr, term->eid, PLDM_PLATFORM_CMD_GET_PDR_REPO_INFO,
                        NULL, 0, resp_buf, &resp_len);
    if (rc != 0) {
        return -1;
    }

    if (resp_len < sizeof(pdr_mgr_get_repo_info_resp_t)) {
        return -1;
    }

    const pdr_mgr_get_repo_info_resp_t *info =
        (const pdr_mgr_get_repo_info_resp_t *)resp_buf;

    if (info->completion_code != PLDM_CC_SUCCESS) {
        return -1;
    }

    term->remote_record_count = info->record_count;
    term->remote_repo_size    = info->repository_size;

    /* --- GetPDRRepositorySignature (0x53) — optional --- */
    resp_len = sizeof(resp_buf);
    rc = mgr_send_recv(mgr, term->eid,
                        PLDM_PLATFORM_CMD_GET_PDR_REPO_SIGNATURE,
                        NULL, 0, resp_buf, &resp_len);

    if (rc == 0 && resp_len >= sizeof(pdr_mgr_get_pdr_sig_resp_t)) {
        const pdr_mgr_get_pdr_sig_resp_t *sig =
            (const pdr_mgr_get_pdr_sig_resp_t *)resp_buf;
        if (sig->completion_code == PLDM_CC_SUCCESS) {
            term->last_signature = sig->signature;
            return 0;
        }
    }

    /* Fallback: pseudo-signature from record_count XOR shifted repo_size */
    term->last_signature = term->remote_record_count ^
                           (term->remote_repo_size << 16);
    return 0;
}

/* ----------------------------------------------------------------
 * Fetch One PDR (with multi-part reassembly)
 *
 * Uses fetch_ctx.next_record_handle as the record to fetch.
 * Loops over GetPDR (0x51) chunks until transfer is complete.
 * Result lands in fetch_ctx.reassembly_buf[0..reassembly_len-1].
 * Updates fetch_ctx.next_record_handle for the next record.
 * ---------------------------------------------------------------- */
int pdr_mgr_fetch_one_pdr(pdr_mgr_t *mgr, pdr_mgr_terminus_t *term)
{
    pdr_mgr_fetch_ctx_t *ctx = &term->fetch_ctx;
    uint8_t resp_buf[sizeof(pdr_mgr_get_pdr_resp_t) + PDR_TRANSFER_CHUNK_SIZE];
    uint16_t resp_len;
    int rc;

    ctx->reassembly_len = 0;

    pdr_mgr_get_pdr_req_t req = {
        .record_handle        = ctx->next_record_handle,
        .data_transfer_handle = 0,
        .transfer_op_flag     = PLDM_TRANSFER_OP_GET_FIRST_PART,
        .request_count        = PDR_TRANSFER_CHUNK_SIZE,
        .record_change_num    = 0,
    };

    for (;;) {
        resp_len = sizeof(resp_buf);
        rc = mgr_send_recv(mgr, term->eid, PLDM_PLATFORM_CMD_GET_PDR,
                            (const uint8_t *)&req, sizeof(req),
                            resp_buf, &resp_len);
        if (rc != 0) {
            return -1;
        }

        if (resp_len < sizeof(pdr_mgr_get_pdr_resp_t)) {
            return -1;
        }

        const pdr_mgr_get_pdr_resp_t *resp =
            (const pdr_mgr_get_pdr_resp_t *)resp_buf;

        if (resp->completion_code != PLDM_CC_SUCCESS) {
            return -1;
        }

        uint16_t chunk_len = resp->response_count;

        /* Validate response contains the advertised data */
        if (resp_len < sizeof(pdr_mgr_get_pdr_resp_t) + chunk_len) {
            return -1;
        }

        /* Check reassembly buffer capacity */
        if ((ctx->reassembly_len + chunk_len) > PDR_MGR_REASSEMBLY_BUF_SIZE) {
            return -1;
        }

        /* Append chunk to reassembly buffer */
        const uint8_t *chunk_data =
            resp_buf + sizeof(pdr_mgr_get_pdr_resp_t);
        memcpy(&ctx->reassembly_buf[ctx->reassembly_len],
               chunk_data, chunk_len);
        ctx->reassembly_len += chunk_len;

        /* Check if this was the last chunk */
        if (resp->transfer_flag == PLDM_TRANSFER_FLAG_END ||
            resp->transfer_flag == PLDM_TRANSFER_FLAG_START_AND_END) {
            ctx->next_record_handle = resp->next_record_handle;
            ctx->records_fetched++;
            return 0;
        }

        /* More chunks needed */
        req.data_transfer_handle = resp->next_data_transfer_handle;
        req.transfer_op_flag     = PLDM_TRANSFER_OP_GET_NEXT_PART;
    }
}

/* ----------------------------------------------------------------
 * Add Remapped PDR to Consolidated Repo
 *
 * Temporarily overrides the repo's handle allocator to force the
 * remapped handle, then restores it.
 * ---------------------------------------------------------------- */
int pdr_mgr_add_remapped_pdr(pdr_mgr_t *mgr, uint32_t remapped_handle,
                              uint8_t pdr_type, const void *data,
                              uint16_t data_len)
{
    uint32_t saved_handle = mgr->repo.next_record_handle;
    mgr->repo.next_record_handle = remapped_handle;

    int rc = pdr_repo_add_record(&mgr->repo, pdr_type, data, data_len, NULL);

    /* Restore — remapped handles live in separate ranges */
    mgr->repo.next_record_handle = saved_handle;

    return rc;
}

/* ----------------------------------------------------------------
 * Purge All PDRs From a Terminus
 *
 * Identifies records by their handle range and removes them.
 * Iterates backwards to avoid index shift issues during removal.
 * ---------------------------------------------------------------- */
int pdr_mgr_purge_terminus_pdrs(pdr_mgr_t *mgr, uint8_t terminus_idx)
{
    uint32_t range_base = (uint32_t)(terminus_idx + 1)
                          << PDR_MGR_HANDLE_RANGE_SHIFT;
    uint32_t range_end  = range_base | PDR_MGR_HANDLE_SUB_MASK;

    for (int i = (int)mgr->repo.count - 1; i >= 0; i--) {
        uint32_t h = mgr->repo.index[i].record_handle;
        if (h >= range_base && h <= range_end) {
            pdr_repo_remove_record(&mgr->repo, h);
        }
    }

    return 0;
}

/* ----------------------------------------------------------------
 * Handle Map Helpers
 *
 * Track remote → local handle mappings for incremental updates.
 * ---------------------------------------------------------------- */
int pdr_mgr_find_handle_mapping(const pdr_mgr_terminus_t *term,
                                 uint32_t remote_handle,
                                 uint32_t *local_handle)
{
    for (uint16_t i = 0; i < term->handle_map_count; i++) {
        if (term->handle_map[i].remote_handle == remote_handle) {
            *local_handle = term->handle_map[i].local_handle;
            return 0;
        }
    }
    return -1;
}

int pdr_mgr_add_handle_mapping(pdr_mgr_terminus_t *term,
                                uint32_t remote_handle,
                                uint32_t local_handle)
{
    if (term->handle_map_count >= PDR_MAX_RECORD_COUNT) {
        return -1;
    }
    term->handle_map[term->handle_map_count].remote_handle = remote_handle;
    term->handle_map[term->handle_map_count].local_handle  = local_handle;
    term->handle_map_count++;
    return 0;
}

int pdr_mgr_remove_handle_mapping(pdr_mgr_terminus_t *term,
                                   uint32_t remote_handle)
{
    for (uint16_t i = 0; i < term->handle_map_count; i++) {
        if (term->handle_map[i].remote_handle == remote_handle) {
            for (uint16_t j = i; j < term->handle_map_count - 1; j++) {
                term->handle_map[j] = term->handle_map[j + 1];
            }
            term->handle_map_count--;
            return 0;
        }
    }
    return -1;
}

/* ----------------------------------------------------------------
 * Fetch PDR By Specific Handle
 *
 * Sets up the fetch context for a targeted fetch and delegates
 * to fetch_one_pdr. Result is in fetch_ctx.reassembly_buf.
 * ---------------------------------------------------------------- */
int pdr_mgr_fetch_pdr_by_handle(pdr_mgr_t *mgr, pdr_mgr_terminus_t *term,
                                 uint32_t remote_handle)
{
    term->fetch_ctx.next_record_handle = remote_handle;
    return pdr_mgr_fetch_one_pdr(mgr, term);
}

/* ----------------------------------------------------------------
 * Sync Terminus
 *
 * Full synchronization sequence:
 *   1. Fetch repo info + signature
 *   2. Compare signature — skip if unchanged
 *   3. Purge previously-synced PDRs
 *   4. Fetch all PDRs with multi-part reassembly
 *   5. Remap handles and add to consolidated repo
 *   6. Update state to SYNCED
 * ---------------------------------------------------------------- */
int pdr_mgr_sync_terminus(pdr_mgr_t *mgr, uint8_t eid)
{
    int idx = find_terminus_idx(mgr, eid);
    if (idx < 0) {
        return -1;
    }

    pdr_mgr_terminus_t *term = &mgr->termini[idx];
    uint32_t old_sig   = term->last_signature;
    bool     was_synced = (term->state == PDR_MGR_TERMINUS_SYNCED ||
                           term->state == PDR_MGR_TERMINUS_STALE);

    term->state = PDR_MGR_TERMINUS_SYNCING;

    /* Step 1: Fetch remote repo info + signature */
    int rc = pdr_mgr_fetch_repo_info(mgr, term);
    if (rc != 0) {
        term->state = PDR_MGR_TERMINUS_ERROR;
        return -1;
    }

    /* Step 2: Skip if signature unchanged */
    if (was_synced && old_sig != 0 && term->last_signature == old_sig) {
        term->state = PDR_MGR_TERMINUS_SYNCED;
        return 0;
    }

    /* Step 3: Purge old PDRs from this terminus */
    pdr_mgr_purge_terminus_pdrs(mgr, (uint8_t)idx);
    term->local_handle_seq   = 1;
    term->local_record_count = 0;
    term->handle_map_count   = 0;

    /* Step 4: Fetch all PDRs */
    term->fetch_ctx.next_record_handle = 0; /* Start from first record */
    term->fetch_ctx.records_fetched    = 0;
    term->fetch_ctx.retries            = 0;

    for (uint32_t r = 0; r < term->remote_record_count; r++) {
        rc = pdr_mgr_fetch_one_pdr(mgr, term);
        if (rc != 0) {
            term->state = PDR_MGR_TERMINUS_ERROR;
            return -1;
        }

        /* Validate reassembled PDR has at least a header */
        if (term->fetch_ctx.reassembly_len < sizeof(pldm_pdr_hdr_t)) {
            term->state = PDR_MGR_TERMINUS_ERROR;
            return -1;
        }

        /* Step 5: Parse header, remap handle, add to consolidated repo */
        const pldm_pdr_hdr_t *pdr_hdr =
            (const pldm_pdr_hdr_t *)term->fetch_ctx.reassembly_buf;

        uint32_t remapped = pdr_mgr_remap_handle((uint8_t)idx,
                                                  term->local_handle_seq++);

        const uint8_t *pdr_data =
            term->fetch_ctx.reassembly_buf + sizeof(pldm_pdr_hdr_t);
        uint16_t pdr_data_len = pdr_hdr->data_length;

        rc = pdr_mgr_add_remapped_pdr(mgr, remapped, pdr_hdr->pdr_type,
                                       pdr_data, pdr_data_len);
        if (rc != 0) {
            term->state = PDR_MGR_TERMINUS_ERROR;
            return -1;
        }

        term->local_record_count++;

        /* Record remote → local handle mapping for incremental updates */
        pdr_mgr_add_handle_mapping(term, pdr_hdr->record_handle, remapped);

        /* next_record_handle == 0 means no more records on this terminus */
        if (term->fetch_ctx.next_record_handle == 0) {
            break;
        }
    }

    /* Step 6: Done */
    term->state = PDR_MGR_TERMINUS_SYNCED;
    return 0;
}

/* ----------------------------------------------------------------
 * Sync All
 * ---------------------------------------------------------------- */
int pdr_mgr_sync_all(pdr_mgr_t *mgr)
{
    int errors = 0;

    for (int i = 0; i < PDR_MGR_MAX_TERMINI; i++) {
        if (mgr->termini[i].state == PDR_MGR_TERMINUS_DISCOVERED ||
            mgr->termini[i].state == PDR_MGR_TERMINUS_STALE) {
            if (pdr_mgr_sync_terminus(mgr, mgr->termini[i].eid) != 0) {
                errors++;
            }
        }
    }

    return (errors > 0) ? -1 : 0;
}

/* ----------------------------------------------------------------
 * Check For Changes (lightweight signature comparison)
 * ---------------------------------------------------------------- */
int pdr_mgr_check_for_changes(pdr_mgr_t *mgr, uint8_t eid, bool *changed)
{
    int idx = find_terminus_idx(mgr, eid);
    if (idx < 0) {
        return -1;
    }

    pdr_mgr_terminus_t *term = &mgr->termini[idx];
    uint32_t old_sig = term->last_signature;

    int rc = pdr_mgr_fetch_repo_info(mgr, term);
    if (rc != 0) {
        return -1;
    }

    *changed = (old_sig == 0 || term->last_signature != old_sig);

    if (*changed && term->state == PDR_MGR_TERMINUS_SYNCED) {
        term->state = PDR_MGR_TERMINUS_STALE;
    }

    return 0;
}

/* ----------------------------------------------------------------
 * Consolidated Repo Access — thin wrappers
 * ---------------------------------------------------------------- */
const pdr_repo_info_t *pdr_mgr_get_repo_info(const pdr_mgr_t *mgr)
{
    return pdr_repo_get_info(&mgr->repo);
}

int pdr_mgr_get_pdr(const pdr_mgr_t *mgr,
                     uint32_t  record_handle,
                     uint32_t  data_transfer_handle,
                     uint32_t *next_record_handle,
                     uint32_t *next_data_transfer_handle,
                     uint8_t  *transfer_flag,
                     const uint8_t **data,
                     uint16_t *data_len)
{
    return pdr_repo_get_pdr(&mgr->repo, record_handle, data_transfer_handle,
                             next_record_handle, next_data_transfer_handle,
                             transfer_flag, data, data_len);
}

int pdr_mgr_find_pdr(const pdr_mgr_t *mgr,
                      uint8_t   pdr_type,
                      uint32_t  start_handle,
                      uint32_t *found_handle,
                      uint32_t *next_handle,
                      const uint8_t **data,
                      uint16_t *data_len)
{
    return pdr_repo_find_pdr(&mgr->repo, pdr_type, start_handle,
                              found_handle, next_handle, data, data_len);
}

uint32_t pdr_mgr_get_repo_signature(pdr_mgr_t *mgr)
{
    return pdr_repo_get_signature(&mgr->repo);
}

int pdr_mgr_lookup_origin(const pdr_mgr_t *mgr, uint32_t handle,
                           uint8_t *eid)
{
    uint32_t term_idx = (handle >> PDR_MGR_HANDLE_RANGE_SHIFT) - 1;

    if (term_idx >= PDR_MGR_MAX_TERMINI) {
        return -1;
    }
    if (mgr->termini[term_idx].state == PDR_MGR_TERMINUS_UNUSED) {
        return -1;
    }

    *eid = mgr->termini[term_idx].eid;
    return 0;
}
