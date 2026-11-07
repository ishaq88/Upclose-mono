/*
 * UpClose grove_pod - sensor pod firmware (card H03)
 * Board: Seeed Grove Beginner Kit for Arduino (ATmega328P, program as "Arduino Uno")
 *
 * Streams the line protocol "upclose-pod v1" over Serial at 250000 baud:
 *   #upclose-pod v1             first line (re-sent after every reset)
 *   A,<t_ms>,<x_mg>,<y_mg>,<z_mg>   accelerometer, ~400 Hz (LIS3DHTR, +/-4 g, 12-bit)
 *   S,<t_ms>,<level>                sound level 0..1023, ~100 Hz (peak-to-peak of a short ADC burst)
 *   T,<t_ms>,<celsius>,<rh>         DHT20 temperature/humidity, every 2 s (one decimal)
 *   B,<t_ms>,<0|1>                  button edges (1 = pressed)
 *   K,<t_ms>,<0..1023>              rotary knob, on change >= 8 counts, max 10 Hz
 * t_ms is millis() since reset.
 *
 * Host -> pod commands (H17, one line each, '\n' terminated, <= 23 chars, malformed lines ignored):
 *   !S,L<seconds_left>   baseline running: OLED "LEARNING <n>s"
 *   !S,N | !S,A | !S,X   normal | anomaly suspected (OLED MISFIRE SUSPECTED, LED blinks, chirps) | not assessed
 *   !M,<0|1>             buzzer mute off/on
 * The pod answers each accepted command with "#ack,<cmd body>" (comment line: old hosts ignore it).
 * The state reverts to idle if no state command arrives for 5 s (host gone). A button hold >= 1 s
 * toggles mute locally (and sends "#ack,M,<0|1>"); the B edges are still sent as before.
 *
 * Design rules (ATmega328P: 2 KB RAM, no OS):
 *  - Nothing in loop() waits for a sensor. The DHT20 is a trigger -> wait -> read
 *    state machine (the stock library blocks ~80 ms). All scheduling uses micros().
 *  - The accelerometer runs its own 32-sample FIFO (stream mode). We drain it every
 *    ~5 ms, so the OLED redraw (a few ms) or a serial write can never lose a sample.
 *    Sample timestamps are spread evenly between the previous and the current drain
 *    so the average rate seen by the host equals the real rate.
 *  - LIS3DH is driven by raw I2C register access (no Seeed library: no float math,
 *    no blocking init, ~1 KB flash saved). Registers per ST datasheet DS: LIS3DH.
 *  - No String objects, no printf. Each record is formatted into a 48-byte buffer
 *    and sent with a single Serial.write().
 *  - OLED (U8x8 text mode): max one text row redrawn per loop pass; rows are
 *    refreshed round-robin about twice a second.
 *
 * Libraries: U8g2 (olikraus). Wire is built in. See README.md.
 * NOTE: written for review without a compiler; first compile happens on the board PC.
 */

#include <Arduino.h>
#include <Wire.h>
#include <U8x8lib.h>

// ---------- pins / addresses (Grove Beginner Kit on-board wiring) ----------
static const uint8_t PIN_KNOB   = A0;
static const uint8_t PIN_SOUND  = A2;
static const uint8_t PIN_BUTTON = 6;
static const uint8_t PIN_LED    = 4;
static const uint8_t PIN_BUZZER = 5;  // silent unless the host says anomaly (and not muted)

static const uint8_t ADDR_LIS3DH = 0x19;  // SDO/SA0 high on the kit
static const uint8_t ADDR_DHT20  = 0x38;

// LIS3DH registers
static const uint8_t LIS_WHO_AM_I   = 0x0F;
static const uint8_t LIS_CTRL_REG1  = 0x20;
static const uint8_t LIS_CTRL_REG4  = 0x23;
static const uint8_t LIS_CTRL_REG5  = 0x24;
static const uint8_t LIS_OUT_X_L    = 0x28;
static const uint8_t LIS_FIFO_CTRL  = 0x2E;
static const uint8_t LIS_FIFO_SRC   = 0x2F;
static const uint8_t LIS_AUTO_INC   = 0x80;

