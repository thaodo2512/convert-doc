/**
 * @file pdr_repo.h
 * @brief PLDM PDR Repository - Blob + Metadata Design
 *
 * Design goals:
 *   - Single contiguous blob for all PDR data (zero-copy serving)
 *   - Per-record index for O(1) access by record handle
 *   - Efficient support for all 5 PDR repository commands:
 *
 *     0x50 GetPDRRepositoryInfo  -> repo-level metadata
 *     0x51 GetPDR                -> fetch record by handle, multi-part transfer
 *     0x52 FindPDR               -> search by PDR type / other criteria
 *     0x53 GetPDRRepositorySignature -> CRC32 over the blob
 *     0x58 RunInitAgent          -> rebuild/reinitialize the repository
 */

#ifndef PLDM_PDR_REPO_H
#define PLDM_PDR_REPO_H

#include <stdint.h>
#include <stdbool.h>
#include <string.h>

/* ----------------------------------------------------------------
 * Configuration
 * ---------------------------------------------------------------- */
#define PDR_REPO_MAX_SIZE           (8 * 1024)  /* Max blob size in bytes     */
#define PDR_MAX_RECORD_COUNT        64           /* Max number of PDR records  */
#define PDR_TRANSFER_CHUNK_SIZE     128          /* Max bytes per GetPDR xfer  */

/* ----------------------------------------------------------------
 * PLDM PDR Common Header (per DSP0248)
 * Every PDR record starts with this 5-byte header.
 * ---------------------------------------------------------------- */
typedef struct __attribute__((packed)) {
    uint32_t record_handle;       /* Unique handle for this record             */
    uint8_t  pdr_header_version;  /* PDR header format version (typically 0x01)*/
    uint8_t  pdr_type;            /* PDR type (e.g., numeric sensor, FRU, etc) */
    uint16_t record_change_num;   /* Incremented on record modification        */
    uint16_t data_length;         /* Length of record data following header     */
} pldm_pdr_hdr_t;

/* ----------------------------------------------------------------
 * Per-Record Index Entry (metadata kept outside the blob)
 *
 * This is the "table of contents" that lets us quickly locate
 * any record inside the blob without parsing.
 * ---------------------------------------------------------------- */
typedef struct {
    uint32_t record_handle;       /* Duplicated from PDR header for fast lookup*/
    uint32_t offset;              /* Byte offset into pdr_blob[]               */
    uint16_t size;                /* Total size INCLUDING the PDR header       */
    uint8_t  pdr_type;            /* Duplicated for FindPDR filtering          */
    uint8_t  _reserved;           /* Alignment padding                         */
} pdr_index_entry_t;

/* ----------------------------------------------------------------
 * Repository-Level Info
 *
 * Pre-computed metadata returned directly by GetPDRRepositoryInfo.
 * Updated on every add/remove/rebuild so the command handler is trivial.
 * ---------------------------------------------------------------- */
typedef struct {
    uint8_t  repository_state;    /* 0=available, 1=update_in_progress, 2=failed */
    uint32_t record_count;        /* Total number of PDR records               */
    uint32_t repository_size;     /* Total bytes used in pdr_blob[]            */
    uint32_t largest_record_size; /* Size of the biggest single record         */
    uint32_t update_timestamp;    /* Seconds since epoch (or system uptime)    */
    uint32_t oem_update_timestamp;
    uint8_t  data_transfer_handle_timeout; /* In seconds                       */
} pdr_repo_info_t;

/* ----------------------------------------------------------------
 * The PDR Repository (top-level structure)
 * ---------------------------------------------------------------- */
typedef struct {
    /* --- The blob: contiguous storage for all PDR record bytes --- */
    uint8_t  blob[PDR_REPO_MAX_SIZE];
    uint32_t blob_used;            /* Bytes currently used in blob[]           */

    /* --- The index: fast lookup table for each record --- */
    pdr_index_entry_t index[PDR_MAX_RECORD_COUNT];
    uint16_t count;                /* Number of records currently stored       */

    /* --- Repo-level metadata (serves GetPDRRepositoryInfo) --- */
    pdr_repo_info_t info;

    /* --- Signature cache (serves GetPDRRepositorySignature) --- */
    uint32_t signature;            /* CRC32 over blob[0..blob_used-1]          */
    bool     signature_valid;      /* Invalidated on any mutation              */

    /* --- Handle allocator --- */
    uint32_t next_record_handle;   /* Monotonically increasing handle counter  */

} pdr_repo_t;

/* ----------------------------------------------------------------
 * API
 * ---------------------------------------------------------------- */

/**
 * @brief Initialize an empty PDR repository.
 *        Call once at startup, or call again to wipe & rebuild (RunInitAgent).
 */
void pdr_repo_init(pdr_repo_t *repo);

