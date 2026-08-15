/* qmi/tests/test_qmi_band.c
 *
 * Host regression tests for qmi/qmi_band.c, built and run with the host
 * compiler (clang on macOS) via `make -C qmi test` — no aarch64 cross
 * toolchain, no QRTR socket, no device required.
 *
 * Strategy: pull the module under test into this translation unit (its
 * main() renamed so our test main() wins), which makes every static helper
 * directly testable, and macro-redirect the socket/clock syscalls to the
 * in-process mocks below. That lets the REAL send_set()/main() code paths
 * run end-to-end against scripted QMI responses, which is what the
 * A-16x/A-21x acceptance criteria exercise:
 *
 *   A-164  SA/NSA masks are independent; the SA list is not duplicated
 *          into NSA (verified on the captured SET wire message)
 *   A-165  send_set()/main() exit nonzero when the result TLV reports
 *          FAILURE or is missing/malformed
 *   A-166  parse_csv rejects garbage, ERANGE overflow, out-of-range bands,
 *          and too many tokens
 *   A-057  response_status/parse_response never read past the buffer on
 *          truncated TLVs (also verifiable with `make test ASAN=1`)
 *   A-213  deadline_ms is CLOCK_MONOTONIC-based and returns budget minus
 *          elapsed milliseconds
 *   A-217  the NR5G mode flag is set only when a valid SA/NSA band list is
 *          present; invalid NR input is refused before any SET is sent
 */

#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <time.h>
#include <unistd.h>

/* ---- tiny test framework ---- */

static int checks, fails;

#define CHECK(cond) do {						\
	checks++;							\
	if (!(cond)) {							\
		fails++;						\
		fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); \
	}								\
} while (0)

/* Silence stdout around calls that print (parse_csv error messages,
 * hexdumps, discovery logs). Assertions always go to stderr, so quieting
 * stdout can never hide a failure. */
static int saved_stdout;

static void quiet_on(void)
{
	int devnull;
	fflush(stdout);
	saved_stdout = dup(STDOUT_FILENO);
	devnull = open("/dev/null", O_WRONLY);
	dup2(devnull, STDOUT_FILENO);
	close(devnull);
}

static void quiet_off(void)
{
	fflush(stdout);
	dup2(saved_stdout, STDOUT_FILENO);
	close(saved_stdout);
}

/* ---- syscall mocks (see header comment) ---- */

/* Same layout as qmi_band.c's struct sockaddr_qrtr (u16 family, u32 node,
 * u32 port) — the callers pass that struct; we fill it through this alias. */
struct qrtr_peer { unsigned short family; uint32_t node; uint32_t port; };

static int (*real_clock_gettime)(clockid_t, struct timespec *) = clock_gettime;
static int last_clockid = -1;		/* clockid passed to mock_clock_gettime */
static struct timespec fake_now;
static int fake_clock_on = 0;

static int mock_clock_gettime(clockid_t clk, struct timespec *tp)
{
	last_clockid = (int)clk;
	if (fake_clock_on) {
		*tp = fake_now;
		return 0;
	}
	return real_clock_gettime(clk, tp);
}

static void fake_advance_ms(int ms)
{
	fake_now.tv_nsec += (long)ms * 1000000L;
	fake_now.tv_sec += fake_now.tv_nsec / 1000000000L;
	fake_now.tv_nsec %= 1000000000L;
}

static int mock_socket(int domain, int type, int protocol)
{
	(void)domain; (void)type; (void)protocol;
	return 7; /* any non-negative fd works; the mocks never touch the real fd table */
}

static int mock_poll(struct pollfd *fds, nfds_t nfds, int timeout)
{
	(void)fds; (void)nfds; (void)timeout;
	return 1; /* always "ready" */
}

static ssize_t mock_recv(int fd, void *buf, size_t len, int flags)
{
	(void)fd; (void)buf; (void)len; (void)flags;
	errno = EAGAIN;
	return -1; /* no stray datagrams: the discovery enumeration loop exits immediately */
}

