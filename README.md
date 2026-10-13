# silverbullet-paper-window

Pi Zero W + Waveshare 2.13" e-paper: shows unchecked SilverBullet tasks one at a time; three buttons: next, previous, done (writes `[x]` back to the page).

Files: `sb_eink.py` (script), `config.example.toml`, `sb-eink.service`, `test_sb_eink.py`.

## Setup checklist

1. `sudo raspi-config` -> Interface Options -> SPI -> enable. Reboot.
2. Dependencies (32-bit Raspberry Pi OS, armv6l):
   `sudo apt install python3-pil python3-gpiozero python3-spidev python3-lgpio fonts-dejavu-core git`
   (Python < 3.11 also needs `tomli`.) No numpy needed.
3. Waveshare driver: `sudo git clone --depth 1 https://github.com/waveshareteam/e-Paper /opt/e-Paper` (the unit file sets `PYTHONPATH` to its `RaspberryPi_JetsonNano/python/lib`; export the same for manual runs).
4. `sudo mkdir /opt/sb-eink && sudo cp sb_eink.py /opt/sb-eink/`
5. `sudo cp config.example.toml /etc/sb-eink.toml`, edit it, `sudo chown pi /etc/sb-eink.toml && sudo chmod 600 /etc/sb-eink.toml`. Set `epd_revision` (printed on the back of the HAT).
6. `sudo cp sb-eink.service /etc/systemd/system/ && sudo systemctl enable --now sb-eink` (adjust `User=`).
7. Desktop test (needs Pillow, no hardware): `python3 sb_eink.py --dry-run --page-file Tasks.md`, or add `--config` to use the server. Type `n`/`p`/`d`/`q` + Enter. Output goes to `eink.png`, the PUT diff is printed, nothing is written. Unit tests: `python3 -m unittest`.

## Button wiring (BCM numbers)

The HAT uses RST 17, DC 25, CS 8 (CE0), BUSY 24, PWR 18, MOSI 10, SCLK 11 (from `waveshare_epd/epdconfig.py`). The defaults avoid those, plus MISO 9 and CE1 7. Each button connects between the GPIO and any GND (internal pull-up).

| Button   | BCM | Physical pin | Nearby GND pin |
|----------|-----|--------------|----------------|
| next     | 5   | 29           | 30             |
| previous | 6   | 31           | 30             |
| done     | 13  | 33           | 34             |

Configurable via `pin_next`, `pin_prev`, `pin_done`.

## Step 0: SilverBullet API

Source: SilverBullet "HTTP API" page (docs.silverbullet.md, current docs; they state no version, so your server version is not confirmed).

Verified:
- `GET /.fs` lists files; `GET /.fs/<path>` reads; `PUT /.fs/<path>` writes; `DELETE` deletes.
- Bearer auth: `Authorization: Bearer <token>` when `SB_AUTH_TOKEN` is set. Cookie login: `POST /.auth` with form fields `username`, `password`, returns JSON `{"status":"ok"}`.
- Conditional writes: GET/PUT return `ETag: "sha256:<hash>"`; PUT accepts `If-Match` and answers `412` on mismatch.
- Clients always send `X-Sync-Mode: true`; the server uses it to tell client requests from browser navigation.

Not verified (configurable or handled defensively):
- Whether `X-Sync-Mode` is strictly required on `/.fs`. It is sent by default (`send_sync_mode`).
- The `/.fs` listing JSON (the script does not use it).
- Multi-space deployments: whether `/.fs` sits under another prefix (`fs_prefix`, or put the prefix in `base_url`).
- Status codes for unauthenticated requests: any 3xx/401/403 is treated as "Auth failed". Redirects are never followed, so a login page cannot be mistaken for the page.
- If no ETag is returned, the script re-reads and compares just before the PUT; a small race window remains between that read and the PUT.

## Step 0: display revision

Source: `waveshareteam/e-Paper`, `RaspberryPi_JetsonNano/python/lib/waveshare_epd/`.

| Revision | Module | Partial refresh | Calls used |
|----------|--------|-----------------|------------|
| V4 (example default) | `epd2in13_V4` | yes (`init_fast`/`display_fast` exist, unused) | full: `init()`, `displayPartBaseImage(buf)`; partial: `displayPartial(buf)`; then `sleep()` |
| V3 | `epd2in13_V3` | yes | same as V4 |
| V2 | `epd2in13_V2` | yes | full: `init(FULL_UPDATE)`, `displayPartBaseImage`; partial: `init(PART_UPDATE)`, `displayPartial`; `sleep()` |
| b_V4 (tri-color) | `epd2in13b_V4` | no | `init()`, `display(black, blank_red)`, `sleep()` every time |

Native size is 122x250 portrait; `getbuffer()` takes a 250x122 landscape image and rotates it. `sleep()` enters deep sleep and drops the PWR pin. The older `epd2in13bc` (104x212) is not supported. `epd_revision` has no built-in default in code; the script exits with a message if it is unset or unsupported. `V4` is the value in the example config.

## Assumptions / not tested on hardware

- Partial refresh after `sleep()` assumes display RAM survives deep sleep (Waveshare's demo does not sleep between partial updates). If partial updates look wrong, set `full_refresh_every = 1`.
- `done` is ignored when offline, and when pressed while the screen was still updating.
- Task identity is its text; duplicate unchecked text (with tag) aborts with "Duplicate task".
- TLS handshakes are slow on ARMv6; plain LAN HTTP avoids that. Verification is on unless `verify_tls = false`.
- Wi-Fi drops: the last page is cached to `cache_file`, shown with an "OFFLINE" marker, and retried every `retry_interval`.
- The script was only run in `--dry-run` mode and via unit tests; no SilverBullet server or panel was available.
