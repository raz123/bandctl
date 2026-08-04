# make's built-in CC=cc would otherwise shadow ?=; command line make CC=... still wins
CC = aarch64-linux-musl-gcc
CFLAGS = -static -O2 -Wall

all: qmi_band

qmi_band: qmi_band.c
	$(CC) $(CFLAGS) -o $@ $<

clean:
	rm -f qmi_band

# push helper (device must be connected)
push: qmi_band
	adb push qmi_band /data/local/tmp/
	adb shell chmod 755 /data/local/tmp/qmi_band

.PHONY: all clean push