/* Scripted recvfrom responses. Each main()/send_set() run pushes the
 * discovery-GET answer and the SET answer; probe_raw's GET response is
 * patched with the transaction id it actually sent (next_txn is a static
 * that advances across runs within one process). */
struct canned_rx {
	unsigned char data[160];
	int len;
	uint32_t node, port;
	int patch_txn;
};

static struct canned_rx rx_queue[16];
static int rx_n = 0, rx_i = 0;

static void rx_reset(void) { rx_n = 0; rx_i = 0; }

static void rx_push(const unsigned char *d, int len, uint32_t node, uint32_t port,
		    int patch_txn)
{
	struct canned_rx *c = &rx_queue[rx_n++];
	memcpy(c->data, d, (size_t)len);
	c->len = len;
	c->node = node;
	c->port = port;
	c->patch_txn = patch_txn;
}

static uint16_t last_get_txn = 0;
static unsigned char set_msg[512];
static int set_msg_len = 0;

static ssize_t mock_recvfrom(int fd, void *buf, size_t len, int flags,
			     struct sockaddr *from, socklen_t *fromlen)
{
	struct canned_rx *c;
	unsigned char *b = buf;
	(void)fd; (void)flags;
	if (rx_i >= rx_n) {
		errno = EAGAIN;
		return -1;
	}
	c = &rx_queue[rx_i++];
	if ((size_t)c->len > len)
		return -1;
	memcpy(b, c->data, (size_t)c->len);
	if (c->patch_txn) {
		b[1] = (unsigned char)last_get_txn;
		b[2] = (unsigned char)(last_get_txn >> 8);
	}
	if (from) {
		struct qrtr_peer *p = (struct qrtr_peer *)from;
		p->family = 42;
		p->node = c->node;
		p->port = c->port;
	}
	if (fromlen)
		*fromlen = sizeof(struct qrtr_peer);
	return c->len;
}

static ssize_t mock_sendto(int fd, const void *buf, size_t len, int flags,
			   const struct sockaddr *to, socklen_t tolen)
{
	const unsigned char *b = buf;
	uint16_t msg_id;
	(void)fd; (void)flags; (void)to; (void)tolen;
	if (len < 7 || b[0] != 0x00)
		return (ssize_t)len; /* QRTR lookup/control packet: ignore */
	msg_id = (uint16_t)b[3] | ((uint16_t)b[4] << 8);
	if (msg_id == 0x0034) {		/* GET probe: remember txn for the canned reply */
		last_get_txn = (uint16_t)b[1] | ((uint16_t)b[2] << 8);
	} else if (msg_id == 0x0033) {	/* SET: capture the wire message for TLV asserts */
		if (len <= sizeof(set_msg)) {
			memcpy(set_msg, buf, len);
			set_msg_len = (int)len;
		}
	}
	return (ssize_t)len;
}

static ssize_t mock_send(int fd, const void *buf, size_t len, int flags)
{
	(void)fd; (void)buf; (void)len; (void)flags;
	return (ssize_t)len;
}

static int mock_getsockname(int fd, struct sockaddr *addr, socklen_t *len)
{
	struct qrtr_peer *p;
	(void)fd;
	if (!addr || !len || *len < sizeof(struct qrtr_peer))
		return -1;
	p = (struct qrtr_peer *)addr;
	p->family = 42;
	p->node = 1;	/* fake local node */
	p->port = 0;
	*len = sizeof(struct qrtr_peer);
	return 0;
}

/* Redirect every syscall the module under test makes to the mocks above,
 * then compile the module into this TU (its main() renamed so our test
 * main() below wins). All system headers were included above, before these
 * macros, so nothing in the module's own #include lines is rewritten. */
#define socket      mock_socket
#define sendto      mock_sendto
#define recvfrom    mock_recvfrom
#define recv        mock_recv
#define send        mock_send
#define poll        mock_poll
#define getsockname mock_getsockname
#define clock_gettime mock_clock_gettime

#define main qmi_band_original_main
#include "../qmi_band.c"
#undef main
#undef clock_gettime
#undef getsockname
#undef poll
#undef send
#undef recv
#undef recvfrom
#undef sendto
#undef socket

