#include <Arduino.h>
#include <Wire.h>
#include "hardware/clocks.h"
#include "config.h"
#include "adf4351.h"
#include "mcp4725.h"
#include "gps.h"
#include "discipliner.h"
#include "status.h"
#include "pio_timing.h"
#include <EEPROM.h>
#include <LittleFS.h>
#include <ArduinoJson.h>
#include <math.h>
#include "hardware/gpio.h"
#include "hardware/pwm.h"
#include "pico/multicore.h"

// Firmware version
#ifndef FW_VERSION_STRING
#define FW_VERSION_STRING "dev"
#endif
static const char* FW_VERSION = FW_VERSION_STRING;

// Helper: emit a compact JSON response to Serial with an optional newline
static void sendJsonMessage(const char* status, const char* msg = nullptr) {
    StaticJsonDocument<192> d;
    d["status"] = status;
    if (msg) d["msg"] = msg;
    serializeJson(d, Serial);
    Serial.println();
}

static void sendJsonKV(const char* status, const char* key, int value) {
    StaticJsonDocument<192> d;
    d["status"] = status;
    d[key] = value;
    serializeJson(d, Serial);
    Serial.println();
}

static void sendTrace(const char* cmd, const char* step) {
#if ADF_TRACE_ENABLED
    StaticJsonDocument<192> d;
    d["status"] = "debug";
    d["event"] = "trace";
    d["cmd"] = cmd;
    d["step"] = step;
    serializeJson(d, Serial);
    Serial.println();
#else
    (void)cmd;
    (void)step;
#endif
}

// Mutable ADF register arrays (initialised from config defaults)
static uint32_t adf1_regs[6];
static uint32_t adf2_regs[6];
static bool adfEepromCommitPending = false;
static uint32_t adfEepromPendingMask = 0;
static uint32_t adfEepromCommitDueMs = 0;
static bool adfFsReady = false;
static bool adf1PersistDirty = false;
static bool adf2PersistDirty = false;
static uint32_t g_discAverageSecs = DISC_AVERAGE_SECS;

// ── EFC calibration state machine ─────────────────────────────────────────
enum class EFCCalState { IDLE, SETTLE_LOW, READ_LOW, SETTLE_HIGH, READ_HIGH };
static EFCCalState  g_efcCalState   = EFCCalState::IDLE;
static uint32_t     g_efcCalTimer   = 0;
static int32_t      g_efcCalFreqLow = 0;   // ppb at DAC_MIN
static int32_t      g_efcCalFreqHigh= 0;   // ppb at DAC_MAX
static const uint32_t EFC_CAL_SETTLE_MS = 15000;
static uint32_t     g_efcCalSettleMs  = EFC_CAL_SETTLE_MS;  // runtime settle time

// Forward declarations for global instances (defined later in this file)
extern Discipliner disc;
extern StatusManager status;

struct DiscCtrlBlob {
    uint32_t magic;
    uint32_t version;
    uint32_t avgWindowSecs;
    float pGain;
    float iGain;
};

struct ADFRegsBlob {
    uint32_t magic;
    uint32_t version;
    uint32_t regs[6];
};

static const char* adfPersistPath(uint32_t eepromAddr) {
    return (eepromAddr == ADF1_EEPROM_ADDR) ? "/adf1_regs.bin" : "/adf2_regs.bin";
}

static uint32_t adfPersistDirtyMask() {
    return (adf1PersistDirty ? 0x1u : 0u) | (adf2PersistDirty ? 0x2u : 0u);
}

static void scheduleADFEEPROMCommit() {
    adfEepromCommitPending = true;
    adfEepromPendingMask = adfPersistDirtyMask();
    adfEepromCommitDueMs = millis() + (uint32_t)ADF_EEPROM_COMMIT_DELAY_MS;
}

static void stageADFRegsPersist(uint32_t eepromAddr) {
    if (eepromAddr == ADF1_EEPROM_ADDR) adf1PersistDirty = true;
    if (eepromAddr == ADF2_EEPROM_ADDR) adf2PersistDirty = true;
}

static void writeADFRegsEEPROMBlock(const uint32_t regs[6], uint32_t eepromAddr) {
    uint32_t magic = ADF_REGS_MAGIC;
    uint32_t version = ADF_REGS_VERSION;
    EEPROM.put(eepromAddr, magic);
    EEPROM.put(eepromAddr + sizeof(magic), version);
    for (int i = 0; i < 6; ++i) {
        EEPROM.put(eepromAddr + sizeof(magic) + sizeof(version) + i * sizeof(uint32_t), regs[i]);
    }
}

static bool commitStagedADFRegsToEEPROM() {
    const uint32_t mask = adfPersistDirtyMask();
    if (mask == 0) {
        return true;
    }

    if (adf1PersistDirty) writeADFRegsEEPROMBlock(adf1_regs, ADF1_EEPROM_ADDR);
    if (adf2PersistDirty) writeADFRegsEEPROMBlock(adf2_regs, ADF2_EEPROM_ADDR);

    sendTrace("adf_persist", "commit_pre");
    bool committed = false;
#if ADF_EEPROM_USE_LOCKOUT
    sendTrace("adf_persist", "lockout_enter");
    multicore_lockout_start_blocking();
    committed = EEPROM.commit();
    multicore_lockout_end_blocking();
    sendTrace("adf_persist", "lockout_exit");
#else
    // Gate PIO interrupt sources globally during commit.
    sendTrace("adf_persist", "pio_irq_src_off");
    pio_set_irq0_source_enabled(pio0, pis_interrupt0, false);
    pio_set_irq1_source_enabled(pio0, pis_interrupt1, false);

    sendTrace("adf_persist", "commit_call");
    committed = EEPROM.commit();
    sendTrace("adf_persist", "commit_return");

    pio_set_irq0_source_enabled(pio0, pis_interrupt0, true);
    pio_set_irq1_source_enabled(pio0, pis_interrupt1, true);
    sendTrace("adf_persist", "pio_irq_src_on");
#endif
    sendTrace("adf_persist", committed ? "commit_ok" : "commit_fail");

    if (committed) {
        StaticJsonDocument<192> dj;
        dj["status"] = "info";
        dj["event"] = "eeprom_write_success";
        dj["component"] = "adf_persist";
        dj["mask"] = mask;
        serializeJson(dj, Serial);
        Serial.println();

        adf1PersistDirty = false;
        adf2PersistDirty = false;
    }
    return committed;
}

static bool commitEEPROMNow() {
    bool committed = false;
#if ADF_EEPROM_USE_LOCKOUT
    multicore_lockout_start_blocking();
    committed = EEPROM.commit();
    multicore_lockout_end_blocking();
#else
    pio_set_irq0_source_enabled(pio0, pis_interrupt0, false);
    pio_set_irq1_source_enabled(pio0, pis_interrupt1, false);
    committed = EEPROM.commit();
    pio_set_irq0_source_enabled(pio0, pis_interrupt0, true);
    pio_set_irq1_source_enabled(pio0, pis_interrupt1, true);
#endif
    return committed;
}