// ---------- schedule (microseconds unless noted) ----------
static const uint32_t ACCEL_DRAIN_US = 5000UL;      // FIFO holds 80 ms of samples; drain every 5 ms
static const uint32_t SOUND_US       = 10000UL;     // 100 Hz
static const uint32_t KNOB_US        = 100000UL;    // max 10 Hz
static const uint32_t TEMP_PERIOD_MS = 2000UL;
static const uint32_t DHT_WAIT_MS    = 90UL;        // datasheet: >= 80 ms conversion
static const uint32_t OLED_ROW_US    = 83000UL;     // 8 rows -> full refresh ~1.5 Hz
static const uint8_t  OLED_ROWS      = 8;
static const uint32_t LONG_PRESS_MS  = 1000UL;
static const uint32_t STATE_STALE_MS = 5000UL;      // no host state command for this long -> idle
static const uint32_t CHIRP_PERIOD_MS = 1000UL;
static const uint16_t CHIRP_HZ       = 2400;
static const uint16_t CHIRP_MS       = 60;
static const uint32_t LED_BLINK_MS   = 150UL;
static const uint8_t  CMD_MAX        = 24;
static const uint8_t  KNOB_DELTA     = 8;
static const uint8_t  SOUND_BURST    = 24;
static const uint16_t DEBOUNCE_MS    = 20;

U8X8_SSD1306_128X64_NONAME_HW_I2C u8x8(U8X8_PIN_NONE);  // SSD1315 is SSD1306-compatible

// ---------- state ----------
static bool accelOk = false;
static bool dhtOk = false;
static uint32_t nextAccelUs, nextSoundUs, nextKnobUs, nextOledUs;
static uint32_t prevDrainUs;
static uint32_t lastAccelMs = 0;
static uint16_t accelWindowCount = 0;
static uint16_t accelHz = 0;
static uint32_t accelWindowStartMs = 0;

static bool btnState = false, btnRaw = false;
static uint32_t btnChangeMs = 0;

static int16_t lastKnob = -1000;
static int16_t knobNow = 0;

static int16_t tempX10 = 0, rhX10 = 0;
static bool haveTemp = false;

enum DhtState : uint8_t { DHT_IDLE, DHT_WAIT, DHT_READ };
static DhtState dhtState = DHT_IDLE;
static uint32_t dhtDueMs = 0;
static uint8_t dhtTries = 0;

static uint8_t oledRow = 0;

// host-driven alert state (about 45 bytes of RAM)
static char alertState = 0;          // 0 idle, 'L' learning, 'N' normal, 'A' anomaly, 'X' not assessed
static uint16_t learnLeft = 0;
static bool muted = false;
static uint32_t lastCmdMs = 0, nextChirpMs = 0, btnDownMs = 0;
static bool longDone = false;
static char cmdBuf[CMD_MAX];
static uint8_t cmdLen = 0;
static bool cmdOverflow = false;

// ---------- record formatting (single buffer, no String) ----------
static char ob[48];
static uint8_t on = 0;

static void putC(char c) { ob[on++] = c; }

static void putU(uint32_t v) {
  char t[10];
  uint8_t n = 0;
  do { t[n++] = '0' + (v % 10); v /= 10; } while (v);
  while (n) ob[on++] = t[--n];
}

static void putI(int32_t v) {
  if (v < 0) { ob[on++] = '-'; v = -v; }
  putU((uint32_t)v);
}

static void putFix1(int16_t x10) {  // 215 -> 21.5
  int16_t v = x10;
  if (v < 0) { ob[on++] = '-'; v = -v; }
  putU((uint32_t)(v / 10));
  ob[on++] = '.';
  ob[on++] = '0' + (v % 10);
}

static void sendRecord() {
  ob[on++] = '\n';
  Serial.write((const uint8_t*)ob, on);  // may wait briefly for buffer space; FIFO absorbs it
  on = 0;
}

// ---------- tiny I2C helpers ----------
static bool i2cWriteReg(uint8_t addr, uint8_t reg, uint8_t val) {
  Wire.beginTransmission(addr);
  Wire.write(reg);
  Wire.write(val);
  return Wire.endTransmission() == 0;
}

