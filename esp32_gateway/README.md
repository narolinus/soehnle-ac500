# ESP32 Gateway for Soehnle AC500

This directory contains an alternative approach to the Home Assistant custom
integration: an `ESP32 + ESPHome` gateway that talks to one or two nearby
Soehnle AC500 purifiers over BLE and exposes them back to Home Assistant over
the normal ESPHome API.

The rest of the repository is intentionally untouched. Everything for this
approach lives below `esp32_gateway/`.

## What this does

- Uses an `ESP32` as the local BLE controller for nearby AC500 purifiers
- Keeps one active `ble_client` connection per purifier
- Exposes each purifier as its own HA sub-device via `esphome.devices`
- Publishes the current state continuously from the live notification frames
- Supports pairing, reconnecting, refreshing, and all confirmed control
  commands

## Current entity set per purifier

- `switch`
  - Power
  - UV-C
  - Auto
  - Night
  - Buzzer
- `select`
  - Fan Mode
  - Timer
- `button`
  - Pair
  - Refresh
  - Reconnect
- `sensor`
  - PM2.5
  - Temperature
  - Filter
  - RSSI
- `binary_sensor`
  - Connected
- `text_sensor`
  - Last Frame

## Hardware

- Recommended: classic `ESP32` boards such as `Node32`, `esp32dev`,
  `ESP32 Mini`
- Not suitable: `ESP8266`
- Practical target: `1-2 AC500` per ESP32

The official ESPHome BLE client documentation notes a maximum of three active
BLE clients on ESP32, but for this use case `1-2` is the realistic target for
stability.

## Files

- [single_ac500_gateway.yaml](/workspace/HomeAssistant/SoehnleLR/esp32_gateway/single_ac500_gateway.yaml)
- [dual_ac500_gateway.yaml](/workspace/HomeAssistant/SoehnleLR/esp32_gateway/dual_ac500_gateway.yaml)
- [packages/ac500_device.yaml](/workspace/HomeAssistant/SoehnleLR/esp32_gateway/packages/ac500_device.yaml)
- [includes/soehnle_ac500_helpers.h](/workspace/HomeAssistant/SoehnleLR/esp32_gateway/includes/soehnle_ac500_helpers.h)

## How to use

1. Copy either `single_ac500_gateway.yaml` or `dual_ac500_gateway.yaml` into
   your ESPHome setup.
2. Adjust Wi-Fi secrets, MAC addresses, names, and board if needed.
3. Flash the ESP32.
4. Adopt it in Home Assistant through ESPHome.
5. Open the created AC500 device in Home Assistant and press the `Pair` button.
6. While the pairing action is running, press the Bluetooth button on the
   purifier.

After the handshake completes, the ESP32 should keep the connection and expose
live state plus controls.

If a purifier stays unknown after the first flash, the most useful diagnostic
entity is the generated `${ac500_name} State` text sensor. It should typically
move through values such as `connecting`, `connected`, `status_received`,
`pairing`, `pair_ack`, or `pair_timeout`.

## Notes

- The BLE MAC addresses are configured manually for now.
- Pairing is still a physical step on the purifier side.
- The package deliberately prefers standard ESPHome building blocks over a
  custom external component so it remains easier to inspect and adapt.
- `Fan Mode` is modeled as a `select` because the purifier has a separate
  `Auto` mode switch and the current raw speed is still reported while auto is
  active.

## Suggested first setup

For the first device, start with:

- [single_ac500_gateway.yaml](/workspace/HomeAssistant/SoehnleLR/esp32_gateway/single_ac500_gateway.yaml)
- one ESP32 near one purifier
- DEBUG logs enabled

Once that is stable, move to the dual setup.

For the dual setup, keep the first rollout simple:

- set both names and MAC addresses in [dual_ac500_gateway.yaml](/workspace/HomeAssistant/SoehnleLR/esp32_gateway/dual_ac500_gateway.yaml)
- bring both purifiers online first and confirm live status updates
- pair them one after the other, not at the same time
- trigger control tests one purifier at a time while DEBUG logs are still enabled

## If memory gets tight

The example YAMLs currently keep `logger: DEBUG` and `web_server:` enabled
because that makes first setup and pairing easier. If your board gets unstable
or reboots under load, reduce memory pressure in this order:

- set `logger.level` to `INFO`
- disable `web_server:`
- stay with one purifier per ESP32
