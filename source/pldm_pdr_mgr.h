/**
 * @file pldm_pdr_mgr.h
 * @brief PLDM PDR Manager - Manager Role Implementation
 *
 * Implements the PLDM manager role: discovers remote endpoints (termini),
 * fetches their PDRs via PLDM commands, remaps handles into non-overlapping
 * ranges, and builds a consolidated PDR repository.
 *
 * Architecture:
 *   pdr_mgr_t
 *     +-- pdr_repo_t repo              (consolidated storage, reuses pdr_repo)
 *     +-- pdr_mgr_terminus_t[8]        (per-endpoint tracking)
 *     +-- pdr_mgr_transport_t          (transport abstraction via callbacks)
 *
 * Handle remapping scheme:
 *   terminus 0: 0x10000-0x1FFFF
 *   terminus 1: 0x20000-0x2FFFF
 *   ...
 *   terminus 7: 0x80000-0x8FFFF
 */

#ifndef PLDM_PDR_MGR_H
#define PLDM_PDR_MGR_H

#include "pldm_pdr_repo.h"

/* ----------------------------------------------------------------
 * Configuration
 * ---------------------------------------------------------------- */
#define PDR_MGR_MAX_TERMINI             8
#define PDR_MGR_REASSEMBLY_BUF_SIZE     256
#define PDR_MGR_MAX_RETRIES             3
#define PDR_MGR_HANDLE_RANGE_SHIFT      16
#define PDR_MGR_HANDLE_SUB_MASK         0xFFFF

/* ----------------------------------------------------------------
 * PLDM Platform M&C Command Codes (DSP0248)
 * ---------------------------------------------------------------- */
#define PLDM_TYPE_PLATFORM                          0x02
#define PLDM_PLATFORM_CMD_GET_PDR_REPO_INFO         0x50
#define PLDM_PLATFORM_CMD_GET_PDR                   0x51
#define PLDM_PLATFORM_CMD_FIND_PDR                  0x52
#define PLDM_PLATFORM_CMD_GET_PDR_REPO_SIGNATURE    0x53

/* ----------------------------------------------------------------
 * PLDM Completion Codes
 * ---------------------------------------------------------------- */
#define PLDM_CC_SUCCESS                             0x00
#define PLDM_CC_ERROR                               0x01
#define PLDM_CC_ERROR_INVALID_DATA                  0x02
#define PLDM_CC_ERROR_INVALID_LENGTH                0x03
#define PLDM_CC_ERROR_UNSUPPORTED_PLDM_CMD          0x04
#define PLDM_CC_ERROR_INVALID_RECORD_HANDLE         0x05

/* ----------------------------------------------------------------
 * PLDM Transfer Flags
 * ---------------------------------------------------------------- */
#define PLDM_TRANSFER_OP_GET_NEXT_PART              0x00
#define PLDM_TRANSFER_OP_GET_FIRST_PART             0x01

#define PLDM_TRANSFER_FLAG_START                    0x00
#define PLDM_TRANSFER_FLAG_MIDDLE                   0x01
#define PLDM_TRANSFER_FLAG_END                      0x04
#define PLDM_TRANSFER_FLAG_START_AND_END            0x05

/* ----------------------------------------------------------------
 * Wire-Format Structs (packed, per DSP0248)
 * ---------------------------------------------------------------- */

/** GetPDRRepositoryInfo (0x50) response */
typedef struct __attribute__((packed)) {
    uint8_t  completion_code;
    uint8_t  repository_state;
    uint8_t  update_time[13];           /* PLDM timestamp104 */
    uint8_t  oem_update_time[13];       /* PLDM timestamp104 */
    uint32_t record_count;
    uint32_t repository_size;
    uint32_t largest_record_size;
    uint8_t  data_transfer_handle_timeout;
} pdr_mgr_get_repo_info_resp_t;

/** GetPDR (0x51) request */
typedef struct __attribute__((packed)) {
    uint32_t record_handle;
    uint32_t data_transfer_handle;
    uint8_t  transfer_op_flag;
    uint16_t request_count;
    uint16_t record_change_num;
} pdr_mgr_get_pdr_req_t;

/** GetPDR (0x51) response header (followed by response_count bytes) */
typedef struct __attribute__((packed)) {
    uint8_t  completion_code;
    uint32_t next_record_handle;
    uint32_t next_data_transfer_handle;
    uint8_t  transfer_flag;
    uint16_t response_count;
    /* record_data[response_count] follows */
} pdr_mgr_get_pdr_resp_t;

/** GetPDRRepositorySignature (0x53) response */
typedef struct __attribute__((packed)) {
    uint8_t  completion_code;
    uint32_t signature;
} pdr_mgr_get_pdr_sig_resp_t;

/* ----------------------------------------------------------------
 * Terminus State Machine
 * ---------------------------------------------------------------- */
typedef enum {
    PDR_MGR_TERMINUS_UNUSED     = 0,
    PDR_MGR_TERMINUS_DISCOVERED = 1,
    PDR_MGR_TERMINUS_SYNCING    = 2,
    PDR_MGR_TERMINUS_SYNCED     = 3,
    PDR_MGR_TERMINUS_STALE      = 4,
    PDR_MGR_TERMINUS_ERROR      = 5,
} pdr_mgr_terminus_state_t;

