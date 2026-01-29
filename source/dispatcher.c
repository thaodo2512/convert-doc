/* subsys/pldm/common/dispatch.c */
#include <zephyr/kernel.h>
#include <zephyr/pldm/pldm.h>
#include <zephyr/logging/log.h>

LOG_MODULE_REGISTER(pldm, CONFIG_PLDM_LOG_LEVEL);

/* Externs for routing */
void pldm_responder_enqueue(struct net_buf *buf);
void pldm_requester_handle_resp(struct net_buf *buf);

void pldm_input(struct net_buf *buf)
{
    struct pldm_msg_ctx *ctx = pldm_buf_ctx(buf);

    if (ctx->is_request) {
#ifdef CONFIG_PLDM_RESPONDER
        LOG_DBG("RX Request from EID %d", ctx->remote_eid);
        /* Ref the buffer because we are passing it to another thread */
        net_buf_ref(buf); 
        pldm_responder_enqueue(buf);
#else
        LOG_WRN("Responder disabled, dropping req");
#endif
    } else {
#ifdef CONFIG_PLDM_REQUESTER
        LOG_DBG("RX Response from EID %d", ctx->remote_eid);
        /* Handle in this context (usually fast) */
        pldm_requester_handle_resp(buf);
#endif
    }
}