static bool saveDiscCtrlSettings(bool emitJson = true) {
    DiscCtrlBlob blob{};
    blob.magic = DISC_CTRL_MAGIC;
    blob.version = DISC_CTRL_VERSION;
    blob.avgWindowSecs = g_discAverageSecs;
    blob.pGain = disc.pGain();
    blob.iGain = disc.iGain();

    EEPROM.put(DISC_CTRL_EEPROM_ADDR, blob);
    bool committed = commitEEPROMNow();

    if (emitJson) {
        StaticJsonDocument<224> dj;
        dj["status"] = committed ? "info" : "error";
        dj["event"] = "saved_disc_ctrl";
        dj["committed"] = committed;
        dj["avg_window_s"] = blob.avgWindowSecs;
        dj["p_gain"] = blob.pGain;
        dj["i_gain"] = blob.iGain;
        serializeJson(dj, Serial);
        Serial.println();
    }
    return committed;
}

static bool loadDiscCtrlSettings(bool emitJson = true) {
    DiscCtrlBlob blob{};
    EEPROM.get(DISC_CTRL_EEPROM_ADDR, blob);

    bool ok = (blob.magic == DISC_CTRL_MAGIC) && (blob.version == DISC_CTRL_VERSION);
    ok = ok && (blob.avgWindowSecs >= DISC_AVERAGE_SECS_MIN) && (blob.avgWindowSecs <= DISC_AVERAGE_SECS_MAX);
    ok = ok && disc.setLoopGains(blob.pGain, blob.iGain);

    if (ok) {
        g_discAverageSecs = blob.avgWindowSecs;
    } else {
        g_discAverageSecs = DISC_AVERAGE_SECS;
        disc.setLoopGains(DISC_P_GAIN, DISC_I_GAIN);
    }
    status.setDiscAvgWindowSecs(g_discAverageSecs);

    if (emitJson) {
        StaticJsonDocument<224> dj;
        dj["status"] = ok ? "info" : "warning";
        dj["event"] = ok ? "loaded_disc_ctrl" : "using_default_disc_ctrl";
        dj["avg_window_s"] = g_discAverageSecs;
        dj["p_gain"] = disc.pGain();
        dj["i_gain"] = disc.iGain();
        serializeJson(dj, Serial);
        Serial.println();
    }
    return ok;
}
// Presence/attempt tracking
enum class PeripheralStatus { PS_UNKNOWN=0, PS_OK=1, PS_ABSENT=2 };
static PeripheralStatus haveDAC = PeripheralStatus::PS_UNKNOWN;
static PeripheralStatus haveADF1 = PeripheralStatus::PS_UNKNOWN;
static PeripheralStatus haveADF2 = PeripheralStatus::PS_UNKNOWN;
static int adf1_attempts = 0;
static int adf2_attempts = 0;
static const int MAX_ADF_ATTEMPTS = 3;

// forward declarations for CLI helper usage (instances defined below)
extern ADF4351 adf1;
extern ADF4351 adf2;
extern Discipliner disc;

// Helper: load regs from EEPROM if magic/version match, otherwise use defaults
static void loadADFRegs(uint32_t regs[6], const uint32_t defaults[6], uint32_t eepromAddr) {
    if (adfFsReady) {
        const char* path = adfPersistPath(eepromAddr);
        File f = LittleFS.open(path, "r");
        if (f) {
            ADFRegsBlob blob{};
            size_t n = f.read((uint8_t*)&blob, sizeof(blob));
            f.close();
            if (n == sizeof(blob) && blob.magic == ADF_REGS_MAGIC && blob.version == ADF_REGS_VERSION) {
                for (int i = 0; i < 6; ++i) regs[i] = blob.regs[i];
                StaticJsonDocument<192> dj;
                dj["status"] = "info";
                dj["event"] = "loaded_adf_regs";
                dj["path"] = path;
                serializeJson(dj, Serial);
                Serial.println();
                return;
            }
        }
    }

    // Fallback: read persisted ADF registers from EEPROM block format.
    uint32_t magic = 0;
    uint32_t version = 0;
    EEPROM.get((int)eepromAddr, magic);
    EEPROM.get((int)(eepromAddr + sizeof(magic)), version);
    if (magic == ADF_REGS_MAGIC && version == ADF_REGS_VERSION) {
        for (int i = 0; i < 6; ++i) {
            uint32_t v = 0;
            EEPROM.get((int)(eepromAddr + sizeof(magic) + sizeof(version) + i * sizeof(uint32_t)), v);
            regs[i] = v;
        }
        StaticJsonDocument<192> dj;
        dj["status"] = "info";
        dj["event"] = "loaded_adf_regs";
        dj["eeprom_addr"] = (unsigned)eepromAddr;
        serializeJson(dj, Serial);
        Serial.println();
        return;
    }

    for (int i = 0; i < 6; ++i) regs[i] = defaults[i];
    {
        StaticJsonDocument<192> dj;
        dj["status"] = "info";
        dj["event"] = "using_default_adf_regs";
        dj["eeprom_addr"] = (unsigned)eepromAddr;
        serializeJson(dj, Serial);
        Serial.println();
    }
}

static bool saveADFRegs(uint32_t regs[6], uint32_t eepromAddr) {
#if !ADF_PERSIST_EEPROM
    (void)regs;
    stageADFRegsPersist(eepromAddr);
#if ADF_AUTO_COMMIT_STAGED
    scheduleADFEEPROMCommit();
#endif
    {
        StaticJsonDocument<192> dj;
        dj["status"] = "info";
        dj["event"] = "saved_adf_regs";
        dj["eeprom_addr"] = (unsigned)eepromAddr;
        dj["staged"] = true;
        dj["pending_mask"] = adfPersistDirtyMask();
        dj["skipped"] = true;
        dj["committed"] = false;
#if ADF_AUTO_COMMIT_STAGED
        dj["auto_commit"] = true;
#else
        dj["auto_commit"] = false;
#endif
        serializeJson(dj, Serial);
        Serial.println();
    }
    return true;
#else
    if (!adfFsReady) {
        sendJsonMessage("error", "adf_persist_fs_not_ready");
        return false;
    }

    const char* path = adfPersistPath(eepromAddr);
    {
        StaticJsonDocument<160> dj;
        dj["status"] = "debug";
        dj["event"] = "adf_persist_write_start";
        dj["path"] = path;
        serializeJson(dj, Serial);
        Serial.println();
    }

    ADFRegsBlob blob{};
    blob.magic = ADF_REGS_MAGIC;
    blob.version = ADF_REGS_VERSION;
    for (int i = 0; i < 6; ++i) blob.regs[i] = regs[i];

    File f = LittleFS.open(path, "w");
    if (!f) {
        sendJsonMessage("error", "adf_persist_open_failed");
        return false;
    }
    size_t n = f.write((const uint8_t*)&blob, sizeof(blob));
    f.flush();
    f.close();
    bool committed = (n == sizeof(blob));

    {
        StaticJsonDocument<192> dj;
        dj["status"] = committed ? "info" : "error";
        dj["event"] = "saved_adf_regs";
        dj["path"] = path;
        dj["committed"] = committed;
        serializeJson(dj, Serial);
        Serial.println();
    }
    return committed;
#endif
}