/* ---- QMI wire-format helpers (little-endian) ---- */

static int put_tlv(uint8_t type, const void *val, uint16_t len, unsigned char *out)
{
	out[0] = type;
	out[1] = (unsigned char)(len & 0xff);
	out[2] = (unsigned char)(len >> 8);
	if (len)
		memcpy(out + 3, val, len);
	return 3 + (int)len;
}

static int put_result(uint16_t status, uint16_t err, unsigned char *out)
{
	unsigned char p[4];
	p[0] = (unsigned char)status;  p[1] = (unsigned char)(status >> 8);
	p[2] = (unsigned char)err;     p[3] = (unsigned char)(err >> 8);
	return put_tlv(TLV_RESULT, p, 4, out);
}

static int build_resp(unsigned char *buf, uint16_t txn, uint16_t msg_id,
		      const unsigned char *tlvs, int tlv_len)
{
	buf[0] = 0x02; /* response type */
	buf[1] = (unsigned char)txn; buf[2] = (unsigned char)(txn >> 8);
	buf[3] = (unsigned char)msg_id; buf[4] = (unsigned char)(msg_id >> 8);
	buf[5] = (unsigned char)(tlv_len & 0xff); buf[6] = (unsigned char)(tlv_len >> 8);
	memcpy(buf + 7, tlvs, (size_t)tlv_len);
	return 7 + tlv_len;
}

static const unsigned char *find_tlv(const unsigned char *msg, int len, uint8_t type)
{
	int off = 7; /* QMI header */
	while (off + 3 <= len) {
		uint16_t l = (uint16_t)msg[off + 1] | ((uint16_t)msg[off + 2] << 8);
		if (msg[off] == type)
			return msg + off + 3;
		off += 3 + (int)l;
	}
	return NULL;
}

static uint16_t le_u16(const unsigned char *p)
{
	return (uint16_t)p[0] | ((uint16_t)p[1] << 8);
}

static uint64_t le_u64(const unsigned char *p)
{
	int i;
	uint64_t v = 0;
	for (i = 7; i >= 0; i--)
		v = (v << 8) | p[i];
	return v;
}

static int band_in_mask(const unsigned char *mask64, int band)
{
	int idx = (band - 1) / 64, bit = (band - 1) % 64;
	return !!(mask64[idx * 8 + bit / 8] & (1u << (bit % 8)));
}

static int mask64_all_zero(const unsigned char *p)
{
	int i;
	for (i = 0; i < 64; i++)
		if (p[i])
			return 0;
	return 1;
}

/* ---- tests ---- */

static void test_parse_csv_accepts(void)
{
	int b[64];
	CHECK(parse_csv("1,2,3,5,8", b, 64) == 5);
	CHECK(b[0] == 1 && b[1] == 2 && b[4] == 8);
	CHECK(parse_csv("1 2 3", b, 64) == 3);		/* space-separated */
	CHECK(parse_csv("1, 2, 3", b, 64) == 3);	/* spaces after commas */
	CHECK(parse_csv(", ,1,2,", b, 64) == 2);	/* stray separators */
	CHECK(parse_csv("65", b, 512) == 1 && b[0] == 65); /* 65 is fine for NR */
	CHECK(parse_csv("70", b, 512) == 1 && b[0] == 70);
	CHECK(parse_csv("64", b, 64) == 1);		/* LTE upper bound */
	CHECK(parse_csv("512", b, 512) == 1);		/* NR upper bound */
	CHECK(parse_csv("", b, 64) == 0);		/* empty list is valid */
}

