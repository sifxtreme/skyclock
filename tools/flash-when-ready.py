"""Wait for an ESP32-S3 in download mode on a USB serial port, then flash it.

Opens the port with DTR/RTS held low (so opening it does not reset the chip),
echoes the ROM boot lines, and as soon as the chip prints "waiting for
download" runs esptool with --before no_reset.

  python flash-when-ready.py [firmware.factory.bin]

Run it with a Python that has pyserial and esptool (ESPHome's venv has both:
~/.local/pipx/venvs/esphome/bin/python). Then hold BOOT, tap RESET, release BOOT.
"""
import serial, time, subprocess, os, sys, glob
PY = sys.executable
BIN = sys.argv[1] if len(sys.argv) > 1 else '.esphome/build/lcd-satellite-proof/build/firmware.factory.bin'
deadline = time.time() + 900
while time.time() < deadline:
    ports = glob.glob('/dev/cu.usbmodem*') + glob.glob('/dev/cu.wchusbserial*')
    if not ports: time.sleep(1); continue
    p = ports[0]
    try:
        s = serial.Serial(); s.port = p; s.baudrate = 115200; s.timeout = 0.3
        s.dtr = False; s.rts = False; s.open()
    except Exception as e:
        time.sleep(1); continue
    print('watching', p, flush=True); buf = b''
    while time.time() < deadline:
        try: chunk = s.read(512)
        except Exception: break
        if chunk:
            buf = (buf + chunk)[-2000:]
            for line in chunk.decode('utf-8', 'replace').splitlines():
                if line.startswith(('rst:', 'boot:', 'waiting')) or 'boot:' in line: print('>>', line.strip(), flush=True)
        if b'waiting for download' in buf or b'DOWNLOAD' in buf:
            s.close(); print('DOWNLOAD MODE, flashing', flush=True)
            r = subprocess.run([PY, '-m', 'esptool', '--port', p, '--before', 'no_reset', '--after', 'hard_reset',
                                '--baud', '460800', 'write_flash', '0x0', BIN], capture_output=True, text=True)
            print('\n'.join(l for l in r.stdout.replace('\r', '\n').splitlines() if not l.startswith('Writing at'))[-1500:], r.stderr[-500:], flush=True)
            sys.exit(r.returncode)
    try: s.close()
    except Exception: pass
print('gave up')
