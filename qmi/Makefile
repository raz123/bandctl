# make's built-in CC=cc would otherwise shadow ?=; command line make CC=... still wins
CC = aarch64-linux-musl-gcc
CFLAGS = -static -O2 -Wall

all: qmi_band

qmi_band: qmi_band.c
	$(CC) $(CFLAGS) -o $@ $<

# Host regression tests for the pure-logic and mocked-socket paths in
# qmi_band.c. Built and run with the host compiler (clang on macOS); no
# aarch64 cross toolchain or device needed. `make test ASAN=1` adds
# AddressSanitizer to prove the truncated-TLV guards never read OOB.
HOSTCC ?= cc
HOSTCFLAGS ?= -O1 -g -Wall -Wno-unused-function -Wno-unused-parameter
ASAN ?= 0

test: test_qmi_band
	./test_qmi_band

test_qmi_band: tests/test_qmi_band.c qmi_band.c
	$(HOSTCC) $(HOSTCFLAGS) $(if $(filter 1,$(ASAN)),-fsanitize=address -fno-omit-frame-pointer) -o $@ tests/test_qmi_band.c

clean:
	rm -f qmi_band test_qmi_band

# push helper (device must be connected)
push: qmi_band
	adb push qmi_band /data/local/tmp/
	adb shell chmod 755 /data/local/tmp/qmi_band

.PHONY: all clean push test