static void test_parse_csv_rejects(void)
{
	int b[64];
	char csv70[300];
	char *p = csv70;
	int i;
	quiet_on();
	CHECK(parse_csv("abc", b, 64) == -1);			/* garbage */
	CHECK(parse_csv("1,2,x", b, 64) == -1);			/* garbage mid-list */
	CHECK(parse_csv("12abc", b, 64) == -1);			/* trailing garbage */
	CHECK(parse_csv("99999999999999999999999999999999999999", b, 64) == -1); /* ERANGE */
	CHECK(parse_csv("0", b, 64) == -1);			/* below range */
	CHECK(parse_csv("-5", b, 64) == -1);
	CHECK(parse_csv("65", b, 64) == -1);			/* A-166: LTE cap 64 */
	CHECK(parse_csv("513", b, 512) == -1);			/* A-166: NR cap 512 */
	for (i = 0; i < 70; i++) {
		if (i)
			*p++ = ',';
		*p++ = '1';
	}
	*p = '\0';
	CHECK(parse_csv(csv70, b, 64) == -1);			/* 70 tokens > max 64 */
	quiet_off();
}

static void test_build_lte_mask(void)
{
	uint64_t m = 0;
	CHECK(build_lte_mask("1,2,3", &m) == 3);
	CHECK(m == 0x7);
	CHECK(build_lte_mask("64", &m) == 1);
	CHECK(m == (1ULL << 63));
	CHECK(build_lte_mask("1,64", &m) == 2);
	CHECK(m == (1ULL | (1ULL << 63)));
	quiet_on();
	CHECK(build_lte_mask("65", &m) == -1);	/* outside LTE 1..64 */
	CHECK(build_lte_mask("abc", &m) == -1);
	quiet_off();
	CHECK(build_lte_mask("", &m) == 0);
	CHECK(m == 0);
}

static void test_build_nr_masks(void)
{
	uint64_t m[8];
	CHECK(build_nr_masks("77", m) == 1);
	CHECK(m[1] == (1ULL << 12));			/* 77-1 = 64 + 12 */
	CHECK(build_nr_masks("78,257", m) == 2);
	CHECK(m[1] == (1ULL << 13) && m[4] == 1);
	CHECK(build_nr_masks("1,64,65,128,129,192,193,256,257,320,321,384,385,448,449,512",
			     m) == 16);
	CHECK(m[0] == 0x8000000000000001ULL);
	CHECK(m[4] == 0x8000000000000001ULL);
	CHECK(m[7] == 0x8000000000000001ULL);
	quiet_on();
	CHECK(build_nr_masks("513", m) == -1);
	CHECK(build_nr_masks("0", m) == -1);
	CHECK(build_nr_masks("junk", m) == -1);
	quiet_off();
	CHECK(build_nr_masks("", m) == 0);		/* empty NR list is valid */
	CHECK(m[0] == 0 && m[7] == 0);
}

/* A-164: SA and NSA are independent masks; the SA list is not duplicated
 * into NSA. (The wire-level half of this lives in test_a217_nr_gating_main.) */
static void test_a164_sa_nsa_split(void)
{
	uint64_t sa[8], nsa[8];
	memset(sa, 0xAA, sizeof(sa));
	memset(nsa, 0xAA, sizeof(nsa));
	CHECK(build_nr_masks("77", sa) == 1);
	CHECK(build_nr_masks("78,257", nsa) == 2);
	CHECK(sa[1] == (1ULL << 12));
	CHECK(nsa[1] == (1ULL << 13) && nsa[4] == 1);
	CHECK(sa[0] == 0 && nsa[0] == 0);	/* no cross-contamination */
}

/* A-057: response_status must never read past the buffer. A truncated result
 * TLV (header claims 4 payload bytes, only 1 present) and any TLV that
 * overruns n must yield -1 — the "missing/malformed result" signal that
 * send_set() turns into a nonzero exit. */
