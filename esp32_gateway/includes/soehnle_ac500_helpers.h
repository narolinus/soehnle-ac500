#pragma once

#include "esphome/components/ble_client/ble_client.h"

#include <cstdint>
#include <string>
#include <vector>

namespace soehnle_ac500 {

static constexpr uint16_t DISCOVERY_SERVICE_UUID16 = 0xFFA0;
static constexpr uint16_t WRITE_CHAR_UUID16 = 0xEF01;
static const char *const DISCOVERY_SERVICE_UUID = "0000ffa0-0000-1000-8000-00805f9b34fb";
static const char *const WRITE_CHAR_UUID = "0000ef01-0000-1000-8000-00805f9b34fb";
static const char *const LIVE_DATA_CHAR_UUID = "0000ef02-0000-1000-8000-00805f9b34fb";
static const char *const ACK_CHAR_UUID = "0000ef03-0000-1000-8000-00805f9b34fb";

static constexpr uint8_t FRAME_LENGTH = 0x03;

struct Status {
  bool valid{false};
  uint8_t frame_variant{0};
  uint8_t fan_raw{0};
  uint8_t timer_raw{0};
  uint8_t flags_raw{0};
  uint16_t pm_raw{0};
  float pm25_ug_m3{0.0f};
  uint8_t temperature_raw{0};
  float temperature_c{0.0f};
  uint16_t filter_raw{0};
  float filter_percent{0.0f};