static bool i2cReadRegs(uint8_t addr, uint8_t reg, uint8_t* buf, uint8_t n) {
  Wire.beginTransmission(addr);
  Wire.write(reg);
  if (Wire.endTransmission(false) != 0) return false;
  if (Wire.requestFrom(addr, n) != n) return false;
  for (uint8_t i = 0; i < n; i++) buf[i] = Wire.read();
  return true;
}

// ---------- accelerometer (LIS3DHTR) ----------
static bool accelBegin() {
  uint8_t who = 0;
  if (!i2cReadRegs(ADDR_LIS3DH, LIS_WHO_AM_I, &who, 1) || who != 0x33) return false;
  bool ok = true;
  ok &= i2cWriteReg(ADDR_LIS3DH, LIS_CTRL_REG1, 0x77);  // 400 Hz, normal mode, XYZ on
  ok &= i2cWriteReg(ADDR_LIS3DH, LIS_CTRL_REG4, 0x98);  // BDU, +/-4 g, high resolution (12 bit)
  ok &= i2cWriteReg(ADDR_LIS3DH, LIS_CTRL_REG5, 0x40);  // FIFO enable
  ok &= i2cWriteReg(ADDR_LIS3DH, LIS_FIFO_CTRL, 0x00);  // bypass first: resets the FIFO
  ok &= i2cWriteReg(ADDR_LIS3DH, LIS_FIFO_CTRL, 0x80);  // stream mode
  return ok;
}

static void accelDrain(uint32_t nowUs, uint32_t nowMs) {
  uint8_t src = 0;
  if (!i2cReadRegs(ADDR_LIS3DH, LIS_FIFO_SRC, &src, 1)) return;
  uint8_t n = src & 0x1F;
  if (src & 0x40) n = 32;  // overrun: FIFO full, 32 unread
  if (n == 0) return;
  uint32_t span = nowUs - prevDrainUs;  // < a few ms normally
  if (span > 100000UL) span = 100000UL;
  for (uint8_t i = 0; i < n; i++) {
    uint8_t b[6];
    if (!i2cReadRegs(ADDR_LIS3DH, LIS_OUT_X_L | LIS_AUTO_INC, b, 6)) break;
    int16_t x = ((int16_t)((b[1] << 8) | b[0]) >> 4) * 2;  // mg (12 bit, 2 mg/digit at +/-4 g)
    int16_t y = ((int16_t)((b[3] << 8) | b[2]) >> 4) * 2;
    int16_t z = ((int16_t)((b[5] << 8) | b[4]) >> 4) * 2;
    uint32_t backUs = (span * (uint32_t)(n - 1 - i)) / n;
    uint32_t t = nowMs - (backUs + 500UL) / 1000UL;
    if (t < lastAccelMs) t = lastAccelMs;  // keep timestamps non-decreasing
    lastAccelMs = t;
    putC('A'); putC(','); putU(t); putC(',');
    putI(x); putC(','); putI(y); putC(','); putI(z);
    sendRecord();
    accelWindowCount++;
  }
  prevDrainUs = nowUs;
}

// ---------- sound ----------
static void soundSample(uint32_t nowMs) {
  uint16_t lo = 1023, hi = 0;
  for (uint8_t i = 0; i < SOUND_BURST; i++) {
    uint16_t v = analogRead(PIN_SOUND);
    if (v < lo) lo = v;
    if (v > hi) hi = v;
  }
  putC('S'); putC(','); putU(nowMs); putC(','); putU(hi - lo);
  sendRecord();
}

// ---------- DHT20 non-blocking state machine ----------
static uint8_t crc8(const uint8_t* d, uint8_t n) {
  uint8_t crc = 0xFF;
  for (uint8_t i = 0; i < n; i++) {
    crc ^= d[i];
    for (uint8_t b = 0; b < 8; b++) crc = (crc & 0x80) ? (uint8_t)((crc << 1) ^ 0x31) : (uint8_t)(crc << 1);
  }
  return crc;
}