static void test_a057_truncated_tlv(void)
{
	unsigned char rx[32], tlvs[16];
	uint16_t err = 0;
	int n;

	/* clean success */
	n = build_resp(rx, 1, 0x0033, tlvs, put_result(0, 0, tlvs));
	CHECK(response_status(rx, n, &err) == 0);

	/* FAILURE with error code decodes (A-165 signal) */
	n = build_resp(rx, 1, 0x0033, tlvs, put_result(1, 5, tlvs));
	err = 0;
	CHECK(response_status(rx, n, &err) == 1);
	CHECK(err == 5);

	/* truncated result TLV: claims len 4, only 1 payload byte present.
	 * Fill the buffer with 0xff so the pre-fix code would deterministically
	 * read 0xffff and fail the == -1 check instead of "passing" by luck. */
	memset(rx, 0xff, sizeof(rx));
	rx[0] = 0x02; rx[1] = 1; rx[2] = 0; rx[3] = 0x33;
	rx[4] = 0; rx[5] = 0; rx[6] = 0;
	rx[7] = TLV_RESULT; rx[8] = 0x04; rx[9] = 0x00; rx[10] = 0x00;
	err = 0;
	CHECK(response_status(rx, 11, &err) == -1);

	/* TLV header claims more than the whole buffer */
	memset(rx, 0xff, sizeof(rx));
	rx[0] = 0x02; rx[1] = 1; rx[2] = 0; rx[3] = 0x33;
	rx[4] = 0; rx[5] = 0; rx[6] = 0;
	rx[7] = TLV_RESULT; rx[8] = 0x04; rx[9] = 0x00;
	CHECK(response_status(rx, 10, &err) == -1);	/* 7 + 3 + 4 > 10 */

	/* QMI header alone (no TLV header fits) */
	rx[0] = 0x02; rx[1] = 1; rx[2] = 0; rx[3] = 0x33;
	rx[4] = 0; rx[5] = 0; rx[6] = 0;
	rx[7] = TLV_RESULT; rx[8] = 0x04;
	CHECK(response_status(rx, 9, &err) == -1);

	/* no result TLV at all (only a mode TLV) */
	n = build_resp(rx, 1, 0x0033, tlvs, put_tlv(TLV_MODE_PREF, "\x10\x00", 2, tlvs));
	CHECK(response_status(rx, n, &err) == -1);

	/* result TLV too short for status+error (len 2): not a usable result */
	n = build_resp(rx, 1, 0x0033, tlvs, put_tlv(TLV_RESULT, "\x00\x00", 2, tlvs));
	CHECK(response_status(rx, n, &err) == -1);

	/* truncated mode TLV must not crash parse_response either */
	memset(rx, 0xff, sizeof(rx));
	rx[0] = 0x02; rx[1] = 1; rx[2] = 0; rx[3] = 0x33;
	rx[4] = 0; rx[5] = 0; rx[6] = 0;
	rx[7] = TLV_MODE_PREF; rx[8] = 0x04; rx[9] = 0x00; rx[10] = 0x00;
	quiet_on();
	parse_response(rx, 11);		/* must return, not overrun */
	quiet_off();
}

/* A-165: send_set() must return nonzero when the result TLV reports
 * FAILURE, and when the result TLV is missing or malformed; zero only on a
 * clean SUCCESS. This is the exit code the web server trusts. Runs the
 * real send_set()/recv_response() code with scripted socket mocks. */
static int run_send_set(int tlv_kind)
{
	uint64_t lte = 0x7, sa[8] = {0}, nsa[8] = {0};
	unsigned char resp[64], tlvs[16];
	int n, ret;
	sa[1] = (1ULL << 12);	/* band 77 */
	rx_reset();
	if (tlv_kind == 0)
		n = put_result(1, 5, tlvs);			/* FAILURE result */
	else if (tlv_kind == 1)
		n = put_tlv(TLV_MODE_PREF, "\x10\x00", 2, tlvs); /* no result TLV */
	else
		n = put_tlv(TLV_RESULT, "\x00\x00", 2, tlvs);	/* too-short result */
	n = build_resp(resp, 1, 0x0033, tlvs, n);
	rx_push(resp, n, 3, 40, 0);
	quiet_on();
	ret = send_set(7, 3, 40, QMI_RAT_LTE | QMI_RAT_NR5G, &lte, sa, nsa);
	quiet_off();
	return ret;
}

static void test_a165_failure_exit(void)
{
	CHECK(run_send_set(0) == 1);	/* FAILURE status -> nonzero exit */
	CHECK(run_send_set(1) == 1);	/* missing result TLV -> nonzero exit */
	CHECK(run_send_set(2) == 1);	/* malformed result TLV -> nonzero exit */
}