/* ----------------------------------------------------------------
 * Per-Terminus Fetch Context
 *
 * Tracks multi-part reassembly and iteration progress while
 * fetching PDRs from a remote endpoint.
 * ---------------------------------------------------------------- */
typedef struct {
    uint8_t  reassembly_buf[PDR_MGR_REASSEMBLY_BUF_SIZE];
    uint16_t reassembly_len;         /* Bytes accumulated so far           */
    uint32_t next_record_handle;     /* Next record to fetch (0 = first)   */
    uint16_t records_fetched;        /* Records successfully fetched       */
    uint8_t  retries;                /* Retry counter for current op       */
} pdr_mgr_fetch_ctx_t;

/* ----------------------------------------------------------------
 * Handle Map Entry (remote → local handle tracking)
 *
 * Used by the change event handler for incremental PDR updates.
 * Maps the original record_handle from the remote terminus to
 * the remapped handle in the consolidated repository.
 * ---------------------------------------------------------------- */
typedef struct {
    uint32_t remote_handle;          /* Original handle on the terminus    */
    uint32_t local_handle;           /* Remapped handle in consolidated    */
} pdr_mgr_handle_map_entry_t;

/* ----------------------------------------------------------------
 * Per-Terminus Tracking
 * ---------------------------------------------------------------- */
typedef struct {
    pdr_mgr_terminus_state_t state;
    uint8_t  eid;                    /* MCTP endpoint ID                   */
    uint8_t  tid;                    /* PLDM terminus ID                   */
    uint16_t terminus_handle;        /* PLDM terminus handle               */
    uint32_t remote_record_count;    /* From GetPDRRepositoryInfo          */
    uint32_t remote_repo_size;       /* From GetPDRRepositoryInfo          */
    uint32_t last_signature;         /* Last known repo signature          */
    uint16_t local_handle_seq;       /* Next sub-handle within our range   */
    uint16_t local_record_count;     /* Records in consolidated repo       */
    pdr_mgr_fetch_ctx_t fetch_ctx;
    /* Handle map for incremental updates (change events) */
    pdr_mgr_handle_map_entry_t handle_map[PDR_MAX_RECORD_COUNT];
    uint16_t handle_map_count;
} pdr_mgr_terminus_t;

/* ----------------------------------------------------------------
 * Transport Abstraction
 *
 * A single blocking send-receive callback. The integrator provides
 * the implementation for their transport (Zephyr MCTP, AF_MCTP, etc.).
 * ---------------------------------------------------------------- */

/**
 * @brief Transport send/receive callback.
 *
 * @param eid        Destination MCTP endpoint ID
 * @param pldm_type  PLDM type (0x02 = Platform M&C)
 * @param command    PLDM command code
 * @param req_data   Request payload (command-specific, may be NULL)
 * @param req_len    Request payload length
 * @param resp_data  Response buffer (filled by transport)
 * @param resp_len   In: buffer size, Out: actual response length
 * @param ctx        Opaque context from pdr_mgr_transport_t
 * @return 0 on success, -1 on transport error
 */
typedef int (*pdr_mgr_send_recv_fn)(
    uint8_t        eid,
    uint8_t        pldm_type,
    uint8_t        command,
    const uint8_t *req_data,
    uint16_t       req_len,
    uint8_t       *resp_data,
    uint16_t      *resp_len,
    void          *ctx
);

typedef struct {
    pdr_mgr_send_recv_fn send_recv;
    void                *ctx;        /* Passed to send_recv on every call  */
} pdr_mgr_transport_t;

/* ----------------------------------------------------------------
 * Top-Level Manager
 * ---------------------------------------------------------------- */
typedef struct {
    pdr_repo_t           repo;       /* Consolidated PDR repository        */
    pdr_mgr_terminus_t   termini[PDR_MGR_MAX_TERMINI];
    pdr_mgr_transport_t  transport;
} pdr_mgr_t;

/* ----------------------------------------------------------------
 * API: Initialization
 * ---------------------------------------------------------------- */

/**
 * @brief Initialize the PDR manager.
 *
 * Zeros all state, initializes the consolidated repo, and stores
 * the transport callbacks.
 */
void pdr_mgr_init(pdr_mgr_t *mgr, const pdr_mgr_transport_t *transport);

/* ----------------------------------------------------------------
 * API: Terminus Management
 * ---------------------------------------------------------------- */

/**
 * @brief Register a remote endpoint.
 *
 * @param mgr              Manager instance
 * @param eid              MCTP endpoint ID
 * @param terminus_handle  PLDM terminus handle
 * @param tid              PLDM terminus ID
 * @param[out] index_out   Assigned slot index (optional, can be NULL)
 * @return 0 on success, -1 if full or duplicate
 */
int pdr_mgr_add_terminus(pdr_mgr_t *mgr, uint8_t eid,
                          uint16_t terminus_handle, uint8_t tid,
                          uint8_t *index_out);