  bool power_enabled() const { return (this->flags_raw & 0x01U) != 0; }
  bool uv_enabled() const { return (this->flags_raw & 0x02U) != 0; }
  bool timer_enabled() const { return (this->flags_raw & 0x04U) != 0; }
  bool buzzer_enabled() const { return (this->flags_raw & 0x08U) != 0; }
  bool auto_enabled() const { return (this->flags_raw & 0x20U) != 0; }
  bool night_enabled() const { return (this->flags_raw & 0x40U) != 0; }
};

inline std::string to_hex(const std::vector<uint8_t> &frame);

inline uint8_t checksum(uint8_t opcode, uint8_t arg1, uint8_t arg2) {
  return static_cast<uint8_t>((FRAME_LENGTH + opcode + arg1 + arg2) & 0xFFU);
}

inline uint8_t frame_checksum(uint8_t length, const std::vector<uint8_t> &payload) {
  uint32_t total = length;
  for (uint8_t byte : payload) {
    total += byte;
  }
  return static_cast<uint8_t>(total & 0xFFU);
}

inline std::vector<uint8_t> build_frame(uint8_t opcode, uint8_t arg1 = 0x00, uint8_t arg2 = 0x00) {
  return {
      0xAA,
      FRAME_LENGTH,
      opcode,
      arg1,
      arg2,
      checksum(opcode, arg1, arg2),
      0xEE,
  };
}

inline bool write_frame(esphome::ble_client::BLEClient *client, const std::vector<uint8_t> &frame) {
  if (client == nullptr) {
    ESP_LOGW("soehnle_ac500", "TX %s fehlgeschlagen: client=null", to_hex(frame).c_str());
    return false;
  }

  auto *chr = client->get_characteristic(DISCOVERY_SERVICE_UUID16, WRITE_CHAR_UUID16);
  if (chr == nullptr) {
    ESP_LOGW("soehnle_ac500", "TX %s fehlgeschlagen: write-char nicht gefunden", to_hex(frame).c_str());
    return false;
  }

  ESP_LOGI("soehnle_ac500", "TX %s", to_hex(frame).c_str());
  auto *data = const_cast<uint8_t *>(frame.data());
  esp_err_t err = chr->write_value(data, static_cast<int16_t>(frame.size()), ESP_GATT_WRITE_TYPE_RSP);
  if (err != ESP_OK) {
    ESP_LOGW("soehnle_ac500", "TX %s fehlgeschlagen: err=%d", to_hex(frame).c_str(), static_cast<int>(err));
  }
  return err == ESP_OK;
}

inline bool ensure_ble_link_paired(esphome::ble_client::BLEClient *client) {
  if (client == nullptr) {
    ESP_LOGW("soehnle_ac500", "BLE-Link-Pairing fehlgeschlagen: client=null");
    return false;
  }

  if (client->is_paired()) {
    ESP_LOGI("soehnle_ac500", "BLE-Link bereits gepairt");
    return true;
  }

  ESP_LOGI("soehnle_ac500", "BLE-Link-Pairing wird angefordert");
  esp_err_t err = client->pair();
  if (err != ESP_OK) {
    ESP_LOGW("soehnle_ac500", "BLE-Link-Pairing Start fehlgeschlagen: err=%d", static_cast<int>(err));
    return false;
  }
  return true;
}

inline bool write_command(
    esphome::ble_client::BLEClient *client,
    uint8_t opcode,
    uint8_t arg1 = 0x00,
    uint8_t arg2 = 0x00) {
  return write_frame(client, build_frame(opcode, arg1, arg2));
}

inline bool validate_frame(const std::vector<uint8_t> &frame) {
  if (frame.size() < 6) {
    return false;
  }
  if (frame.front() != 0xAA || frame.back() != 0xEE) {
    return false;
  }
  const uint8_t length = frame[1];
  if (frame.size() != static_cast<size_t>(length) + 4U) {
    return false;
  }
  std::vector<uint8_t> payload(frame.begin() + 2, frame.end() - 2);
  const uint8_t expected = frame_checksum(length, payload);
  return frame[frame.size() - 2] == expected;
}

inline bool is_live_frame(const std::vector<uint8_t> &frame) {
  return validate_frame(frame) && frame.size() >= 16U && frame[2] == 0xA0 &&
         (frame[3] == 0x21 || frame[3] == 0x22);
}

inline bool is_pair_ack(const std::vector<uint8_t> &frame) {
  return frame == build_frame(0xA2, 0x00, 0x02);
}

inline bool parse_live_frame(const std::vector<uint8_t> &frame, Status *out) {
  if (out == nullptr || !is_live_frame(frame)) {
    return false;
  }

  out->valid = true;
  out->frame_variant = frame[3];
  out->fan_raw = frame[4];
  out->timer_raw = frame[5];
  out->flags_raw = frame[6];

  if (frame[3] == 0x21) {
    out->pm_raw = static_cast<uint16_t>(frame[8] | (frame[9] << 8));
  } else {
    out->pm_raw = static_cast<uint16_t>((frame[7] << 8) | frame[8]);
  }

  out->pm25_ug_m3 = static_cast<float>(out->pm_raw) / 10.0f;
  out->temperature_raw = frame[10];
  out->temperature_c = static_cast<float>(out->temperature_raw) / 10.0f;
  out->filter_raw = static_cast<uint16_t>(frame[12] | (frame[13] << 8));
  out->filter_percent = (static_cast<float>(out->filter_raw) / 4320.0f) * 100.0f;
  return true;
}

inline std::string fan_label(uint8_t raw) {
  switch (raw) {
    case 0:
      return "Low";
    case 1:
      return "Medium";
    case 2:
      return "High";
    case 3:
      return "Turbo";
    default:
      return "";
  }
}

inline uint8_t fan_value_from_label(const std::string &label) {
  if (label == "Low") {
    return 0;
  }
  if (label == "Medium") {
    return 1;
  }
  if (label == "High") {
    return 2;
  }
  if (label == "Turbo") {
    return 3;
  }
  return 0;
}

inline std::string timer_label(uint8_t raw) {
  switch (raw) {
    case 0:
      return "Off";
    case 2:
      return "2 h";
    case 4:
      return "4 h";
    case 8:
      return "8 h";
    default:
      return "";
  }
}

inline uint8_t timer_value_from_label(const std::string &label) {
  if (label == "Off") {
    return 0;
  }
  if (label == "2 h") {
    return 2;
  }
  if (label == "4 h") {
    return 4;
  }
  if (label == "8 h") {
    return 8;
  }
  return 0;
}

inline std::string to_hex(const std::vector<uint8_t> &frame) {
  static const char *const HEX = "0123456789abcdef";
  std::string out;
  out.reserve(frame.size() * 2U);

  for (uint8_t byte : frame) {
    out.push_back(HEX[(byte >> 4U) & 0x0FU]);
    out.push_back(HEX[byte & 0x0FU]);
  }

  return out;
}

}  // namespace soehnle_ac500