/* Drive the real main() with scripted discovery + SET responses, then
 * inspect the captured SET wire message. */
static int run_main_capture(int argc, char **argv)
{
	unsigned char resp[64], tlvs[8];
	int n, ret;
	rx_reset();
	set_msg_len = 0;
	last_get_txn = 0;
	n = build_resp(resp, 0, 0x0034, tlvs, put_result(0, 0, tlvs));
	rx_push(resp, n, 3, 40, 1);	/* discovery probe answer (txn patched) */
	n = build_resp(resp, 1, 0x0033, tlvs, put_result(0, 0, tlvs));
	rx_push(resp, n, 3, 40, 0);	/* SET response */
	quiet_on();
	ret = qmi_band_original_main(argc, argv);
	quiet_off();
	return ret;
}

/* A-217: the NR5G mode flag is gated on a non-empty valid SA/NSA band
 * list, invalid NR input is refused before any SET is sent, and (A-164) the
 * captured SET message carries the independent SA and NSA masks. */
static void test_a217_nr_gating_main(void)
{
	char *argv0 = "qmi_band";
	const unsigned char *t;

	/* SA list only: NR5G flag set, SA mask has 77, NSA is NOT a copy of
	 * SA (A-164), LTE mask is 1|2|3. */
	{
		char *av[] = { argv0, "--set", "1,2,3", "77", NULL };
		CHECK(run_main_capture(4, av) == 0);
		t = find_tlv(set_msg, set_msg_len, TLV_MODE_PREF);
		CHECK(t != NULL);
		if (t)
			CHECK(le_u16(t) == (QMI_RAT_LTE | QMI_RAT_NR5G));
		t = find_tlv(set_msg, set_msg_len, TLV_LTE_BAND_PREF);
		CHECK(t != NULL && le_u64(t) == 0x7);
		t = find_tlv(set_msg, set_msg_len, TLV_NR5G_SA_BAND_PREF);
		CHECK(t != NULL && band_in_mask(t, 77));
		t = find_tlv(set_msg, set_msg_len, TLV_NR5G_NSA_BAND_PREF);
		CHECK(t != NULL && mask64_all_zero(t));	/* A-164: no SA duplication */
	}

	/* explicit NSA list: SA=77, NSA=78+257, mode keeps NR5G */
	{
		char *av[] = { argv0, "--set", "1,2,3", "77", "78,257", NULL };
		CHECK(run_main_capture(5, av) == 0);
		t = find_tlv(set_msg, set_msg_len, TLV_MODE_PREF);
		CHECK(t != NULL && le_u16(t) == (QMI_RAT_LTE | QMI_RAT_NR5G));
		t = find_tlv(set_msg, set_msg_len, TLV_NR5G_SA_BAND_PREF);
		CHECK(t != NULL && band_in_mask(t, 77) && !band_in_mask(t, 78));
		t = find_tlv(set_msg, set_msg_len, TLV_NR5G_NSA_BAND_PREF);
		CHECK(t != NULL && band_in_mask(t, 78) && band_in_mask(t, 257) &&
		      !band_in_mask(t, 77));
	}

	/* --set-all-lte: no NR masks -> NR5G flag must stay OFF (A-217) */
	{
		char *av[] = { argv0, "--set-all-lte", "1,2,3", NULL };
		CHECK(run_main_capture(3, av) == 0);
		t = find_tlv(set_msg, set_msg_len, TLV_MODE_PREF);
		CHECK(t != NULL && le_u16(t) == QMI_RAT_LTE);
		t = find_tlv(set_msg, set_msg_len, TLV_NR5G_SA_BAND_PREF);
		CHECK(t != NULL && mask64_all_zero(t));
		t = find_tlv(set_msg, set_msg_len, TLV_NR5G_NSA_BAND_PREF);
		CHECK(t != NULL && mask64_all_zero(t));
	}

	/* empty NR list is still a valid SET: mode stays LTE-only */
	{
		char *av[] = { argv0, "--set", "1,2,3", "", NULL };
		CHECK(run_main_capture(4, av) == 0);
		t = find_tlv(set_msg, set_msg_len, TLV_MODE_PREF);
		CHECK(t != NULL && le_u16(t) == QMI_RAT_LTE);
	}

	/* invalid NR input is refused before any SET is sent (A-217) */
	{
		char *av[] = { argv0, "--set", "1,2,3", "513", NULL };
		CHECK(run_main_capture(4, av) == 1);
		CHECK(set_msg_len == 0);
	}
	{
		char *av[] = { argv0, "--set", "1,2,3", "77", "junk", NULL };
		CHECK(run_main_capture(5, av) == 1);
		CHECK(set_msg_len == 0);
	}

	/* empty LTE mask is refused (existing guard) */
	{
		char *av[] = { argv0, "--set", "", "77", NULL };
		CHECK(run_main_capture(4, av) == 1);
		CHECK(set_msg_len == 0);
	}

	/* a FAILURE SET response propagates a nonzero exit through main (A-165) */
	{
		unsigned char resp[64], tlvs[8];
		char *av[] = { argv0, "--set", "1,2,3", "77", NULL };
		int n;
		rx_reset();
		set_msg_len = 0;
		n = build_resp(resp, 0, 0x0034, tlvs, put_result(0, 0, tlvs));
		rx_push(resp, n, 3, 40, 1);
		n = build_resp(resp, 1, 0x0033, tlvs, put_result(1, 0x05, tlvs));
		rx_push(resp, n, 3, 40, 0);
		quiet_on();
		CHECK(qmi_band_original_main(4, av) == 1);
		quiet_off();
	}
}