static void serviceADFEEPROMCommit() {
#if !ADF_PERSIST_EEPROM
#if ADF_AUTO_COMMIT_STAGED
    if (!adfEepromCommitPending) return;

    if ((int32_t)(millis() - adfEepromCommitDueMs) < 0) return;

    const uint32_t pendingMask = adfEepromPendingMask;
    const bool committed = commitStagedADFRegsToEEPROM();

    StaticJsonDocument<192> dj;
    dj["status"] = committed ? "info" : "error";
    dj["event"] = "adf_persist_auto_commit";
    dj["committed"] = committed;
    dj["pending_mask"] = pendingMask;
    dj["remaining_mask"] = adfPersistDirtyMask();
    serializeJson(dj, Serial);
    Serial.println();

    adfEepromCommitPending = false;
    adfEepromPendingMask = 0;
#endif
    return;
#else
    // ADF persistence now uses LittleFS immediate file writes, no deferred EEPROM commit.
    return;

    if (!adfEepromCommitPending) return;

    // millis rollover-safe due-time check
    if ((int32_t)(millis() - adfEepromCommitDueMs) < 0) return;

    {
        StaticJsonDocument<96> dj;
        dj["status"] = "debug";
        dj["event"] = "eep_pre";
        dj["mask"] = adfEepromPendingMask;
        serializeJson(dj, Serial);
        Serial.println();
        Serial.flush();
        delay((uint32_t)ADF_EEPROM_DEBUG_SLEEP_MS);
    }

    bool committed = false;
#if ADF_EEPROM_USE_LOCKOUT
    {
        StaticJsonDocument<96> dj;
        dj["status"] = "debug";
        dj["event"] = "eep_lock";
        serializeJson(dj, Serial);
        Serial.println();
        Serial.flush();
        delay((uint32_t)ADF_EEPROM_DEBUG_SLEEP_MS);
    }
    multicore_lockout_start_blocking();
    committed = EEPROM.commit();
    multicore_lockout_end_blocking();
#else
    committed = EEPROM.commit();
#endif

    {
        StaticJsonDocument<96> dj;
        dj["status"] = committed ? "info" : "error";
        dj["event"] = "eep_done";
        dj["mask"] = adfEepromPendingMask;
        dj["ok"] = committed;
        serializeJson(dj, Serial);
        Serial.println();
        Serial.flush();
        delay((uint32_t)ADF_EEPROM_DEBUG_SLEEP_MS);
    }

    adfEepromCommitPending = false;
    adfEepromPendingMask = 0;
#endif
}

static bool programAndPersistADF(ADF4351& adf, uint32_t regs[6], uint32_t eepromAddr, PeripheralStatus availability) {
    bool programmed = false;
    if (availability == PeripheralStatus::PS_OK) {
        sendTrace("adf", "before_program");
        adf.program(regs);
        programmed = true;
        sendTrace("adf", "after_program");
    }
    sendTrace("adf", "before_save");
    saveADFRegs(regs, eepromAddr);
    sendTrace("adf", "after_save");
    return programmed;
}

// CLI helpers
static void printADFRegs(const char *name, uint32_t regs[6]) {
    StaticJsonDocument<384> dj;
    dj["status"] = "ok";
    dj["cmd"] = "adf_regs";
    dj["name"] = name;
    JsonArray arr = dj.createNestedArray("regs");
    for (int i = 0; i < 6; ++i) {
        arr.add(regs[i]);
    }
    serializeJson(dj, Serial);
    Serial.println();
}

// ------------------------------------------------------------------
// CLI RX FIFO buffering
// ------------------------------------------------------------------
static const int CLI_QUEUE_DEPTH = 16;
static String cliQueue[CLI_QUEUE_DEPTH];
static int cliQueueHead = 0;
static int cliQueueTail = 0;
static int cliQueueCount = 0;
static String cliRxLine;

static bool enqueueCLICommand(const String& cmd) {
    if (cliQueueCount >= CLI_QUEUE_DEPTH) return false;
    cliQueue[cliQueueTail] = cmd;
    cliQueueTail = (cliQueueTail + 1) % CLI_QUEUE_DEPTH;
    cliQueueCount++;
    return true;
}

static bool dequeueCLICommand(String& out) {
    if (cliQueueCount <= 0) return false;
    out = cliQueue[cliQueueHead];
    cliQueueHead = (cliQueueHead + 1) % CLI_QUEUE_DEPTH;
    cliQueueCount--;
    return true;
}

static void pollSerialIntoCLIQueue() {
    while (Serial.available()) {
        char ch = (char)Serial.read();
        if (ch == '\r') continue;
        if (ch == '\n') {
            String line = cliRxLine;
            cliRxLine = "";
            line.trim();
            if (line.length() == 0) continue;
            if (!enqueueCLICommand(line)) {
                sendJsonMessage("error", "cli_rx_fifo_overflow");
            }
        } else {
            cliRxLine += ch;
            if (cliRxLine.length() > 600) {
                // Prevent pathological growth on malformed input.
                cliRxLine = "";
                sendJsonMessage("error", "cli_line_too_long");
            }
        }
    }
}