static void dhtStep(uint32_t nowMs) {
  if ((int32_t)(nowMs - dhtDueMs) < 0) return;
  switch (dhtState) {
    case DHT_IDLE: {
      Wire.beginTransmission(ADDR_DHT20);
      Wire.write(0xAC); Wire.write(0x33); Wire.write(0x00);
      if (Wire.endTransmission() != 0) {
        dhtOk = false;
        dhtDueMs = nowMs + TEMP_PERIOD_MS;  // retry next period
        return;
      }
      dhtState = DHT_WAIT;
      dhtTries = 0;
      dhtDueMs = nowMs + DHT_WAIT_MS;
      break;
    }
    case DHT_WAIT:
      dhtState = DHT_READ;  // fall through to the read on this same step
    case DHT_READ: {
      uint8_t d[7];
      if (Wire.requestFrom(ADDR_DHT20, (uint8_t)7) != 7) {
        dhtOk = false;
        dhtState = DHT_IDLE;
        dhtDueMs = nowMs + TEMP_PERIOD_MS;
        return;
      }
      for (uint8_t i = 0; i < 7; i++) d[i] = Wire.read();
      if (d[0] & 0x80) {  // still busy: look again in 10 ms (max 5 tries)
        if (++dhtTries > 5) { dhtState = DHT_IDLE; dhtDueMs = nowMs + TEMP_PERIOD_MS; dhtOk = false; }
        else dhtDueMs = nowMs + 10;
        return;
      }
      if (crc8(d, 6) != d[6]) {  // bad frame: skip, no record
        dhtOk = false;
        dhtState = DHT_IDLE;
        dhtDueMs = nowMs + TEMP_PERIOD_MS;
        return;
      }
      uint32_t rawH = ((uint32_t)d[1] << 12) | ((uint32_t)d[2] << 4) | (d[3] >> 4);
      uint32_t rawT = ((uint32_t)(d[3] & 0x0F) << 16) | ((uint32_t)d[4] << 8) | d[5];
      tempX10 = (int16_t)(((rawT * 2000UL) >> 20) - 500);  // raw/2^20*200-50 C, in tenths
      rhX10 = (int16_t)((rawH * 1000UL) >> 20);            // raw/2^20*100 %, in tenths
      haveTemp = true;
      dhtOk = true;
      putC('T'); putC(','); putU(nowMs); putC(','); putFix1(tempX10); putC(','); putFix1(rhX10);
      sendRecord();
      dhtState = DHT_IDLE;
      dhtDueMs = nowMs + TEMP_PERIOD_MS - DHT_WAIT_MS;  // next trigger ~2 s after this one
      break;
    }
  }
}

// ---------- host commands / alert outputs (all non-blocking) ----------
static void ackCmd(char k, const char* a) {
  putC('#'); putC('a'); putC('c'); putC('k'); putC(','); putC(k); putC(',');
  while (*a) putC(*a++);
  sendRecord();
}

static void setMute(bool m) {
  muted = m;
  if (m) noTone(PIN_BUZZER);
}

static void setState(char s, uint32_t nowMs) {
  if (s != alertState) {
    alertState = s;
    noTone(PIN_BUZZER);
    nextChirpMs = nowMs;  // chirp right away on entering anomaly
  }
}

// cmdBuf holds one line without the newline, e.g. "!S,L12". Anything malformed is ignored.
static void handleCmd(uint32_t nowMs) {
  if (cmdLen < 4 || cmdBuf[0] != '!' || cmdBuf[2] != ',') return;
  char k = cmdBuf[1];
  const char* a = cmdBuf + 3;
  if (k == 'S') {
    if (a[0] == 'L') {
      const char* q = a + 1;
      if (!*q) return;
      uint16_t v = 0;
      while (*q >= '0' && *q <= '9' && v < 1000) v = v * 10 + (*q++ - '0');
      if (*q) return;
      learnLeft = v;
      setState('L', nowMs);
    } else if ((a[0] == 'N' || a[0] == 'A' || a[0] == 'X') && a[1] == 0) {
      setState(a[0], nowMs);
    } else {
      return;
    }
    lastCmdMs = nowMs;
  } else if (k == 'M' && (a[0] == '0' || a[0] == '1') && a[1] == 0) {
    setMute(a[0] == '1');
  } else {
    return;
  }
  ackCmd(k, a);
}