/**
 * @brief Add a PDR record to the repository.
 *
 * @param repo       Repository instance
 * @param pdr_type   PDR type code (DSP0248 Table 28)
 * @param data       Record data (everything AFTER the common PDR header)
 * @param data_len   Length of data in bytes
 * @param[out] handle_out  Assigned record handle (optional, can be NULL)
 * @return 0 on success, -1 if repo is full or blob has no space
 *
 * Internally: writes the PDR common header + data into the blob,
 *             appends an index entry, and updates repo info.
 */
int pdr_repo_add_record(pdr_repo_t *repo,
                         uint8_t     pdr_type,
                         const void *data,
                         uint16_t    data_len,
                         uint32_t   *handle_out);

/**
 * @brief Remove a PDR record by handle.
 *
 * NOTE: This compacts the blob (memmove) and rebuilds affected index offsets.
 *       Acceptable for embedded if removals are rare (typically only on
 *       hot-plug events). If frequent removal is needed, consider a
 *       free-list approach instead of compaction.
 */
int pdr_repo_remove_record(pdr_repo_t *repo, uint32_t record_handle);

/* ----------------------------------------------------------------
 * Command Handlers
 * Each maps almost 1:1 to the corresponding PLDM command.
 * ---------------------------------------------------------------- */

/**
 * @brief [0x50] GetPDRRepositoryInfo
 *        Just returns a pointer to the pre-computed info struct.
 */
const pdr_repo_info_t *pdr_repo_get_info(const pdr_repo_t *repo);

/**
 * @brief [0x51] GetPDR
 *
 * @param repo              Repository
 * @param record_handle     Handle to fetch (0x00000000 = first record)
 * @param data_transfer_handle  Byte offset within the record for multi-part
 * @param[out] next_record_handle  Handle of the next record (0 if last)
 * @param[out] next_data_transfer_handle  Offset for next chunk (0 if complete)
 * @param[out] transfer_flag   0=start, 1=middle, 4=end, 5=start_and_end
 * @param[out] data         Pointer into blob (zero-copy!)
 * @param[out] data_len     Bytes returned in this chunk
 * @return 0 on success, -1 if handle not found
 */
int pdr_repo_get_pdr(const pdr_repo_t *repo,
                      uint32_t  record_handle,
                      uint32_t  data_transfer_handle,
                      uint32_t *next_record_handle,
                      uint32_t *next_data_transfer_handle,
                      uint8_t  *transfer_flag,
                      const uint8_t **data,
                      uint16_t *data_len);

/**
 * @brief [0x52] FindPDR
 *        Search for records matching a given PDR type.
 *
 * @param repo          Repository
 * @param pdr_type      PDR type to search for
 * @param start_handle  Start searching from this handle (0 = beginning)
 * @param[out] found_handle  Handle of the matching record
 * @param[out] next_handle   Handle to continue searching (0 if no more)
 * @param[out] data     Pointer into blob (zero-copy)
 * @param[out] data_len Size of the found record
 * @return 0 if found, -1 if no match
 *
 * NOTE: For more complex FindPDR filters (e.g., by entity type, container ID),
 *       extend this function with additional filter parameters.
 */
int pdr_repo_find_pdr(const pdr_repo_t *repo,
                       uint8_t   pdr_type,
                       uint32_t  start_handle,
                       uint32_t *found_handle,
                       uint32_t *next_handle,
                       const uint8_t **data,
                       uint16_t *data_len);

/**
 * @brief [0x53] GetPDRRepositorySignature
 *        Returns CRC32 over the entire blob. Lazy-computed and cached.
 */
uint32_t pdr_repo_get_signature(pdr_repo_t *repo);

/**
 * @brief [0x58] RunInitAgent
 *        Wipes the repository and triggers a full rebuild.
 *
 * @param repo           Repository to reinitialize
 * @param init_callback  Application callback that re-populates the repo
 *                       by calling pdr_repo_add_record() for each PDR.
 * @param ctx            Opaque context passed to the callback
 */
typedef void (*pdr_init_callback_t)(pdr_repo_t *repo, void *ctx);

int pdr_repo_run_init_agent(pdr_repo_t *repo,
                             pdr_init_callback_t init_callback,
                             void *ctx);

/* ----------------------------------------------------------------
 * Internal Helpers (exposed for unit testing)
 * ---------------------------------------------------------------- */

/** Find the index position for a given record handle. Returns -1 if not found. */
int pdr_repo_find_index(const pdr_repo_t *repo, uint32_t record_handle);

/** Recompute repo info fields (record_count, largest_record_size, etc.) */
void pdr_repo_update_info(pdr_repo_t *repo);

/** Invalidate the cached signature (called on any mutation) */
static inline void pdr_repo_invalidate_signature(pdr_repo_t *repo) {
    repo->signature_valid = false;
}

#endif /* PLDM_PDR_REPO_H */