static void handleCLI(String s) {
    s.trim();
    if (s.length() == 0) return;
    // If the input looks like JSON, parse it using ArduinoJson
    if (s.startsWith("{")) {
        StaticJsonDocument<768> doc;
        DeserializationError err = deserializeJson(doc, s);
        if (err) {
            sendJsonMessage("error", "JSON parse error");
            return;
        }
        const char* cmd = doc["cmd"];
        if (!cmd) return;
        if (strcmp(cmd, "adf1") == 0) {
            const char* action = doc["action"];
            if (!action) return;
            if (strcmp(action, "show") == 0) printADFRegs("ADF1", adf1_regs);
            else if (strcmp(action, "save") == 0) saveADFRegs(adf1_regs, ADF1_EEPROM_ADDR);
            else if (strcmp(action, "load") == 0) {
                loadADFRegs(adf1_regs, ADF1_REGS, ADF1_EEPROM_ADDR);
                if (haveADF1 == PeripheralStatus::PS_OK) {
                    adf1.program(adf1_regs);
                } else {
                    sendJsonMessage("info", "adf1_unavailable_skip_program");
                }
            }
            else if (strcmp(action, "program") == 0) {
                bool programmed = programAndPersistADF(adf1, adf1_regs, ADF1_EEPROM_ADDR, haveADF1);
                StaticJsonDocument<192> dj;
                dj["status"] = "ok";
                dj["cmd"] = "adf1";
                dj["action"] = "program";
                dj["programmed"] = programmed;
                serializeJson(dj, Serial);
                Serial.println();
            }
            else if (strcmp(action, "default") == 0) for (int i=0;i<6;i++) adf1_regs[i]=ADF1_REGS[i];
            else if (strcmp(action, "set_all") == 0) {
                JsonVariant regsVar = doc["regs"];
                if (!regsVar.is<JsonArray>()) {
                    sendJsonMessage("error", "set_all requires regs array");
                    return;
                }
                JsonArray regsArr = regsVar.as<JsonArray>();
                if (regsArr.size() != 6) {
                    sendJsonMessage("error", "set_all requires exactly 6 regs");
                    return;
                }
                uint32_t newRegs[6];
                int idx = 0;
                for (JsonVariant v : regsArr) {
                    if (!v.is<uint32_t>()) {
                        sendJsonMessage("error", "set_all regs must be uint32");
                        return;
                    }
                    newRegs[idx++] = v.as<uint32_t>();
                }
                for (int i = 0; i < 6; ++i) adf1_regs[i] = newRegs[i];

                bool doProgram = doc["program"] | true;
                bool programmed = false;
                if (doProgram) {
                    sendTrace("adf1", "set_all_before_program_persist");
                    programmed = programAndPersistADF(adf1, adf1_regs, ADF1_EEPROM_ADDR, haveADF1);
                    sendTrace("adf1", "set_all_after_program_persist");
                }

                StaticJsonDocument<192> dj;
                dj["status"] = "ok";
                dj["cmd"] = "adf1";
                dj["action"] = "set_all";
                dj["program"] = doProgram;
                dj["programmed"] = programmed;
                serializeJson(dj, Serial);
                Serial.println();
            }
            else if (strcmp(action, "set") == 0) {
                int idx = doc["index"] | -1;
                uint32_t val = doc["value"] | 0;
                if (idx >=0 && idx < 6) {
                    adf1_regs[idx] = val;
                    StaticJsonDocument<192> dj;
                    dj["status"] = "ok";
                    dj["cmd"] = "adf1";
                    dj["index"] = idx;
                    dj["value"] = val;
                    serializeJson(dj, Serial);
                    Serial.println();
                }
            }
        } else if (strcmp(cmd, "adf2") == 0) {
            const char* action = doc["action"];
            if (!action) return;
            if (strcmp(action, "show") == 0) printADFRegs("ADF2", adf2_regs);
            else if (strcmp(action, "save") == 0) saveADFRegs(adf2_regs, ADF2_EEPROM_ADDR);
            else if (strcmp(action, "load") == 0) {
                loadADFRegs(adf2_regs, ADF2_REGS, ADF2_EEPROM_ADDR);
                if (haveADF2 == PeripheralStatus::PS_OK) {
                    adf2.program(adf2_regs);
                } else {
                    sendJsonMessage("info", "adf2_unavailable_skip_program");
                }
            }
            else if (strcmp(action, "program") == 0) {
                bool programmed = programAndPersistADF(adf2, adf2_regs, ADF2_EEPROM_ADDR, haveADF2);
                StaticJsonDocument<192> dj;
                dj["status"] = "ok";
                dj["cmd"] = "adf2";
                dj["action"] = "program";
                dj["programmed"] = programmed;
                serializeJson(dj, Serial);
                Serial.println();
            }
            else if (strcmp(action, "default") == 0) for (int i=0;i<6;i++) adf2_regs[i]=ADF2_REGS[i];
            else if (strcmp(action, "set_all") == 0) {
                JsonVariant regsVar = doc["regs"];
                if (!regsVar.is<JsonArray>()) {
                    sendJsonMessage("error", "set_all requires regs array");
                    return;
                }
                JsonArray regsArr = regsVar.as<JsonArray>();
                if (regsArr.size() != 6) {
                    sendJsonMessage("error", "set_all requires exactly 6 regs");
                    return;
                }
                uint32_t newRegs[6];
                int idx = 0;
                for (JsonVariant v : regsArr) {
                    if (!v.is<uint32_t>()) {
                        sendJsonMessage("error", "set_all regs must be uint32");
                        return;
                    }
                    newRegs[idx++] = v.as<uint32_t>();
                }
                for (int i = 0; i < 6; ++i) adf2_regs[i] = newRegs[i];

                bool doProgram = doc["program"] | true;
                bool programmed = false;
                if (doProgram) {
                    sendTrace("adf2", "set_all_before_program_persist");
                    programmed = programAndPersistADF(adf2, adf2_regs, ADF2_EEPROM_ADDR, haveADF2);
                    sendTrace("adf2", "set_all_after_program_persist");
                }

                StaticJsonDocument<192> dj;
                dj["status"] = "ok";
                dj["cmd"] = "adf2";
                dj["action"] = "set_all";
                dj["program"] = doProgram;
                dj["programmed"] = programmed;
                serializeJson(dj, Serial);
                Serial.println();
            }
            else if (strcmp(action, "set") == 0) {
                int idx = doc["index"] | -1;
                uint32_t val = doc["value"] | 0;
                if (idx >=0 && idx < 6) {
                    adf2_regs[idx] = val;
                    StaticJsonDocument<192> dj;
                    dj["status"] = "ok";
                    dj["cmd"] = "adf2";
                    dj["index"] = idx;
                    dj["value"] = val;
                    serializeJson(dj, Serial);
                    Serial.println();
                }
            }
        } else if (strcmp(cmd, "dac") == 0) {
            // Accept numeric value, named presets, or "resume"
            int val = -1;
            bool doResume = false;
            if (doc["value"].is<int>()) {
                val = doc["value"].as<int>();
            } else if (doc["value"].is<const char*>()) {
                const char* preset = doc["value"].as<const char*>();
                if      (strcmp(preset, "min")    == 0) val = DAC_MIN;
                else if (strcmp(preset, "max")    == 0) val = DAC_MAX;
                else if (strcmp(preset, "centre") == 0 ||
                         strcmp(preset, "center") == 0) val = DAC_CENTRE;
                else if (strcmp(preset, "resume") == 0) doResume = true;
            }
            if (doResume) {
                disc.resetIntegral();
                disc.setCalActive(false);
                sendJsonMessage("ok", "loop resumed");
            } else if (val >= DAC_MIN && val <= DAC_MAX) {
                disc.setCalActive(true);
                disc.setDACValue((uint16_t)val);
                StaticJsonDocument<192> dj;
                dj["status"] = "ok";
                dj["cmd"] = "dac";
                dj["value"] = val;
                dj["note"] = "loop frozen — send {\"cmd\":\"dac\",\"value\":\"resume\"} to restore";
                serializeJson(dj, Serial);
                Serial.println();
            } else {
                sendJsonMessage("error", "DAC value out of range (use 100-3995, min/max/centre/resume)");
            }
        } else if (strcmp(cmd, "disc_ctrl") == 0) {
            const char* action = doc["action"] | "get";
            if (strcmp(action, "get") == 0) {
                StaticJsonDocument<256> dj;
                dj["status"] = "ok";
                dj["cmd"] = "disc_ctrl";
                dj["action"] = "get";
                dj["avg_window_s"] = g_discAverageSecs;
                dj["p_gain"] = disc.pGain();
                dj["i_gain"] = disc.iGain();
                serializeJson(dj, Serial);
                Serial.println();
            } else if (strcmp(action, "set") == 0) {
                bool haveAny = false;
                bool persist = doc["persist"] | true;
                uint32_t newAvg = g_discAverageSecs;
                if (doc.containsKey("avg_window_s")) {
                    haveAny = true;
                    int avgIn = doc["avg_window_s"].as<int>();
                    if (avgIn < DISC_AVERAGE_SECS_MIN || avgIn > DISC_AVERAGE_SECS_MAX) {
                        sendJsonMessage("error", "avg_window_s out of range");
                        return;
                    }
                    newAvg = (uint32_t)avgIn;
                }

                float newP = disc.pGain();
                float newI = disc.iGain();
                if (doc.containsKey("p_gain")) {
                    haveAny = true;
                    newP = doc["p_gain"].as<float>();
                }
                if (doc.containsKey("i_gain")) {
                    haveAny = true;
                    newI = doc["i_gain"].as<float>();
                }
                if (!haveAny) {
                    sendJsonMessage("error", "disc_ctrl set requires avg_window_s and/or p_gain/i_gain");
                    return;
                }
                if (!disc.setLoopGains(newP, newI)) {
                    sendJsonMessage("error", "p_gain or i_gain out of range");
                    return;
                }

                g_discAverageSecs = newAvg;
                status.setDiscAvgWindowSecs(g_discAverageSecs);

                StaticJsonDocument<256> dj;
                dj["status"] = "ok";
                dj["cmd"] = "disc_ctrl";
                dj["action"] = "set";
                dj["avg_window_s"] = g_discAverageSecs;
                dj["p_gain"] = disc.pGain();
                dj["i_gain"] = disc.iGain();
                dj["persist_requested"] = persist;
                dj["persisted"] = persist ? saveDiscCtrlSettings(false) : false;
                serializeJson(dj, Serial);
                Serial.println();
            } else if (strcmp(action, "save") == 0) {
                bool persisted = saveDiscCtrlSettings(false);
                StaticJsonDocument<256> dj;
                dj["status"] = persisted ? "ok" : "error";
                dj["cmd"] = "disc_ctrl";
                dj["action"] = "save";
                dj["avg_window_s"] = g_discAverageSecs;
                dj["p_gain"] = disc.pGain();
                dj["i_gain"] = disc.iGain();
                dj["persisted"] = persisted;
                serializeJson(dj, Serial);
                Serial.println();
            } else if (strcmp(action, "load") == 0) {
                bool loaded = loadDiscCtrlSettings(false);
                StaticJsonDocument<256> dj;
                dj["status"] = loaded ? "ok" : "warning";
                dj["cmd"] = "disc_ctrl";
                dj["action"] = "load";
                dj["avg_window_s"] = g_discAverageSecs;
                dj["p_gain"] = disc.pGain();
                dj["i_gain"] = disc.iGain();
                dj["loaded"] = loaded;
                serializeJson(dj, Serial);
                Serial.println();
            } else {
                sendJsonMessage("error", "disc_ctrl action must be get|set|save|load");
            }
        } else if (strcmp(cmd, "status_ctrl") == 0) {
            const char* action = doc["action"] | "get";
            if (strcmp(action, "get") == 0) {
                StaticJsonDocument<192> dj;
                dj["status"] = "ok";
                dj["cmd"] = "status_ctrl";
                dj["action"] = "get";
                dj["status_interval_ms"] = status.statusIntervalMs();
                serializeJson(dj, Serial);
                Serial.println();
            } else if (strcmp(action, "set") == 0) {
                int intervalMs = doc["status_interval_ms"] | -1;
                if (intervalMs < 200 || intervalMs > 10000) {
                    sendJsonMessage("error", "status_interval_ms out of range (200..10000)");
                    return;
                }
                status.setStatusIntervalMs((uint32_t)intervalMs);
                StaticJsonDocument<192> dj;
                dj["status"] = "ok";
                dj["cmd"] = "status_ctrl";
                dj["action"] = "set";
                dj["status_interval_ms"] = status.statusIntervalMs();
                serializeJson(dj, Serial);
                Serial.println();
            } else {
                sendJsonMessage("error", "status_ctrl action must be get|set");
            }
        } else if (strcmp(cmd, "adf_persist") == 0) {
            const char* action = doc["action"];
            if (!action) {
                sendJsonMessage("error", "adf_persist requires action");
                return;
            }

            if (strcmp(action, "status") == 0) {
                StaticJsonDocument<192> dj;
                dj["status"] = "ok";
                dj["cmd"] = "adf_persist";
                dj["action"] = "status";
                dj["pending_mask"] = adfPersistDirtyMask();
                dj["adf1_pending"] = adf1PersistDirty;
                dj["adf2_pending"] = adf2PersistDirty;
                serializeJson(dj, Serial);
                Serial.println();
            } else if (strcmp(action, "commit") == 0) {
                bool committed = commitStagedADFRegsToEEPROM();

                StaticJsonDocument<192> done;
                done["status"] = committed ? "ok" : "error";
                done["cmd"] = "adf_persist";
                done["action"] = "commit";
                done["committed"] = committed;
                done["pending_mask"] = adfPersistDirtyMask();
                serializeJson(done, Serial);
                Serial.println();
            } else {
                sendJsonMessage("error", "adf_persist action must be status|commit");
            }
        } else if (strcmp(cmd, "efc_cal") == 0) {
            if (g_efcCalState != EFCCalState::IDLE) {
                sendJsonMessage("error", "efc_cal already running");
            } else {
                uint32_t settleMs = EFC_CAL_SETTLE_MS;
                if (doc.containsKey("settle_s")) {
                    int ss = doc["settle_s"].as<int>();
                    if (ss >= 5 && ss <= 120) settleMs = (uint32_t)ss * 1000;
                }
                g_efcCalSettleMs = settleMs;
                disc.setCalActive(true);
                disc.setDACValue(DAC_MIN);
                g_efcCalTimer = millis();
                g_efcCalState = EFCCalState::SETTLE_LOW;
                StaticJsonDocument<128> dj;
                dj["status"] = "ok";
                dj["cmd"] = "efc_cal";
                dj["msg"] = "started: settling at DAC_MIN";
                dj["dac_min"] = DAC_MIN;
                dj["dac_max"] = DAC_MAX;
                dj["settle_s"] = (int)(settleMs / 1000);
                serializeJson(dj, Serial); Serial.println();
            }
        } else if (strcmp(cmd, "help") == 0) {
            sendJsonMessage("info", "JSON commands: adf1/adf2, dac, adf_persist, disc_ctrl get|set|save|load, status_ctrl get|set, efc_cal");
        } else if (strcmp(cmd, "info") == 0) {
            StaticJsonDocument<384> dj;
            dj["status"] = "ok";
            dj["cmd"] = "info";
            dj["version"] = FW_VERSION;
            dj["board"] = "RP2350";
            dj["have_dac"] = static_cast<int>(haveDAC);
            dj["have_adf1"] = static_cast<int>(haveADF1);
            dj["have_adf2"] = static_cast<int>(haveADF2);
            dj["disc_avg_window_s"] = g_discAverageSecs;
            dj["disc_p_gain"] = disc.pGain();
            dj["disc_i_gain"] = disc.iGain();
            dj["status_interval_ms"] = status.statusIntervalMs();
            bool want_regs = doc["regs"] | false;
            if (want_regs) {
                JsonArray a1 = dj.createNestedArray("adf1_regs");
                for (int i = 0; i < 6; ++i) a1.add(adf1_regs[i]);
                JsonArray a2 = dj.createNestedArray("adf2_regs");
                for (int i = 0; i < 6; ++i) a2.add(adf2_regs[i]);
            }
            serializeJson(dj, Serial);
            Serial.println();
        }
        return;
    }
    // tokens
    int sp1 = s.indexOf(' ');
    String cmd = (sp1 == -1) ? s : s.substring(0, sp1);
    String rest = (sp1 == -1) ? String("") : s.substring(sp1 + 1);

    if (cmd == "adf1") {
        int sp2 = rest.indexOf(' ');
        String sub = (sp2 == -1) ? rest : rest.substring(0, sp2);
        String args = (sp2 == -1) ? String("") : rest.substring(sp2 + 1);
        if (sub == "show") { printADFRegs("ADF1", adf1_regs); }
        else if (sub == "save") { saveADFRegs(adf1_regs, ADF1_EEPROM_ADDR); }
        else if (sub == "load") {
            loadADFRegs(adf1_regs, ADF1_REGS, ADF1_EEPROM_ADDR);
            if (haveADF1 == PeripheralStatus::PS_OK) adf1.program(adf1_regs);
            else sendJsonMessage("info", "adf1_unavailable_skip_program");
        }
        else if (sub == "program") {
            bool programmed = programAndPersistADF(adf1, adf1_regs, ADF1_EEPROM_ADDR, haveADF1);
            sendJsonKV("ok", "programmed", programmed ? 1 : 0);
        }
        else if (sub == "default") { for (int i=0;i<6;i++) adf1_regs[i]=ADF1_REGS[i]; }
        else if (sub == "set") {
            int sp = args.indexOf(' ');
            if (sp == -1) { sendJsonMessage("error", "usage: adf1 set <0-5> <hex>"); }
            else {
                int idx = args.substring(0, sp).toInt();
                String v = args.substring(sp+1);
                uint32_t val = (uint32_t) strtoul(v.c_str(), nullptr, 16);
                if (idx >=0 && idx < 6) {
                    adf1_regs[idx] = val;
                    StaticJsonDocument<192> dj;
                    dj["status"] = "ok";
                    dj["cmd"] = "adf1";
                    dj["index"] = idx;
                    dj["value"] = val;
                    serializeJson(dj, Serial);
                    Serial.println();
                } else sendJsonMessage("error", "index out of range");
            }
        }
    } else if (cmd == "adf2") {
        int sp2 = rest.indexOf(' ');
        String sub = (sp2 == -1) ? rest : rest.substring(0, sp2);
        String args = (sp2 == -1) ? String("") : rest.substring(sp2 + 1);
        if (sub == "show") { printADFRegs("ADF2", adf2_regs); }
        else if (sub == "save") { saveADFRegs(adf2_regs, ADF2_EEPROM_ADDR); }
        else if (sub == "load") {
            loadADFRegs(adf2_regs, ADF2_REGS, ADF2_EEPROM_ADDR);
            if (haveADF2 == PeripheralStatus::PS_OK) adf2.program(adf2_regs);
            else sendJsonMessage("info", "adf2_unavailable_skip_program");
        }
        else if (sub == "program") {
            bool programmed = programAndPersistADF(adf2, adf2_regs, ADF2_EEPROM_ADDR, haveADF2);
            sendJsonKV("ok", "programmed", programmed ? 1 : 0);
        }
        else if (sub == "default") { for (int i=0;i<6;i++) adf2_regs[i]=ADF2_REGS[i]; }
        else if (sub == "set") {
            int sp = args.indexOf(' ');
            if (sp == -1) { sendJsonMessage("error", "usage: adf2 set <0-5> <hex>"); }
            else {
                int idx = args.substring(0, sp).toInt();
                String v = args.substring(sp+1);
                uint32_t val = (uint32_t) strtoul(v.c_str(), nullptr, 16);
                if (idx >=0 && idx < 6) {
                    adf2_regs[idx] = val;
                    StaticJsonDocument<192> dj;
                    dj["status"] = "ok";
                    dj["cmd"] = "adf2";
                    dj["index"] = idx;
                    dj["value"] = val;
                    serializeJson(dj, Serial);
                    Serial.println();
                } else sendJsonMessage("error", "index out of range");
            }
        }
    } else if (cmd == "info") {
        StaticJsonDocument<256> dj;
        dj["status"] = "ok";
        dj["cmd"] = "info";
        dj["version"] = FW_VERSION;
        dj["board"] = "RP2350";
        dj["disc_avg_window_s"] = g_discAverageSecs;
        dj["disc_p_gain"] = disc.pGain();
        dj["disc_i_gain"] = disc.iGain();
        serializeJson(dj, Serial);
        Serial.println();
    } else if (cmd == "help") {
        StaticJsonDocument<256> dj;
        dj["status"] = "info";
        dj["help"] = "adf1 show|save|load|program|default|set <i> <hex>; adf2 show|save|load|program|default|set <i> <hex>; disc_ctrl get|set|save|load; status_ctrl get|set";
        serializeJson(dj, Serial);
        Serial.println();
    } else {
        sendJsonMessage("error", "Unknown command. Type 'help'.");
    }
}

