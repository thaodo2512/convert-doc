/**
 * @file pdr_repo.c
 * @brief PLDM PDR Repository Implementation
 */

#include "pldm_pdr_repo.h"

/* ----------------------------------------------------------------
 * Simple CRC32 (no lookup table to save flash on embedded)
 * Replace with hardware CRC if your MCU has one.
 * ---------------------------------------------------------------- */
static uint32_t crc32_byte(uint32_t crc, uint8_t byte)
{
    crc ^= byte;
    for (int i = 0; i < 8; i++) {
        crc = (crc >> 1) ^ (0xEDB88320 & (-(crc & 1)));
    }
    return crc;
}

static uint32_t crc32_buf(const uint8_t *buf, uint32_t len)
{
    uint32_t crc = 0xFFFFFFFF;
    for (uint32_t i = 0; i < len; i++) {
        crc = crc32_byte(crc, buf[i]);
    }
    return crc ^ 0xFFFFFFFF;
}

/* ----------------------------------------------------------------
 * Init
 * ---------------------------------------------------------------- */
void pdr_repo_init(pdr_repo_t *repo)
{
    memset(repo, 0, sizeof(*repo));
    repo->info.repository_state = 0; /* available */
    repo->next_record_handle = 1;    /* 0 is reserved for "first" */
}

/* ----------------------------------------------------------------
 * Init with external blob
 * ---------------------------------------------------------------- */
void pdr_repo_init_ext(pdr_repo_t *repo, uint8_t *blob, uint32_t blob_capacity)
{
    pdr_repo_init(repo);
    repo->blob = blob;
    repo->blob_capacity = blob_capacity;
}

/* ----------------------------------------------------------------
 * Index an existing record in the blob (zero-copy)
 * ---------------------------------------------------------------- */
int pdr_repo_index_record(pdr_repo_t *repo, uint32_t offset)
{
    if (repo->count >= PDR_MAX_RECORD_COUNT) {
        return -1;
    }

    const pldm_pdr_hdr_t *hdr = (const pldm_pdr_hdr_t *)&repo->blob[offset];
    uint16_t total_size = sizeof(pldm_pdr_hdr_t) + hdr->data_length;

    if ((offset + total_size) > repo->blob_capacity) {
        return -1;
    }

    pdr_index_entry_t *entry = &repo->index[repo->count];
    entry->record_handle = hdr->record_handle;
    entry->offset        = offset;
    entry->size          = total_size;
    entry->pdr_type      = hdr->pdr_type;
    entry->flags         = 0;

    repo->count++;

    if (hdr->record_handle >= repo->next_record_handle) {
        repo->next_record_handle = hdr->record_handle + 1;
    }

    return 0;
}

/* ----------------------------------------------------------------
 * Internal: find index by handle
 * ---------------------------------------------------------------- */
int pdr_repo_find_index(const pdr_repo_t *repo, uint32_t record_handle)
{
    /* Handle 0 means "get the first non-tombstone record" */
    if (record_handle == 0) {
        for (uint16_t i = 0; i < repo->count; i++) {
            if (!pdr_index_is_tombstone(&repo->index[i])) {
                return (int)i;
            }
        }
        return -1;
    }

    for (uint16_t i = 0; i < repo->count; i++) {
        if (repo->index[i].record_handle == record_handle &&
            !pdr_index_is_tombstone(&repo->index[i])) {
            return (int)i;
        }
    }
    return -1;
}

/* ----------------------------------------------------------------
 * Internal: recompute repo-level info after mutation
 * ---------------------------------------------------------------- */
