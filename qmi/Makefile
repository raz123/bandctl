# make's built-in CC=cc would otherwise shadow ?=; command line make CC=... still wins
CC = aarch64-linux-musl-gcc
CFLAGS = -static -O2 -Wall

all: qmi_band

qmi_band: qmi_band.c
	$(CC) $(CFLAGS) -o $@ $<

clean:
	rm -f qmi_band

# push helper (device must be connected)
# Push to a module-private dir, NOT /data/local/tmp/qmi_band: the server
# execs that exact path as root when the module binary is missing (A-198
# root-exec fallback), so the dev workflow must not feed it (A-205).
push: qmi_band
	adb shell mkdir -p /data/local/tmp/bandctl
	adb push qmi_band /data/local/tmp/bandctl/
	adb shell chmod 755 /data/local/tmp/bandctl/qmi_band

.PHONY: all clean push