// ============================================================
// Object instantiation
// ============================================================

ADF4351 adf1(ADF1_CLK_PIN, ADF1_MOSI_PIN, ADF1_LE_PIN,
             ADF1_CE_PIN,  ADF1_LD_PIN);

ADF4351 adf2(ADF2_CLK_PIN, ADF2_MOSI_PIN, ADF2_LE_PIN,
             ADF2_CE_PIN,  ADF2_LD_PIN);

MCP4725         dac(MCP4725_ADDR);
GPSParser       gps(Serial1);
Discipliner     disc(dac);
StatusManager   status(disc, gps, adf1, adf2);
PIOTimingEngine timing(GPS_1PPS_PIN, FREQ_COUNT_PIN);

// ============================================================
// Core 1 - PIO timing engine only
// IRQ handlers are registered in timing.begin() and fire
// automatically. Core1 just needs to stay alive.
// ============================================================

void setup1() {
    // Required for safe flash writes from core0 using multicore_lockout_*.
    multicore_lockout_victim_init();

#if DISABLE_CORE1_TIMING_ENGINE
    sendJsonMessage("info", "core1_timing_disabled");
#else
    uint32_t actual = clock_get_hz(clk_sys);
    bool timingOk = timing.begin();
    timing.setSysclkHz(actual);
    timing.setOCXOFreq(10000000);
    // Emit startup diagnostic so serial monitor shows init result.
    {
        StaticJsonDocument<128> dj;
        dj["status"] = timingOk ? "info" : "error";
        dj["event"] = "timing_init";
        dj["ok"] = timingOk;
        serializeJson(dj, Serial);
        Serial.println();
    }
#endif
}

