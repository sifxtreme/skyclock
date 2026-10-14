# Flashing the panel (Waveshare ESP32-S3-Touch-LCD-7, Rev 1.2)

`skyclock-panel.yaml` turns the board into a dumb picture frame. It joins Wi-Fi, fetches one 800x480 PNG every 30 s and shows it full screen. Validated with **ESPHome 2026.9.1**.

## 1. Install ESPHome (macOS)

```bash
brew install pipx python@3.13
pipx install --python python3.13 esphome==2026.9.1
```

## 2. Secrets

```bash
cd esphome
cp secrets.example.yaml secrets.yaml
openssl rand -base64 32        # paste as api_key
```

Set your **2.4 GHz** Wi-Fi name and password, and `image_url`. Never commit `secrets.yaml`.

## 3. First flash over USB: what actually worked

The board has two USB-C ports along the top, **USB** (left) and **UART1** (right). There are two side buttons on the left edge, **RESET** (upper) and **BOOT** (lower). A slide switch below them is marked **UART1 / UART2**.

1. **Set the slide switch to UART1.** On UART2, the UART1 port still appears on the Mac (WCH CH343, `/dev/cu.usbmodem*`), but the chip never answers.
2. **Use the UART1 port.** The native USB port vanishes about a second after boot under the factory demo, because those pins are shared with CAN.
3. **Enter download mode by hand.** The DTR/RTS auto-reset did not work from macOS. With the cable in: hold BOOT, press and release RESET, then release BOOT.
4. Compile, then write the image without resetting:

```bash
esphome compile skyclock-panel.yaml
python ../tools/flash-when-ready.py      # waits for "waiting for download", then flashes
```

`tools/flash-when-ready.py` holds the port open with DTR/RTS low, so opening it does not reset the chip. It prints the chip's boot line (`boot:0x0 (DOWNLOAD...)` means download mode). Then it runs `esptool --before no_reset write_flash 0x0 firmware.factory.bin`. Edit the paths at the top of the script for your setup.

## 4. Later flashes over Wi-Fi

```bash
esphome upload skyclock-panel.yaml --device <panel-ip>
esphome logs   skyclock-panel.yaml --device <panel-ip>
```

## Log lines

- `IMAGE OK #N`: one good fetch.
- `IMAGE ERROR #N`: a failed fetch or decode. One at boot, before Wi-Fi is up, is normal.
- `HEARTBEAT up=... ok=... err=... last_ok=...s ago heap=... psram=... rssi=...`: every 60 s.
- `STALE: ... Rebooting.`: no good image for 3 refresh intervals. This guards against the known `online_image` hang (esphome/issues#6530).

## Things that did not work

- **PSRAM at 120 MHz plus a 21 MHz pixel clock** (meant to reduce flicker): far worse flicker, and a panic on boot. Keep `psram: speed: 80MHz` and the preset's 16 MHz pixel clock. Change one variable at a time if you experiment.