static void serialRxStep(uint32_t nowMs) {
  for (uint8_t n = 0; n < 16 && Serial.available() > 0; n++) {  // bounded work per loop pass
    char c = (char)Serial.read();
    if (c == '\r') continue;
    if (c == '\n') {
      if (!cmdOverflow) { cmdBuf[cmdLen] = 0; handleCmd(nowMs); }
      cmdLen = 0;
      cmdOverflow = false;
    } else if (cmdLen < CMD_MAX - 1) {
      cmdBuf[cmdLen++] = c;
    } else {
      cmdOverflow = true;  // too long: drop the line at its newline
    }
  }
  if (alertState != 0 && (nowMs - lastCmdMs) > STATE_STALE_MS) setState(0, nowMs);
}

static void alertOutputsStep(uint32_t nowMs) {
  bool blink = alertState == 'A' && ((nowMs / LED_BLINK_MS) & 1);
  digitalWrite(PIN_LED, (blink || (alertState != 'A' && btnState)) ? HIGH : LOW);
  if (alertState == 'A' && !muted && (int32_t)(nowMs - nextChirpMs) >= 0) {
    tone(PIN_BUZZER, CHIRP_HZ, CHIRP_MS);  // timer-driven, returns immediately
    nextChirpMs = nowMs + CHIRP_PERIOD_MS;
  }
}

// ---------- button / knob ----------
static void buttonStep(uint32_t nowMs) {
  bool raw = digitalRead(PIN_BUTTON) == HIGH;  // Grove button is active high
  if (raw != btnRaw) { btnRaw = raw; btnChangeMs = nowMs; }
  if (btnRaw != btnState && (nowMs - btnChangeMs) >= DEBOUNCE_MS) {
    btnState = btnRaw;  // LED is driven by alertOutputsStep()
    if (btnState) { btnDownMs = nowMs; longDone = false; }
    putC('B'); putC(','); putU(nowMs); putC(','); putC(btnState ? '1' : '0');
    sendRecord();
  }
  if (btnState && !longDone && (nowMs - btnDownMs) >= LONG_PRESS_MS) {  // hold: toggle mute once
    longDone = true;
    setMute(!muted);
    char v[2] = { muted ? '1' : '0', 0 };
    ackCmd('M', v);
  }
}

static void knobStep(uint32_t nowMs) {
  analogRead(PIN_KNOB);  // discard: mux settling after the sound burst
  int16_t v = (int16_t)analogRead(PIN_KNOB);
  knobNow = v;
  int16_t d = v - lastKnob;
  if (d < 0) d = -d;
  if (lastKnob < -500 || d >= KNOB_DELTA) {
    lastKnob = v;
    putC('K'); putC(','); putU(nowMs); putC(','); putU((uint32_t)v);
    sendRecord();
  }
}

// ---------- OLED: one row per call ----------
static char* appendU(char* p, uint32_t v) {
  char t[10];
  uint8_t n = 0;
  do { t[n++] = '0' + (v % 10); v /= 10; } while (v);
  while (n) *p++ = t[--n];
  return p;
}

static void oledDrawRow(uint8_t row) {
  char b[17];
  char* p = b;
  switch (row) {
    case 0: strcpy_P(b, PSTR("UpClose pod v1")); p = b + strlen(b); break;
    case 1: strcpy_P(b, accelOk ? PSTR("streaming") : PSTR("ACCEL FAIL")); p = b + strlen(b); break;
    case 2: {  // knob mapped to the fake-ECU RPM used by the bridge: 600..3000
      strcpy_P(b, PSTR("RPM ")); p = b + 4;
      p = appendU(p, 600UL + ((uint32_t)knobNow * 2400UL) / 1023UL);
      break;
    }
    case 3: strcpy_P(b, btnState ? PSTR("BTN DOWN FAULT") : PSTR("BTN up ok")); p = b + strlen(b); break;
    case 4: {
      if (!haveTemp) { strcpy_P(b, dhtOk ? PSTR("T --") : PSTR("T no sensor")); p = b + strlen(b); break; }
      *p++ = 'T'; *p++ = ' ';
      int16_t t = tempX10;
      if (t < 0) { *p++ = '-'; t = -t; }
      p = appendU(p, t / 10); *p++ = '.'; *p++ = '0' + (t % 10);
      *p++ = 'C'; *p++ = ' ';
      p = appendU(p, rhX10 / 10); *p++ = '%';
      break;
    }
    case 5:
      strcpy_P(b, PSTR("ACC ")); p = b + 4;
      p = appendU(p, accelHz); strcpy_P(p, PSTR(" Hz")); p += 3;
      break;
    case 6:  // host-driven alert banner, line 1
      switch (alertState) {
        case 'L': strcpy_P(b, PSTR("LEARNING ")); p = b + 9; p = appendU(p, learnLeft); *p++ = 's'; break;
        case 'A': strcpy_P(b, PSTR("MISFIRE")); p = b + 7; break;
        case 'X': strcpy_P(b, PSTR("NOT ASSESSED")); p = b + 12; break;
        case 'N': strcpy_P(b, PSTR("NORMAL")); p = b + 6; break;
        default: break;  // idle: blank
      }
      break;
    default:  // row 7: banner line 2 and mute flag
      if (alertState == 'A') { strcpy_P(b, PSTR("SUSPECTED")); p = b + 9; }
      if (muted) { strcpy_P(p, PSTR(" mute")); p += 5; }
      break;
  }
  while (p < b + 16) *p++ = ' ';  // pad: overwrite the old text
  *p = 0;
  u8x8.drawString(0, row, b);
}