void loop1() {
    // All PIO work is interrupt-driven on this core.
    // tight_loop_contents() hints to the compiler/CPU
    // not to optimise this away.
    tight_loop_contents();
}

// ============================================================
// Core 0
// ============================================================

void setup() {
    // 150MHz = fully validated RP2350 speed, no overclocking needed.
    // At 150MHz, PIO timestamp resolution = 6.67ns.
    // GPS 1PPS accuracy (~20ns RMS) dominates anyway.
    set_sys_clock_khz(150000, true);

    Serial.begin(115200);
    delay(5000);
    // Print the banner multiple times so a late-opening serial monitor is
    // more likely to capture at least one of the messages.

    {
        StaticJsonDocument<192> dj;
        dj["status"] = "info";
        dj["event"] = "firmware_boot";
        dj["version"] = FW_VERSION;
        dj["board"] = "RP2350";
        serializeJson(dj, Serial);
        Serial.println();
    }

#if USE_PWM_DAC
    // PWM DAC mode: no I2C needed.  Set 12-bit resolution so the 0-4095
    // range matches the MCP4725 scale, then drive the pin to centre.
    analogWriteResolution(12);
    analogWrite(PWM_DAC_PIN, DAC_CENTRE);
    haveDAC = PeripheralStatus::PS_OK;
    disc.setDACEnabled(true);
    sendJsonMessage("info", "pwm_dac_mode");
#else
    // I2C for MCP4725 DAC - avoid forcing SDA/SCL pins to prevent accidental
    // alternate-function assignment that can interfere with other peripherals.
    // Use default TwoWire pin assignments provided by the core.
    Wire.begin();

    delay(50);
    // Detect DAC presence via I2C ACK
    Wire.beginTransmission(MCP4725_ADDR);
    if (Wire.endTransmission() != 0) {
        haveDAC = PeripheralStatus::PS_ABSENT;
        sendJsonMessage("warning", "mcp4725_not_detected");
        disc.setDACEnabled(false);
        // Immediate steady alarm for missing critical hardware
        status.setAlarmSteady(true);
    } else {
        haveDAC = PeripheralStatus::PS_OK;
        disc.setDACEnabled(true);
    }
#endif

    // GPS UART
    Serial1.setTX(GPS_TX_PIN);
    Serial1.setRX(GPS_RX_PIN);
    gps.begin(GPS_BAUD);

#if !USE_PWM_DAC
    if (haveDAC == PeripheralStatus::PS_OK) {
        dac.begin();
    }
#endif
    disc.begin();
    status.begin();

    // EEPROM emulation must be initialized before put/get and committed after writes.
    // Reserve a small region sufficient for DAC + ADF blocks.
    EEPROM.begin(1024);
    loadDiscCtrlSettings(true);

#if ADF_PERSIST_EEPROM
    adfFsReady = LittleFS.begin();
    {
        StaticJsonDocument<192> dj;
        dj["status"] = adfFsReady ? "info" : "error";
        dj["event"] = "adf_persist_fs";
        dj["ready"] = adfFsReady;
        serializeJson(dj, Serial);
        Serial.println();
    }
#else
    adfFsReady = false;
#endif

    // Initialise mutable ADF regs from EEPROM or defaults
    loadADFRegs(adf1_regs, ADF1_REGS, ADF1_EEPROM_ADDR);
    loadADFRegs(adf2_regs, ADF2_REGS, ADF2_EEPROM_ADDR);

    // Initialise and program both ADF4351s
    const bool adf1Installed = (ADF1_INSTALLED != 0);
    const bool adf2Installed = (ADF2_INSTALLED != 0);

    if (adf1Installed) {
        adf1.begin();
    } else {
        haveADF1 = PeripheralStatus::PS_ABSENT;
        sendJsonMessage("info", "adf1_not_installed_skip_init");
    }

    if (adf2Installed) {
        adf2.begin();
    } else {
        haveADF2 = PeripheralStatus::PS_ABSENT;
        sendJsonMessage("info", "adf2_not_installed_skip_init");
    }

    if (adf1Installed) {
        adf1.program(adf1_regs);
        adf1_attempts = 1;
        delay(100);
        {
            StaticJsonDocument<192> dj;
            dj["event"] = "adf_lock";
            dj["adf"] = "adf1";
            dj["locked"] = adf1.isLocked();
            serializeJson(dj, Serial);
            Serial.println();
        }
    }

    if (adf2Installed) {
        adf2.program(adf2_regs);
        adf2_attempts = 1;
        delay(100);
        {
            StaticJsonDocument<192> dj;
            dj["event"] = "adf_lock";
            dj["adf"] = "adf2";
            dj["locked"] = adf2.isLocked();
            serializeJson(dj, Serial);
            Serial.println();
        }
    }

    // Allow some time for the ADF VCOs to lock after programming.
    // Wait up to this timeout, polling lock status periodically.
    const uint32_t ADF_LOCK_WAIT_MS = 5000;
    const uint32_t ADF_LOCK_POLL_MS = 200;
    uint32_t startMs = millis();
    bool a1_locked = !adf1Installed;
    bool a2_locked = !adf2Installed;
    while ((millis() - startMs) < ADF_LOCK_WAIT_MS) {
        if (adf1Installed) a1_locked = adf1.isLocked();
        if (adf2Installed) a2_locked = adf2.isLocked();
        if (a1_locked && a2_locked) break;
        delay(ADF_LOCK_POLL_MS);
    }
    {
        StaticJsonDocument<192> dj;
        dj["event"] = "init_adf_locks";
        dj["adf1_locked"] = a1_locked;
        dj["adf2_locked"] = a2_locked;
        serializeJson(dj, Serial);
        Serial.println();
    }

    if (adf1Installed) haveADF1 = a1_locked ? PeripheralStatus::PS_OK : PeripheralStatus::PS_ABSENT;
    if (adf2Installed) haveADF2 = a2_locked ? PeripheralStatus::PS_OK : PeripheralStatus::PS_ABSENT;

    // If any critical subsystem failed to initialize after the grace period,
    // assert the alarm LED so the operator can see an issue.
    bool initFailed = false;
    if (adf1Installed && !a1_locked) {
        sendJsonMessage("error", "adf1_failed_to_lock_during_init");
        initFailed = true;
    }
    if (adf2Installed && !a2_locked) {
        sendJsonMessage("error", "adf2_failed_to_lock_during_init");
        initFailed = true;
    }
    if (initFailed) {
        status.setAlarmSteady(true);
    }
}