void pdr_repo_update_info(pdr_repo_t *repo)
{
    uint32_t live_count = 0;
    uint32_t live_size  = 0;
    uint32_t largest    = 0;

    for (uint16_t i = 0; i < repo->count; i++) {
        if (pdr_index_is_tombstone(&repo->index[i])) {
            continue;
        }
        live_count++;
        live_size += repo->index[i].size;
        if (repo->index[i].size > largest) {
            largest = repo->index[i].size;
        }
    }

    repo->info.record_count       = live_count;
    repo->info.repository_size    = live_size;
    repo->info.largest_record_size = largest;

    /* TODO: update timestamp from your platform's time source */
    /* repo->info.update_timestamp = platform_get_time(); */

    pdr_repo_invalidate_signature(repo);
}

/* ----------------------------------------------------------------
 * Add Record
 * ---------------------------------------------------------------- */
int pdr_repo_add_record(pdr_repo_t *repo,
                         uint8_t     pdr_type,
                         const void *data,
                         uint16_t    data_len,
                         uint32_t   *handle_out)
{
    uint16_t total_size = sizeof(pldm_pdr_hdr_t) + data_len;

    /* Check capacity */
    if (repo->count >= PDR_MAX_RECORD_COUNT) {
        return -1;
    }
    if ((repo->blob_used + total_size) > repo->blob_capacity) {
        return -1;
    }

    /* Assign handle */
    uint32_t handle = repo->next_record_handle++;

    /* Write PDR common header into blob */
    pldm_pdr_hdr_t hdr = {
        .record_handle      = handle,
        .pdr_header_version = 0x01,
        .pdr_type           = pdr_type,
        .record_change_num  = 0,
        .data_length        = data_len,
    };

    uint32_t offset = repo->blob_used;
    memcpy(&repo->blob[offset], &hdr, sizeof(hdr));
    memcpy(&repo->blob[offset + sizeof(hdr)], data, data_len);

    /* Append index entry */
    pdr_index_entry_t *entry = &repo->index[repo->count];
    entry->record_handle = handle;
    entry->offset        = offset;
    entry->size          = total_size;
    entry->pdr_type      = pdr_type;
    entry->flags         = 0;

    repo->blob_used += total_size;
    repo->count++;

    pdr_repo_update_info(repo);

    if (handle_out) {
        *handle_out = handle;
    }

    return 0;
}

/* ----------------------------------------------------------------
 * Remove Record (tombstone — no compaction, O(1))
 * ---------------------------------------------------------------- */
int pdr_repo_remove_record(pdr_repo_t *repo, uint32_t record_handle)
{
    int idx = pdr_repo_find_index(repo, record_handle);
    if (idx < 0) {
        return -1;
    }

    /* Mark as tombstone — blob data stays in place until RunInitAgent */
    repo->index[idx].flags |= PDR_INDEX_FLAG_TOMBSTONE;

    pdr_repo_update_info(repo);

    return 0;
}

/* ----------------------------------------------------------------
 * [0x50] GetPDRRepositoryInfo
 * ---------------------------------------------------------------- */
const pdr_repo_info_t *pdr_repo_get_info(const pdr_repo_t *repo)
{
    return &repo->info;
}

/* ----------------------------------------------------------------
 * [0x51] GetPDR — with multi-part transfer support
 * ---------------------------------------------------------------- */