static void oledStep(uint32_t nowUs) {
  if ((int32_t)(nowUs - nextOledUs) < 0) return;
  nextOledUs += OLED_ROW_US;
  if ((int32_t)(nowUs - nextOledUs) > 0) nextOledUs = nowUs + OLED_ROW_US;
  oledDrawRow(oledRow);
  oledRow = (oledRow + 1) % OLED_ROWS;
}

// ---------- scheduling helper ----------
static inline bool due(uint32_t nowUs, uint32_t& next, uint32_t period) {
  if ((int32_t)(nowUs - next) < 0) return false;
  next += period;
  if ((int32_t)(nowUs - next) > 0) next = nowUs + period;  // fell far behind: resync, don't burst
  return true;
}

void setup() {
  Serial.begin(250000);
  pinMode(PIN_BUTTON, INPUT);
  pinMode(PIN_LED, OUTPUT);
  digitalWrite(PIN_LED, LOW);
  pinMode(PIN_BUZZER, OUTPUT);  // silent until the host reports an anomaly
  digitalWrite(PIN_BUZZER, LOW);
  ADCSRA = (ADCSRA & 0xF8) | 0x04;  // ADC clock /16 (~13 us/conversion) for the sound burst

  Wire.begin();
  Wire.setClock(400000);
  u8x8.setBusClock(400000);
  u8x8.begin();
  u8x8.setFlipMode(1);  // the kit's OLED is mounted upside down
  u8x8.setFont(u8x8_font_chroma48medium8_r);
  u8x8.clear();

  delay(100);  // DHT20 power-up (setup only; loop never blocks)
  accelOk = accelBegin();

  Serial.println(F("#upclose-pod v1"));
  if (!accelOk) Serial.println(F("#err accel not found at 0x19"));

  uint32_t nowUs = micros();
  uint32_t nowMs = millis();
  prevDrainUs = nowUs;
  nextAccelUs = nowUs + ACCEL_DRAIN_US;
  nextSoundUs = nowUs + SOUND_US;
  nextKnobUs = nowUs;
  nextOledUs = nowUs + OLED_ROW_US;
  dhtDueMs = nowMs + 500;
  accelWindowStartMs = nowMs;
  lastAccelMs = nowMs;
  btnRaw = btnState = (digitalRead(PIN_BUTTON) == HIGH);
}

void loop() {
  uint32_t nowUs = micros();
  uint32_t nowMs = millis();

  if (accelOk && due(nowUs, nextAccelUs, ACCEL_DRAIN_US)) accelDrain(nowUs, nowMs);
  if (due(nowUs, nextSoundUs, SOUND_US)) soundSample(nowMs);
  buttonStep(nowMs);
  serialRxStep(nowMs);
  alertOutputsStep(nowMs);
  if (due(nowUs, nextKnobUs, KNOB_US)) knobStep(nowMs);
  dhtStep(nowMs);

  if (nowMs - accelWindowStartMs >= 1000UL) {  // measured accel rate for the OLED
    accelHz = accelWindowCount;
    accelWindowCount = 0;
    accelWindowStartMs = nowMs;
  }
  oledStep(nowUs);
}
