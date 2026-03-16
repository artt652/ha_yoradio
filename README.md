# ёRadio — Home Assistant Integration

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![GitHub release](https://img.shields.io/github/release/artt652/ha_yoradio.svg)](https://github.com/artt652/ha_yoradio/releases)
![Maintenance](https://img.shields.io/maintenance/yes/2025.svg)

A Home Assistant custom integration for [yoRadio](https://github.com/e2002/yoradio) — an internet-radio firmware for ESP32/ESP8266 devices. Control your yoRadio device directly from Home Assistant via MQTT.

---

## Features

- Full media-player controls: play, pause, stop, next/previous, volume
- Station list populated automatically from the device playlist
- Album-art fetched from the iTunes Search API
- Browse & play from Home Assistant media sources
- UI-based setup via **Settings → Devices & Services** (no YAML required)
- Supports multiple yoRadio devices simultaneously

---

## Requirements

- Home Assistant 2023.1 or newer  
- [MQTT integration](https://www.home-assistant.io/integrations/mqtt/) configured  
- A yoRadio device with MQTT enabled

---

## Installation

### Via HACS (recommended)

1. Open **HACS → Integrations**.
2. Click the three-dot menu in the top-right and choose **Custom repositories**.
3. Add `https://github.com/artt652/ha_yoradio` with category **Integration**.
4. Search for **ёRadio** and click **Download**.
5. Restart Home Assistant.

### Manual

1. Copy the `custom_components/yoradio` folder into your
   `<config>/custom_components/` directory.
2. Restart Home Assistant.

---

## Configuration

1. Go to **Settings → Devices & Services → Add Integration**.
2. Search for **ёRadio**.
3. Fill in:

| Field | Default | Description |
|---|---|---|
| **MQTT Root Topic** | `yoradio` | Must match the topic set in yoRadio firmware |
| **Device Name** | `yoRadio` | Friendly name shown in HA |
| **Max Volume** | `254` | Raw maximum volume value of your device |

> To add a second device, repeat step 1–3 with a different root topic.

---

## MQTT Topics

The integration subscribes to:

| Topic | Direction | Content |
|---|---|---|
| `<root>/status` | Device → HA | JSON: `title`, `name`, `station`, `on`, `status` |
| `<root>/volume` | Device → HA | Integer volume value |
| `<root>/playlist` | Device → HA | URL of the M3U/TSV playlist |
| `<root>/command` | HA → Device | Command strings (see below) |

### Commands sent

`play <n>`, `next`, `prev`, `stop`, `start`, `vol <n>`, `turnon`, `turnoff`

---

## Options

After initial setup, options (name, max volume) can be changed via  
**Settings → Devices & Services → ёRadio → Configure**.

---

## License

[GNU General Public License v3.0](LICENSE)
