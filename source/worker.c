/* subsys/pldm/responder/worker.c */
#include <zephyr/kernel.h>
#include <zephyr/pldm/pldm.h>
#include <libpldm/base.h>

/* Queue for incoming requests */
K_MSGQ_DEFINE(pldm_resp_q, sizeof(struct net_buf *), 10, 4);

/* Prototypes for specific handlers */
int pldm_handle_base(struct net_buf *req, struct net_buf *resp);
int pldm_handle_platform(struct net_buf *req, struct net_buf *resp);

void pldm_responder_enqueue(struct net_buf *buf)
{
    k_msgq_put(&pldm_resp_q, &buf, K_NO_WAIT);
}

static void pldm_resp_thread(void *p1, void *p2, void *p3)
{
    struct net_buf *req_buf;
    struct net_buf *resp_buf;
    int rc;

    while (1) {
        k_msgq_get(&pldm_resp_q, &req_buf, K_FOREVER);

        struct pldm_msg_hdr *hdr = (struct pldm_msg_hdr *)req_buf->data;
        
        /* Alloc response buffer */
        resp_buf = net_buf_alloc(NULL, K_NO_WAIT); // Use a defined pool in real code
        
        /* Dispatch based on Type */
        switch (hdr->type) {
        case PLDM_BASE:
            rc = pldm_handle_base(req_buf, resp_buf);
            break;
        case PLDM_PLATFORM:
#ifdef CONFIG_PLDM_PLATFORM_TYPE
            rc = pldm_handle_platform(req_buf, resp_buf);
#else
            rc = -ENOTSUP;
#endif
            break;
        default:
            rc = -ENOTSUP;
        }

        if (rc == 0) {
            /* Send response back to source */
            uint8_t dst = pldm_buf_ctx(req_buf)->remote_eid;
            pldm_transport_send(dst, resp_buf);
        }

        net_buf_unref(req_buf);
        if (resp_buf) net_buf_unref(resp_buf);
    }
}

K_THREAD_DEFINE(pldm_server, CONFIG_PLDM_RESPONDER_STACK_SIZE, 
                pldm_resp_thread, NULL, NULL, NULL, 5, 0, 0);