int pdr_repo_get_pdr(const pdr_repo_t *repo,
                      uint32_t  record_handle,
                      uint32_t  data_transfer_handle,
                      uint32_t *next_record_handle,
                      uint32_t *next_data_transfer_handle,
                      uint8_t  *transfer_flag,
                      const uint8_t **data,
                      uint16_t *data_len)
{
    int idx = pdr_repo_find_index(repo, record_handle);
    if (idx < 0) {
        return -1;
    }

    const pdr_index_entry_t *entry = &repo->index[idx];

    /* Validate data_transfer_handle (offset within this record) */
    if (data_transfer_handle >= entry->size) {
        return -1;
    }

    /* Calculate how much data to return in this chunk */
    uint32_t remaining = entry->size - data_transfer_handle;
    uint16_t chunk = (remaining > PDR_TRANSFER_CHUNK_SIZE)
                     ? PDR_TRANSFER_CHUNK_SIZE
                     : (uint16_t)remaining;

    *data     = &repo->blob[entry->offset + data_transfer_handle];
    *data_len = chunk;

    /* Next data transfer handle (for multi-part) */
    bool is_first = (data_transfer_handle == 0);
    bool is_last  = ((data_transfer_handle + chunk) >= entry->size);

    if (is_last) {
        *next_data_transfer_handle = 0;
    } else {
        *next_data_transfer_handle = data_transfer_handle + chunk;
    }

    /* Transfer flag */
    if (is_first && is_last) {
        *transfer_flag = 0x05; /* StartAndEnd */
    } else if (is_first) {
        *transfer_flag = 0x00; /* Start */
    } else if (is_last) {
        *transfer_flag = 0x04; /* End */
    } else {
        *transfer_flag = 0x01; /* Middle */
    }

    /* Next record handle — skip tombstones */
    *next_record_handle = 0;
    for (int j = idx + 1; j < repo->count; j++) {
        if (!pdr_index_is_tombstone(&repo->index[j])) {
            *next_record_handle = repo->index[j].record_handle;
            break;
        }
    }

    return 0;
}

/* ----------------------------------------------------------------
 * [0x52] FindPDR — search by PDR type
 * ---------------------------------------------------------------- */
int pdr_repo_find_pdr(const pdr_repo_t *repo,
                       uint8_t   pdr_type,
                       uint32_t  start_handle,
                       uint32_t *found_handle,
                       uint32_t *next_handle,
                       const uint8_t **data,
                       uint16_t *data_len)
{
    /* Determine starting index */
    int start_idx = 0;
    if (start_handle != 0) {
        start_idx = pdr_repo_find_index(repo, start_handle);
        if (start_idx < 0) {
            return -1;
        }
        start_idx++; /* Start searching AFTER the given handle */
    }

    /* Linear scan for matching PDR type, skipping tombstones */
    for (int i = start_idx; i < repo->count; i++) {
        if (pdr_index_is_tombstone(&repo->index[i])) {
            continue;
        }
        if (repo->index[i].pdr_type == pdr_type) {
            *found_handle = repo->index[i].record_handle;
            *data         = &repo->blob[repo->index[i].offset];
            *data_len     = repo->index[i].size;

            /* Find next matching record for continuation, skipping tombstones */
            *next_handle = 0;
            for (int j = i + 1; j < repo->count; j++) {
                if (pdr_index_is_tombstone(&repo->index[j])) {
                    continue;
                }
                if (repo->index[j].pdr_type == pdr_type) {
                    *next_handle = repo->index[j].record_handle;
                    break;
                }
            }
            return 0;
        }
    }

    return -1; /* No match found */
}

/* ----------------------------------------------------------------
 * [0x53] GetPDRRepositorySignature — lazy CRC32
 * ---------------------------------------------------------------- */
uint32_t pdr_repo_get_signature(pdr_repo_t *repo)
{
    if (!repo->signature_valid) {
        repo->signature = crc32_buf(repo->blob, repo->blob_used);
        repo->signature_valid = true;
    }
    return repo->signature;
}

/* ----------------------------------------------------------------
 * [0x58] RunInitAgent — wipe and rebuild
 * ---------------------------------------------------------------- */
int pdr_repo_run_init_agent(pdr_repo_t *repo,
                             pdr_init_callback_t init_callback,
                             void *ctx)
{
    if (!init_callback) {
        return -1;
    }

    /* Mark repo as updating */
    repo->info.repository_state = 1; /* update_in_progress */

    /* Wipe everything except the state flag */
    repo->blob_used = 0;
    repo->count = 0;
    repo->next_record_handle = 1;
    repo->signature_valid = false;
    memset(repo->blob, 0, repo->blob_capacity);

    /* Let the application re-populate */
    init_callback(repo, ctx);

    /* Mark as available again */
    repo->info.repository_state = 0; /* available */
    pdr_repo_update_info(repo);

    return 0;
}
