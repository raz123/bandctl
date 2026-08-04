/* qmi_band.c - QRTR QMI NAS band-preference client for Poco F3 (SM8250, SDX55M).
 *
 * Sends QMI_NAS_GET_SYSTEM_SELECTION_PREFERENCE (0x0034) and
 * QMI_NAS_SET_SYSTEM_SELECTION_PREFERENCE (0x0033) directly over QRTR
 * (AF_QIPCRTR=42). No QMI_CTL client-id handshake: the QRTR source port
 * IS the client id (libqmi qmi-endpoint-qrtr.c).
 *
 * Usage:
 *   qmi_band --get
 *   qmi_band --set <lte_csv> <nr_csv>     e.g. --set 1,2,3,5,8 1,3,5,77,78
 *   qmi_band --set-all-lte <lte_csv>      convenience: NR masks empty
 *
 * Build: aarch64-linux-musl-gcc -static -O2 -o qmi_band qmi_band.c
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
#include <unistd.h>
#include <stdint.h>
#include <poll.h>
#include <time.h>
#include <sys/socket.h>
#include <sys/types.h>

/* --- QRTR plumbing (mirrors qrtr_probe2.c) --- */

static uint32_t le32(const unsigned char *p)
{
	return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

static uint16_t le16(const unsigned char *p)
{
	return (uint16_t)p[0] | ((uint16_t)p[1] << 8);
}

struct sockaddr_qrtr {
	unsigned short sq_family;
	uint32_t sq_node;
	uint32_t sq_port;
};

struct __attribute__((packed)) qmi_hdr {
	uint8_t type;      /* 0x00 request, 0x02 response */
	uint16_t txn_id;
	uint16_t msg_id;
	uint16_t msg_len;  /* payload length after this 7-byte header */
};

#define QRTR_PORT_CTRL       0xfffffffeu
#define QRTR_TYPE_NEW_LOOKUP 10
#define QRTR_TYPE_NEW_SERVER 4

struct __attribute__((packed)) qrtr_ctrl_pkt {
	uint32_t cmd;
	union {
		struct __attribute__((packed)) { uint32_t service, instance, node, port; } server;
		struct __attribute__((packed)) { uint32_t node, port; } client;
	};
};

/* QMI NAS messages */
#define QMI_NAS_GET_SYSTEM_SELECTION_PREFERENCE 0x0034
#define QMI_NAS_SET_SYSTEM_SELECTION_PREFERENCE 0x0033
#define QMI_NAS_GET_SERVING_SYSTEM              0x0003

/* TLVs */
#define TLV_RESULT               0x02
#define TLV_MODE_PREF            0x11
#define TLV_LTE_BAND_PREF        0x15
#define TLV_CHANGE_DURATION      0x17
#define TLV_LTE_BAND_PREF_EXT    0x23
#define TLV_NR5G_SA_BAND_PREF    0x2F
#define TLV_NR5G_SA_BAND_PREF_EXT 0x2C
#define TLV_NR5G_NSA_BAND_PREF   0x30
#define TLV_NR5G_NSA_BAND_PREF_EXT 0x2D

#define QMI_RAT_LTE  0x10  /* 1 << 4 */
#define QMI_RAT_NR5G 0x40  /* 1 << 6 */

static void hexdump(const char *tag, const unsigned char *b, int n)
{
	int i;
	printf("%s (%d bytes):", tag, n);
	for (i = 0; i < n; i++)
		printf(" %02x", b[i]);
	printf("\n");
	fflush(stdout);
}

static int wait_recv(int fd, unsigned char *rx, int max, int ms)
{
	struct pollfd pfd = {fd, POLLIN, 0};
	int ret = poll(&pfd, 1, ms);
	if (ret <= 0) return ret; /* 0=timeout, <0=err */
	return (int)recv(fd, rx, max, 0);
}

/* --- message building --- */

static int add_tlv(unsigned char *buf, int off, uint8_t type,
		   const void *val, uint16_t len)
{
	buf[off++] = type;
	buf[off++] = (uint8_t)(len & 0xff);
	buf[off++] = (uint8_t)(len >> 8);
	memcpy(buf + off, val, len);
	return off + len;
}

/* SET 0x0033 request. lte = u64 mask, sa/nsa = 8 x u64 masks (64 bytes each).
 * Returns total message length. */
static int build_set(unsigned char *buf, uint16_t mode,
		     const uint64_t *lte, const uint64_t *sa, const uint64_t *nsa,
		     uint8_t duration)
{
	struct qmi_hdr *h = (struct qmi_hdr *)buf;
	int off = (int)sizeof(*h);

	memset(h, 0, sizeof(*h));
	h->type = 0x00;
	h->txn_id = 1;
	h->msg_id = QMI_NAS_SET_SYSTEM_SELECTION_PREFERENCE;

	off = add_tlv(buf, off, TLV_MODE_PREF, &mode, 2);
	off = add_tlv(buf, off, TLV_LTE_BAND_PREF, lte, 8);
	off = add_tlv(buf, off, TLV_NR5G_SA_BAND_PREF, sa, 64);
	off = add_tlv(buf, off, TLV_NR5G_NSA_BAND_PREF, nsa, 64);
	off = add_tlv(buf, off, TLV_CHANGE_DURATION, &duration, 1);
	h->msg_len = (uint16_t)(off - (int)sizeof(*h));
	return off;
}

/* --- response parsing --- */

static void print_lte_mask(const unsigned char *v, int len)
{
	uint64_t m = 0;
	int i, any = 0;
	if (len >= 8) {
		int k;
		for (k = 0; k < 8; k++)
			m |= (uint64_t)v[k] << (8 * k);
	}
	printf("    LTE bands: ");
	for (i = 1; i <= 64; i++)
		if (m & (1ULL << (i - 1))) { printf("%d ", i); any = 1; }
	if (!any) printf("(none)");
	printf("\n");
}

static void print_nr_mask(const char *tag, const unsigned char *v, int len)
{
	int i, j, any = 0;
	printf("    %s bands: ", tag);
	/* NR5G SA/NSA pref: 8 x u64LE; band = 1 + j + i*64 for bit j of u64 i */
	for (i = 0; i < 8; i++) {
		uint64_t w = 0;
		int k;
		if (8 * (i + 1) > len) break;
		for (k = 0; k < 8; k++)
			w |= (uint64_t)v[8 * i + k] << (8 * k);
		for (j = 0; j < 64; j++)
			if (w & (1ULL << j)) { printf("%d ", 1 + j + i * 64); any = 1; }
	}
	if (!any) printf("(none)");
	printf("\n");
}

/* Parse a response: prints result TLV (status/error), mode, LTE/NR masks. */
static void parse_response(const unsigned char *rx, int n)
{
	int off = 7;
	int have_result = 0;

	while (off + 3 <= n) {
		uint8_t type = rx[off];
		uint16_t len = le16(rx + off + 1);
		const unsigned char *v = rx + off + 3;
		if (off + 3 + (int)len > n) {
			printf("  malformed TLV 0x%02x len %u (overruns)\n", type, len);
			break;
		}
		switch (type) {
		case TLV_RESULT: {
			uint16_t status = le16(v);
			uint16_t err = len >= 4 ? le16(v + 2) : 0;
			printf("  result: status=%u error=0x%04x %s\n", status, err,
			       status == 0 ? "SUCCESS" : "FAILURE");
			have_result = 1;
			break;
		}
		case TLV_MODE_PREF: {
			uint16_t mode = le16(v);
			printf("  mode pref: 0x%04x (LTE=%d NR5G=%d)\n", mode,
			       !!(mode & QMI_RAT_LTE), !!(mode & QMI_RAT_NR5G));
			break;
		}
		case TLV_LTE_BAND_PREF:
		case TLV_LTE_BAND_PREF_EXT:
			print_lte_mask(v, len);
			break;
		case TLV_NR5G_SA_BAND_PREF:
		case TLV_NR5G_SA_BAND_PREF_EXT:
			print_nr_mask("NR5G SA", v, len);
			break;
		case TLV_NR5G_NSA_BAND_PREF:
		case TLV_NR5G_NSA_BAND_PREF_EXT:
			print_nr_mask("NR5G NSA", v, len);
			break;
		default:
			printf("  TLV 0x%02x len %u\n", type, len);
			break;
		}
		off += 3 + (int)len;
	}
	(void)have_result;
}

/* --- CSV parsing -> masks --- */

static int parse_csv(const char *csv, int *bands, int max)
{
	int n = 0;
	const char *p = csv;
	while (*p && n < max) {
		while (*p == ' ' || *p == ',') p++;
		if (!*p) break;
		char *end;
		long b = strtol(p, &end, 10);
		if (end == p) { p++; continue; }
		bands[n++] = (int)b;
		p = end;
	}
	return n;
}

static int build_lte_mask(const char *csv, uint64_t *mask)
{
	int bands[64];
	int n = parse_csv(csv, bands, 64);
	int i;
	*mask = 0;
	for (i = 0; i < n; i++)
		if (bands[i] >= 1 && bands[i] <= 64)
			*mask |= (1ULL << (bands[i] - 1));
	return n;
}

static int build_nr_masks(const char *csv, uint64_t *masks /*[8]*/)
{
	int bands[512];
	int n = parse_csv(csv, bands, 512);
	int i;
	memset(masks, 0, 8 * sizeof(uint64_t));
	for (i = 0; i < n; i++) {
		int b = bands[i];
		if (b < 1) continue;
		{
			int idx = (b - 1) / 64;
			int bit = (b - 1) % 64;
			if (idx < 8)
				masks[idx] |= (1ULL << bit);
		}
	}
	return n;
}

/* --- operations --- */

/* Probe a port with a raw QMI request; returns response length (>0) or <=0. */
static int probe_raw(int fd, uint32_t node, uint32_t port, uint16_t msg_id,
		     const unsigned char *tlvs, int tlv_len,
		     unsigned char *rx, int rxmax, int timeout_ms)
{
	unsigned char msg[512];
	struct qmi_hdr q;
	struct sockaddr_qrtr dst;
	int len;

	memset(&q, 0, sizeof(q));
	q.type = 0x00;
	q.txn_id = 1;
	q.msg_id = msg_id;
	q.msg_len = (uint16_t)tlv_len;
	memcpy(msg, &q, sizeof(q));
	len = (int)sizeof(q);
	if (tlv_len)
		memcpy(msg + len, tlvs, tlv_len);
	len += tlv_len;

	memset(&dst, 0, sizeof(dst));
	dst.sq_family = 42;
	dst.sq_node = node;
	dst.sq_port = port;

	if (sendto(fd, msg, len, 0, (struct sockaddr *)&dst, sizeof(dst)) < 0)
		return -1;
	{
		struct pollfd pfd = {fd, POLLIN, 0};
		time_t end = time(NULL) + timeout_ms / 1000 + 1;
		while (time(NULL) < end) {
			int ms = (int)(end - time(NULL)) * 1000;
			int pr = poll(&pfd, 1, ms < 20 ? 20 : ms);
			if (pr <= 0) return 0; /* timeout */
			int n = (int)recv(fd, rx, rxmax, 0);
			if (n < 0) return -1;
			if (n >= 7 && rx[0] == 0x02) return n;
			/* ignore stray datagrams (e.g. late NEW_SERVER) */
		}
	}
	return 0;
}

/* Decode status/error from a response; returns -1 if no result TLV. */
static int response_status(const unsigned char *rx, int n, uint16_t *err)
{
	int off = 7;
	while (off + 3 <= n) {
		uint8_t t = rx[off];
		uint16_t l = le16(rx + off + 1);
		if (t == TLV_RESULT && l >= 4) {
			*err = le16(rx + off + 5);
			return le16(rx + off + 3);
		}
		off += 3 + (int)l;
	}
	return -1;
}

/* --scan: probe every discovered port with several message IDs and report
 * which (port, msg) combo returns status=0. This is a firmware-mapping
 * diagnostic. */
static int do_scan(int fd)
{
	struct sockaddr_qrtr dst, sa;
	socklen_t sl = sizeof(sa);
	struct qrtr_ctrl_pkt lookup;
	unsigned char rx[2048];
	uint32_t local_node = 1;
	struct { uint32_t svc, node, port; } svcs[32];
	int nsvc = 0;
	static const uint16_t msgs[] = {
		QMI_NAS_GET_SERVING_SYSTEM,               /* 0x0003 */
		QMI_NAS_GET_SYSTEM_SELECTION_PREFERENCE,  /* 0x0034 */
		QMI_NAS_SET_SYSTEM_SELECTION_PREFERENCE,  /* 0x0033 */
		0x0024, /* DMS_GET_DEVICE_CAPABILITIES */
	};
	const char *mnames[] = { "NAS_GET_SERVING_SYSTEM(0x03)",
				 "NAS_GET_SSP(0x34)",
				 "NAS_SET_SSP(0x33)",
				 "DMS_GET_CAPS(0x24)" };
	int i, s, m;

	/* trigger autobind + learn node */
	memset(&lookup, 0, sizeof(lookup));
	lookup.cmd = QRTR_TYPE_NEW_LOOKUP;
	lookup.server.service = 1;
	lookup.server.instance = 0;
	memset(&dst, 0, sizeof(dst));
	dst.sq_family = 42;
	dst.sq_node = 1;
	dst.sq_port = QRTR_PORT_CTRL;
	sendto(fd, &lookup, sizeof(lookup), 0, (struct sockaddr *)&dst, sizeof(dst));
	if (getsockname(fd, (struct sockaddr *)&sa, &sl) == 0 && sa.sq_node)
		local_node = sa.sq_node;
	printf("scan: local node = %u\n", local_node);

	/* lookup services 1..8 with instance 0 AND instance 1 */
	for (i = 1; i <= 8; i++) {
		for (s = 0; s <= 1; s++) {
			memset(&lookup, 0, sizeof(lookup));
			lookup.cmd = QRTR_TYPE_NEW_LOOKUP;
			lookup.server.service = (uint32_t)i;
			lookup.server.instance = (uint32_t)s;
			memset(&dst, 0, sizeof(dst));
			dst.sq_family = 42;
			dst.sq_node = local_node;
			dst.sq_port = QRTR_PORT_CTRL;
			sendto(fd, &lookup, sizeof(lookup), 0,
			       (struct sockaddr *)&dst, sizeof(dst));
		}
	}
	{
		time_t end = time(NULL) + 3;
		while (time(NULL) < end) {
			int ms = (int)(end - time(NULL)) * 1000;
			int ret = wait_recv(fd, rx, sizeof(rx), ms < 50 ? 50 : ms);
			if (ret <= 0) break;
			if (ret >= 20 && le32(rx) == QRTR_TYPE_NEW_SERVER) {
				uint32_t svc = le32(rx + 4), node = le32(rx + 12), port = le32(rx + 16);
				if (node && port && nsvc < 32) {
					svcs[nsvc].svc = svc;
					svcs[nsvc].node = node;
					svcs[nsvc].port = port;
					nsvc++;
				}
			}
		}
	}
	printf("scan: discovered %d endpoints\n", nsvc);
	for (s = 0; s < nsvc; s++)
		printf("  svc=%u node=%u port=%u\n", svcs[s].svc, svcs[s].node, svcs[s].port);

	for (s = 0; s < nsvc; s++) {
		for (m = 0; m < 4; m++) {
			uint16_t err = 0;
			int st;
			int ret = probe_raw(fd, svcs[s].node, svcs[s].port, msgs[m],
					     NULL, 0, rx, sizeof(rx), 500);
			if (ret <= 0) {
				printf("  svc=%u port=%u %-30s : no response\n",
				       svcs[s].svc, svcs[s].port, mnames[m]);
				continue;
			}
			st = response_status(rx, ret, &err);
			if (st == 0)
				printf("  svc=%u port=%u %-30s : *** status=0 SUCCESS ***\n",
				       svcs[s].svc, svcs[s].port, mnames[m]);
			else
				printf("  svc=%u port=%u %-30s : status=%d err=0x%04x\n",
				       svcs[s].svc, svcs[s].port, mnames[m], st, err);
		}
	}
	return 0;
}


static int send_get(int fd, uint32_t node, uint32_t port)
{
	struct qmi_hdr q;
	struct sockaddr_qrtr dst;
	unsigned char rx[2048];
	int ret;

	memset(&q, 0, sizeof(q));
	q.type = 0x00;
	q.txn_id = 1;
	q.msg_id = QMI_NAS_GET_SYSTEM_SELECTION_PREFERENCE;
	q.msg_len = 0;

	memset(&dst, 0, sizeof(dst));
	dst.sq_family = 42;
	dst.sq_node = node;
	dst.sq_port = port;

	if (sendto(fd, &q, sizeof(q), 0, (struct sockaddr *)&dst, sizeof(dst)) < 0) {
		printf("sendto GET: %s\n", strerror(errno));
		return 1;
	}
	ret = wait_recv(fd, rx, sizeof(rx), 5000);
	if (ret <= 0) {
		printf("GET response: %s\n", ret == 0 ? "timeout" : strerror(errno));
		return 1;
	}
	hexdump("GET response", rx, ret);
	parse_response(rx, ret);
	return 0;
}

static int send_set(int fd, uint32_t node, uint32_t port,
		    uint16_t mode, const uint64_t *lte,
		    const uint64_t *sa, const uint64_t *nsa)
{
	unsigned char msg[512];
	unsigned char rx[2048];
	struct sockaddr_qrtr dst;
	int len, ret;

	len = build_set(msg, mode, lte, sa, nsa, 0x01); /* duration PERMANENT */

	memset(&dst, 0, sizeof(dst));
	dst.sq_family = 42;
	dst.sq_node = node;
	dst.sq_port = port;

	hexdump("SET request", msg, len);
	if (sendto(fd, msg, len, 0, (struct sockaddr *)&dst, sizeof(dst)) < 0) {
		printf("sendto SET: %s\n", strerror(errno));
		return 1;
	}
	ret = wait_recv(fd, rx, sizeof(rx), 5000);
	if (ret <= 0) {
		printf("SET response: %s\n", ret == 0 ? "timeout" : strerror(errno));
		return 1;
	}
	hexdump("SET response", rx, ret);
	parse_response(rx, ret);
	return 0;
}

/* --- main --- */

static void usage(const char *a0)
{
	printf("usage:\n"
	       "  %s --scan\n"
	       "  %s --get\n"
	       "  %s --set <lte_csv> <nr_csv>\n"
	       "  %s --set-all-lte <lte_csv>\n",
	       a0, a0, a0, a0);
}

/* Validation-driven NAS discovery.
 *
 * This firmware's QRTR service numbering does NOT match the QMI service
 * ids (svc=4 is NOT NAS here; the real NAS endpoints are svc=3 and svc=5,
 * per on-device probing). So instead of trusting the service id, we:
 *  1. enumerate every endpoint qrtr-ns reports (svc 1..8, instance 0 and 1),
 *  2. send QMI_NAS_GET_SYSTEM_SELECTION_PREFERENCE to each unique port,
 *  3. pick the first port that answers status=0 (SUCCESS) with matching
 *     msg_id — that is the live NAS endpoint.
 * If none validate, fall back to a raw port-range sweep of the modem node.
 * Returns 0 on success with node/port set.
 */
static int find_nas(int fd, uint32_t *node, uint32_t *port)
{
	struct sockaddr_qrtr dst, sa;
	socklen_t sl = sizeof(sa);
	struct qrtr_ctrl_pkt lookup;
	unsigned char rx[2048];
	uint32_t local_node = 1;
	struct { uint32_t svc, node, port; } svcs[64];
	int nsvc = 0, i, s, v;
	uint16_t err = 0;
	int st;

	/* trigger autobind + learn local node */
	memset(&lookup, 0, sizeof(lookup));
	lookup.cmd = QRTR_TYPE_NEW_LOOKUP;
	lookup.server.service = 1;
	lookup.server.instance = 0;
	memset(&dst, 0, sizeof(dst));
	dst.sq_family = 42;
	dst.sq_node = 1;
	dst.sq_port = QRTR_PORT_CTRL;
	sendto(fd, &lookup, sizeof(lookup), 0, (struct sockaddr *)&dst, sizeof(dst));
	if (getsockname(fd, (struct sockaddr *)&sa, &sl) == 0 && sa.sq_node)
		local_node = sa.sq_node;
	printf("  local node = %u\n", local_node);

	/* enumerate services 1..8, instances 0 and 1 */
	for (i = 1; i <= 8; i++) {
		for (v = 0; v <= 1; v++) {
			memset(&lookup, 0, sizeof(lookup));
			lookup.cmd = QRTR_TYPE_NEW_LOOKUP;
			lookup.server.service = (uint32_t)i;
			lookup.server.instance = (uint32_t)v;
			memset(&dst, 0, sizeof(dst));
			dst.sq_family = 42;
			dst.sq_node = local_node;
			dst.sq_port = QRTR_PORT_CTRL;
			sendto(fd, &lookup, sizeof(lookup), 0,
			       (struct sockaddr *)&dst, sizeof(dst));
		}
	}
	{
		time_t end = time(NULL) + 3;
		while (time(NULL) < end) {
			int ms = (int)(end - time(NULL)) * 1000;
			int ret = wait_recv(fd, rx, sizeof(rx), ms < 50 ? 50 : ms);
			if (ret <= 0) break;
			if (ret >= 20 && le32(rx) == QRTR_TYPE_NEW_SERVER) {
				uint32_t svc = le32(rx + 4), n = le32(rx + 12), p = le32(rx + 16);
				if (n && p && nsvc < 64) {
					svcs[nsvc].svc = svc;
					svcs[nsvc].node = n;
					svcs[nsvc].port = p;
					nsvc++;
				}
			}
		}
	}
	printf("  qrtr-ns reports %d endpoints\n", nsvc);

	/* probe each unique port with GET 0x0034; pick first status=0 */
	for (s = 0; s < nsvc && !(*node && *port); s++) {
		int already = 0, k;
		for (k = 0; k < s; k++)
			if (svcs[k].node == svcs[s].node && svcs[k].port == svcs[s].port)
				already = 1;
		if (already) continue;
		printf("  probing svc=%u node=%u port=%u ...\n",
		       svcs[s].svc, svcs[s].node, svcs[s].port);
		{
			int pr = probe_raw(fd, svcs[s].node, svcs[s].port,
				     QMI_NAS_GET_SYSTEM_SELECTION_PREFERENCE,
				     NULL, 0, rx, sizeof(rx), 600);
			if (pr <= 0)
				continue;
			st = response_status(rx, pr, &err);
		}
		if (st == 0) {
			*node = svcs[s].node;
			*port = svcs[s].port;
			printf("  -> NAS endpoint svc=%u node=%u port=%u\n",
			       svcs[s].svc, svcs[s].node, svcs[s].port);
		} else {
			printf("  svc=%u port=%u: status=%d err=0x%04x (not NAS)\n",
			       svcs[s].svc, svcs[s].port, st, err);
		}
	}

	if (!(*node && *port)) {
		/* last resort: raw sweep of modem node ports */
		printf("  fallback: sweeping node 3 ports 40-70 for status=0...\n");
		for (s = 40; s <= 70 && !(*node && *port); s++) {
			int pr = probe_raw(fd, 3, (uint32_t)s,
				     QMI_NAS_GET_SYSTEM_SELECTION_PREFERENCE,
				     NULL, 0, rx, sizeof(rx), 400);
			if (pr <= 0)
				continue;
			st = response_status(rx, pr, &err);
			printf("  port %d: status=%d err=0x%04x\n", s, st, err);
			if (st == 0) {
				*node = 3;
				*port = (uint32_t)s;
				printf("  -> NAS endpoint node 3 port %d\n", s);
			}
		}
	}
	return (*node && *port) ? 0 : 1;
}

int main(int argc, char **argv)
{
	int fd, mode = 0; /* 1=get 2=set */
	uint32_t nas_node = 0, nas_port = 0;
	const char *lte_csv = NULL, *nr_csv = NULL;

	if (argc < 2) { usage(argv[0]); return 2; }
	if (!strcmp(argv[1], "--scan")) {
		fd = socket(42, SOCK_DGRAM, 0);
		if (fd < 0) { printf("socket(): %s\n", strerror(errno)); return 1; }
		return do_scan(fd);
	}
	if (!strcmp(argv[1], "--get")) {
		mode = 1;
	} else if (!strcmp(argv[1], "--set")) {
		if (argc < 4) { usage(argv[0]); return 2; }
		mode = 2;
		lte_csv = argv[2];
		nr_csv = argv[3];
	} else if (!strcmp(argv[1], "--set-all-lte")) {
		if (argc < 3) { usage(argv[0]); return 2; }
		mode = 2;
		lte_csv = argv[2];
		nr_csv = "";
	} else {
		usage(argv[0]);
		return 2;
	}

	fd = socket(42, SOCK_DGRAM, 0);
	if (fd < 0) { printf("socket(): %s\n", strerror(errno)); return 1; }

	printf("discovering QMI NAS endpoint (validation-driven)...\n");
	if (find_nas(fd, &nas_node, &nas_port)) {
		printf("FATAL: could not locate QMI NAS service\n");
		return 1;
	}
	printf("NAS at node %u port %u\n", nas_node, nas_port);

	if (mode == 1) {
		return send_get(fd, nas_node, nas_port);
	}

	/* mode 2: set */
	{
		uint64_t lte = 0, sa[8], nsa[8];
		uint16_t mode_pref;
		int nlte, nsa_n, nnsa;
		nlte = build_lte_mask(lte_csv, &lte);
		nsa_n = build_nr_masks(nr_csv, sa);
		nnsa = build_nr_masks(nr_csv, nsa);
		if (nlte <= 0 || lte == 0) {
			printf("ERROR: empty LTE mask (refusing; would disable all LTE)\n");
			return 1;
		}
		mode_pref = QMI_RAT_LTE;
		if (nsa_n > 0 || nnsa > 0)
			mode_pref |= QMI_RAT_NR5G;
		printf("SET lte bands=%d nr bands=%d mode=0x%04x\n", nlte, nsa_n, mode_pref);
		return send_set(fd, nas_node, nas_port, mode_pref, &lte, sa, nsa);
	}
}
