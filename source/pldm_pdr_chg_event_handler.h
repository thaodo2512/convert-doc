/**
 * @file pldm_pdr_chg_event_handler.h
 * @brief pldmPDRRepositoryChgEvent — manager-side event handler
 *
 * Processes received PDR change events and applies incremental
 * updates to the manager's consolidated PDR repository.
 *
 * Depends on both pldm_pdr_mgr.h (manager state, transport) and
 * pldm_pdr_chg_event.h (event types, decode).
 *
 * Endpoints that only generate events do NOT need this file —
 * they only need pldm_pdr_chg_event.h.
 */

#ifndef PLDM_PDR_CHG_EVENT_HANDLER_H
#define PLDM_PDR_CHG_EVENT_HANDLER_H

#include "pldm_pdr_chg_event.h"
#include "pldm_pdr_mgr.h"

/**
 * @brief Process a received pldmPDRRepositoryChgEvent.
 *
 * Called by the main thread when PlatformEventMessage delivers a
 * PDR change event from a remote terminus.
 *
 * Behavior by event format:
 *   - refreshEntireRepository: triggers pdr_mgr_sync_terminus()
 *   - formatIsPDRTypes:        triggers pdr_mgr_sync_terminus()
 *   - formatIsPDRHandles:      incremental update using handle map
 *     (falls back to full re-sync on any fetch/add error)
 *
 * @param mgr            PDR manager instance
 * @param eid            EID of the terminus that sent the event
 * @param event_data     Raw event data bytes (wire format)
 * @param event_data_len Length of event data
 * @return 0 on success, -1 on decode error or unrecoverable failure
 */
int pdr_chg_event_handle(pdr_mgr_t *mgr, uint8_t eid,
                          const uint8_t *event_data, uint16_t event_data_len);

#endif /* PLDM_PDR_CHG_EVENT_HANDLER_H */