/**
 * @brief Remove a terminus and purge all its PDRs from the consolidated repo.
 */
int pdr_mgr_remove_terminus(pdr_mgr_t *mgr, uint8_t eid);

/**
 * @brief Find a terminus by EID. Returns NULL if not found.
 */
pdr_mgr_terminus_t *pdr_mgr_find_terminus(pdr_mgr_t *mgr, uint8_t eid);

/**
 * @brief Query the current state of a terminus.
 */
int pdr_mgr_get_terminus_state(const pdr_mgr_t *mgr, uint8_t eid,
                                pdr_mgr_terminus_state_t *state);

/* ----------------------------------------------------------------
 * API: PDR Synchronization
 * ---------------------------------------------------------------- */

/**
 * @brief Full sync of a single terminus.
 *
 * 1. Fetch repo info + signature
 * 2. Skip if signature unchanged
 * 3. Purge old PDRs
 * 4. Fetch all PDRs with multi-part reassembly
 * 5. Remap handles and add to consolidated repo
 * 6. Mark as SYNCED
 */
int pdr_mgr_sync_terminus(pdr_mgr_t *mgr, uint8_t eid);

/**
 * @brief Sync all termini in DISCOVERED or STALE state.
 * @return 0 if all succeeded, -1 if any failed
 */
int pdr_mgr_sync_all(pdr_mgr_t *mgr);

/**
 * @brief Lightweight check — fetch signature and compare.
 * @param[out] changed  true if the remote repo has changed
 */
int pdr_mgr_check_for_changes(pdr_mgr_t *mgr, uint8_t eid, bool *changed);

/* ----------------------------------------------------------------
 * API: Consolidated Repo Access (thin wrappers)
 * ---------------------------------------------------------------- */

const pdr_repo_info_t *pdr_mgr_get_repo_info(const pdr_mgr_t *mgr);

int pdr_mgr_get_pdr(const pdr_mgr_t *mgr,
                     uint32_t  record_handle,
                     uint32_t  data_transfer_handle,
                     uint32_t *next_record_handle,
                     uint32_t *next_data_transfer_handle,
                     uint8_t  *transfer_flag,
                     const uint8_t **data,
                     uint16_t *data_len);

int pdr_mgr_find_pdr(const pdr_mgr_t *mgr,
                      uint8_t   pdr_type,
                      uint32_t  start_handle,
                      uint32_t *found_handle,
                      uint32_t *next_handle,
                      const uint8_t **data,
                      uint16_t *data_len);

uint32_t pdr_mgr_get_repo_signature(pdr_mgr_t *mgr);

/**
 * @brief Determine which terminus owns a given handle.
 *
 * @param mgr     Manager instance
 * @param handle  Record handle from the consolidated repo
 * @param[out] eid  EID of the originating terminus
 * @return 0 on success, -1 if handle doesn't map to a known terminus
 */
int pdr_mgr_lookup_origin(const pdr_mgr_t *mgr, uint32_t handle,
                           uint8_t *eid);

/* ----------------------------------------------------------------
 * Internal Helpers (exposed for testing)
 * ---------------------------------------------------------------- */

/** Fetch GetPDRRepositoryInfo (0x50) + GetPDRRepositorySignature (0x53) */
int pdr_mgr_fetch_repo_info(pdr_mgr_t *mgr, pdr_mgr_terminus_t *term);

/** Fetch a single PDR with multi-part reassembly */
int pdr_mgr_fetch_one_pdr(pdr_mgr_t *mgr, pdr_mgr_terminus_t *term);

/** Compute a remapped handle from terminus index and sequence number */
uint32_t pdr_mgr_remap_handle(uint8_t terminus_idx, uint16_t seq);

/** Add a PDR to the consolidated repo with a forced handle */
int pdr_mgr_add_remapped_pdr(pdr_mgr_t *mgr, uint32_t remapped_handle,
                              uint8_t pdr_type, const void *data,
                              uint16_t data_len);

/** Remove all PDRs belonging to a terminus from the consolidated repo */
int pdr_mgr_purge_terminus_pdrs(pdr_mgr_t *mgr, uint8_t terminus_idx);

/** Fetch a specific PDR by remote handle (result in fetch_ctx.reassembly_buf) */
int pdr_mgr_fetch_pdr_by_handle(pdr_mgr_t *mgr, pdr_mgr_terminus_t *term,
                                 uint32_t remote_handle);

/** Look up local (remapped) handle from a remote handle */
int pdr_mgr_find_handle_mapping(const pdr_mgr_terminus_t *term,
                                 uint32_t remote_handle,
                                 uint32_t *local_handle);

/** Record a remote → local handle mapping */
int pdr_mgr_add_handle_mapping(pdr_mgr_terminus_t *term,
                                uint32_t remote_handle,
                                uint32_t local_handle);

/** Remove a handle mapping by remote handle */
int pdr_mgr_remove_handle_mapping(pdr_mgr_terminus_t *term,
                                   uint32_t remote_handle);

#endif /* PLDM_PDR_MGR_H */