/* A-213: deadline_ms is CLOCK_MONOTONIC-based and returns budget minus
 * elapsed milliseconds. Deterministic with the fake clock, plus one
 * real-clock smoke test. */
static void test_a213_deadline(void)
{
	struct timespec start;
	int d;

	start.tv_sec = 100;
	start.tv_nsec = 250000000L;
	fake_now = start;
	fake_clock_on = 1;
	d = deadline_ms(&start, 500);
	CHECK(d == 500);
	CHECK(last_clockid == CLOCK_MONOTONIC);	/* monotonic, not wall clock */
	fake_advance_ms(100);
	CHECK(deadline_ms(&start, 500) == 400);
	fake_advance_ms(400);
	CHECK(deadline_ms(&start, 500) == 0);	/* exactly at deadline */
	fake_advance_ms(1);
	CHECK(deadline_ms(&start, 500) <= 0);	/* past deadline */
	CHECK(deadline_ms(&start, 0) <= 0);	/* zero budget */
	fake_clock_on = 0;

	/* real-clock smoke: a short sleep must visibly consume the budget */
	{
		struct timespec sleep = { 0, 30000000L };
		clock_gettime(CLOCK_MONOTONIC, &start);
		nanosleep(&sleep, NULL);
		d = deadline_ms(&start, 200);
		CHECK(d > 0 && d <= 200 && d >= 150);
	}
}

static void banner(const char *name)
{
	printf("  ok - %s\n", name);
}

int main(void)
{
	test_parse_csv_accepts();
	banner("parse_csv accepts");
	test_parse_csv_rejects();
	banner("parse_csv rejects (garbage/ERANGE/513/65/70-token)");
	test_build_lte_mask();
	banner("build_lte_mask");
	test_build_nr_masks();
	banner("build_nr_masks (1..512 across 8 words)");
	test_a164_sa_nsa_split();
	banner("A-164 SA/NSA mask split");
	test_a057_truncated_tlv();
	banner("A-057 truncated-TLV guards (response_status/parse_response)");
	test_a165_failure_exit();
	banner("A-165 FAILURE-exit signal (send_set)");
	test_a217_nr_gating_main();
	banner("A-217 NR5G flag gating + A-164 wire format (real main)");
	test_a213_deadline();
	banner("A-213 monotonic deadline helper");

	if (fails) {
		printf("%d of %d checks FAILED\n", fails, checks);
		return 1;
	}
	printf("All %d checks passed\n", checks);
	return 0;
}