void loop() {
    // CLI input first (FIFO buffered, non-blocking) so command handling stays
    // responsive even if other subsystems are degraded.
    pollSerialIntoCLIQueue();
    String nextCmd;
    if (dequeueCLICommand(nextCmd)) {
        handleCLI(nextCmd);
    }

    // Handle staged EEPROM commit outside command handlers.
    serviceADFEEPROMCommit();

    // Parse incoming GPS NMEA sentences
    gps.update();

    // Collect PIO timing result (Core1 populates this via IRQ)
    TimingResult tr = timing.getResult();
    const GPSState& gs = gps.state();

    bool gpsGood = gs.hasFix && gs.ppsValid;

    // Rolling ring buffer of per-second frequency-error samples.
    // The discipliner is called every second with the rolling mean
    // over the most recent effectiveAvgSecs entries.
    static double   freqErrRing[DISC_AVERAGE_SECS_MAX];
    static uint32_t freqErrRingIdx   = 0;
    static uint32_t freqErrRingCount = 0;

    // EFC calibration state machine — suspends normal discipliner while running.
    if (g_efcCalState != EFCCalState::IDLE) {
        if (tr.freqValid) status.setMeasuredOCXO(tr.measuredFreq_Hz, tr.freqError_ppb);
        uint32_t elapsed = millis() - g_efcCalTimer;
        switch (g_efcCalState) {
            case EFCCalState::SETTLE_LOW:
                if (elapsed >= g_efcCalSettleMs) {
                    g_efcCalState = EFCCalState::READ_LOW;
                    g_efcCalTimer = millis();
                }
                break;
            case EFCCalState::READ_LOW:
                if (tr.freqValid) {
                    g_efcCalFreqLow = tr.freqError_ppb;
                    disc.setDACValue(DAC_MAX);
                    g_efcCalTimer = millis();
                    g_efcCalState = EFCCalState::SETTLE_HIGH;
                    StaticJsonDocument<128> dj2;
                    dj2["event"] = "efc_cal";
                    dj2["step"] = "low_read";
                    dj2["dac"] = DAC_MIN;
                    dj2["freq_error_ppb"] = g_efcCalFreqLow;
                    serializeJson(dj2, Serial); Serial.println();
                }
                break;
            case EFCCalState::SETTLE_HIGH:
                if (elapsed >= g_efcCalSettleMs) {
                    g_efcCalState = EFCCalState::READ_HIGH;
                    g_efcCalTimer = millis();
                }
                break;
            case EFCCalState::READ_HIGH:
                if (tr.freqValid) {
                    g_efcCalFreqHigh = tr.freqError_ppb;
                    int32_t swing_ppb = g_efcCalFreqHigh - g_efcCalFreqLow;
                    int32_t dac_span  = DAC_MAX - DAC_MIN;
                    float slope = (dac_span > 0) ? (float)swing_ppb / (float)dac_span : 0.0f;
                    StaticJsonDocument<256> dj3;
                    dj3["event"] = "efc_cal";
                    dj3["step"] = "done";
                    dj3["dac_min"] = DAC_MIN;
                    dj3["dac_max"] = DAC_MAX;
                    dj3["freq_at_min_ppb"] = g_efcCalFreqLow;
                    dj3["freq_at_max_ppb"] = g_efcCalFreqHigh;
                    dj3["swing_ppb"] = swing_ppb;
                    dj3["slope_ppb_per_dac"] = slope;
                    serializeJson(dj3, Serial); Serial.println();
                    // Restore DAC to centre and re-enter discipliner
                    disc.setDACValue(DAC_CENTRE);
                    disc.resetIntegral();
                    disc.setCalActive(false);
                    freqErrRingIdx = 0;
                    freqErrRingCount = 0;
                    g_efcCalState = EFCCalState::IDLE;
                }
                break;
            default: break;
        }
        // Skip normal discipliner loop while calibrating
        goto skip_discipliner;
    }

    if (tr.freqValid) {
        status.setMeasuredOCXO(tr.measuredFreq_Hz, tr.freqError_ppb);
        // Feed DAC snapshot into lock detection ring buffer every second
        disc.feedLockSample();

        // Push this second's sample into the rolling ring buffer
        freqErrRing[freqErrRingIdx] = tr.freqError_ppb;
        freqErrRingIdx = (freqErrRingIdx + 1) % DISC_AVERAGE_SECS_MAX;
        if (freqErrRingCount < DISC_AVERAGE_SECS_MAX) freqErrRingCount++;

        // Acquiring: short window (base/4) + full gain for fast pull-in.
        // Locked:    full window (base)    + gain * LOCKED_RATIO for stability.
        uint32_t effectiveAvgSecs = (disc.state() == DiscState::LOCKED)
                                    ? g_discAverageSecs
                                    : g_discAverageSecs / 4;
        if (effectiveAvgSecs < 1) effectiveAvgSecs = 1;
        status.setDiscAvgWindowSecs(effectiveAvgSecs);

        // Always advance warmup/state-machine (do not gate behind window fill)
        disc.tickWarmup(gpsGood);

        // Compute rolling mean over the most recent effectiveAvgSecs samples
        uint32_t usable = (freqErrRingCount < effectiveAvgSecs)
                          ? freqErrRingCount : effectiveAvgSecs;
        if (usable > 0) {
            double sum = 0.0;
            for (uint32_t i = 0; i < usable; i++) {
                uint32_t idx = (freqErrRingIdx + DISC_AVERAGE_SECS_MAX - 1 - i)
                               % DISC_AVERAGE_SECS_MAX;
                sum += freqErrRing[idx];
            }
            int32_t avgFreqError_ppb = (int32_t)lround(sum / (double)usable);
            disc.update(avgFreqError_ppb, gpsGood, effectiveAvgSecs);
        }
    } else if (!gpsGood) {
        freqErrRingIdx   = 0;
        freqErrRingCount = 0;
        disc.update(0, false);
    }

    skip_discipliner:

    // Log OCXO frequency measurement every 10 seconds
    if (tr.freqValid) {
        static uint32_t lastFreqLog = 0;
        if (millis() - lastFreqLog >= 10000) {
            lastFreqLog = millis();
            StaticJsonDocument<256> dj;
            dj["event"] = "ocxo";
            dj["pulse_count"] = tr.freqPulseCount;
            dj["measured_hz"] = tr.measuredFreq_Hz;
            dj["freq_error_ppb"] = tr.freqError_ppb;
            dj["dac_value"] = disc.dacValue();
            switch (disc.state()) {
                case DiscState::WARMUP:    dj["disc_state"] = "WARMUP";    break;
                case DiscState::ACQUIRING: dj["disc_state"] = "ACQUIRING"; break;
                case DiscState::LOCKED:    dj["disc_state"] = "LOCKED";    break;
                case DiscState::HOLDOVER:  dj["disc_state"] = "HOLDOVER";  break;
                case DiscState::FREERUN:   dj["disc_state"] = "FREERUN";   break;
            }
            serializeJson(dj, Serial);
            Serial.println();
        }
    }

    // LEDs, alarm, periodic debug print
    status.update();

    // heartbeat removed — use presence of JSON status messages instead

    // Reprogram ADF4351 if lock is lost
    static uint32_t lastLockCheck = 0;
    if (millis() - lastLockCheck >= 1000) {
        lastLockCheck = millis();
        if ((ADF1_INSTALLED != 0) && !adf1.isLocked()) {
            if (haveADF1 == PeripheralStatus::PS_OK && adf1_attempts < MAX_ADF_ATTEMPTS) {
                sendJsonMessage("warning", "adf1_lock_lost_reprogramming_attempt");
                adf1.program(adf1_regs);
                adf1_attempts++;
            } else if (haveADF1 == PeripheralStatus::PS_OK && adf1_attempts >= MAX_ADF_ATTEMPTS) {
                sendJsonMessage("warning", "adf1_unresponsive_disabling_reprogram");
                haveADF1 = PeripheralStatus::PS_ABSENT;
            }
        } else {
            // reset attempts on successful lock
            adf1_attempts = 1;
        }
        if ((ADF2_INSTALLED != 0) && !adf2.isLocked()) {
            if (haveADF2 == PeripheralStatus::PS_OK && adf2_attempts < MAX_ADF_ATTEMPTS) {
                sendJsonMessage("warning", "adf2_lock_lost_reprogramming_attempt");
                adf2.program(adf2_regs);
                adf2_attempts++;
            } else if (haveADF2 == PeripheralStatus::PS_OK && adf2_attempts >= MAX_ADF_ATTEMPTS) {
                sendJsonMessage("warning", "adf2_unresponsive_disabling_reprogram");
                haveADF2 = PeripheralStatus::PS_ABSENT;
            }
        } else {
            adf2_attempts = 1;
        }
    }

    delay(10);

}
