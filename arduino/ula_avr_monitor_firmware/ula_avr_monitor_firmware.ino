/*
  ula_avr_monitor_firmware.ino
  ULA 4 bits + monitor de registradores AVR — ATmega328P (Arduino Uno)

  Pinagem:
    D2  (PD2) — LED B0   : bit 0 do resultado / preview do campo em foco
    D3  (PD3) — LED B1   : bit 1
    D4  (PD4) — LED B2   : bit 2
    D5  (PD5) — LED B3   : bit 3
    D6  (PD6) — LED Carry
    D7  (PD7) — Chave B0 : INPUT_PULLUP, LOW = ativo
    D8  (PB0) — Chave B1 : INPUT_PULLUP, LOW = ativo
    D9  (PB1) — Chave B2 : INPUT_PULLUP, LOW = ativo
    D10 (PB2) — Chave B3 : INPUT_PULLUP, LOW = ativo
    D11 (PB3) — Botão    : INPUT + pull-down EXTERNO (HIGH = pressionado)
    A0        — Potenciômetro (lido e enviado no JSON; não seleciona operação)

  Estados (ula_estado):
    0 = EDITING  — campos podem ser editados pela API ou pelo hardware
    4 = RESULT   — resultado calculado; botão reinicia (reset)

  Focus (ula_focus):
    0 = OP  1 = X  2 = Y
    Indica qual campo as chaves físicas estão editando agora.
    O campo em foco é atualizado continuamente a partir das chaves a cada
    iteração do loop() e exibido nos LEDs como preview.

  Fluxo físico (sem API) — 3 pressionamentos:
    1. Ajusta chaves para OP, pressiona botão → has_op=true, focus→X
    2. Ajusta chaves para X,  pressiona botão → has_x=true,  focus→Y
    3. Ajusta chaves para Y,  pressiona botão → has_y=true → calcula → RESULT
    4. Pressiona botão → reset, volta ao início

  Fluxo híbrido (API + hardware):
    - API pode setar qualquer campo individual via "set_field".
    - API pode mudar o focus via "focus".
    - API pode disparar o cálculo via "compute_current".
    - Hardware confirma o campo em foco ao pressionar o botão.
    - Quando has_op && has_x && has_y → calcula automaticamente.

  Protocolo serial — três categorias de linha (JSON Lines a 115200 baud):

    1) SNAPSHOT (Arduino → Python, a cada 500 ms):
       {"type":"snapshot", ...registradores... , "ula":{...}}

    2) COMANDO (Python → Arduino, sob demanda):
       {"cmd":"set_field","field":"op","value":"ADD"}   // setar um campo
       {"cmd":"set_field","field":"x","value":1}
       {"cmd":"set_field","field":"y","value":2}
       {"cmd":"focus","field":"y"}                      // mudar focus
       {"cmd":"compute_current"}                        // calcular
       {"cmd":"reset"}                                  // reset
       {"cmd":"ula","op":4,"x":1,"y":2}                // compat: seta tudo + calcula

    3) ACK (Arduino → Python, resposta ao COMANDO):
       {"type":"ack","cmd":"set_field","ok":true,"field":"x","value":1}
       {"type":"ack","cmd":"focus","ok":true,"field":"y"}
       {"type":"ack","cmd":"compute_current","ok":true,"result":3,"carry":0,...}
       {"type":"ack","cmd":"compute_current","ok":false,"error":"missing_fields","missing":["x"]}
       {"type":"ack","cmd":"reset","ok":true}
       {"type":"ack","cmd":"ula","ok":true,...}   // compat
*/

#include <avr/pgmspace.h>
#include <EEPROM.h>
#include <string.h>
#include <stdlib.h>

// ── Pinos ────────────────────────────────────────────────────────────────────
#define LED_B0     2
#define LED_B1     3
#define LED_B2     4
#define LED_B3     5
#define LED_CARRY  6
#define SW_B0      7
#define SW_B1      8
#define SW_B2      9
#define SW_B3     10
#define BTN       11
#define POT_PIN   A0

// ── Constantes ───────────────────────────────────────────────────────────────
#define BLOCK_SIZE    64u
#define SRAM_SIZE   2048u
#define EEPROM_SIZE 1024u
#define FLASH_SIZE 32768u
#define SRAM_BASE   0x0100u
#define SNAPSHOT_MS   500u
#define DEBOUNCE_MS    50u

// focus_field
#define FOCUS_OP 0
#define FOCUS_X  1
#define FOCUS_Y  2

// estado
#define STATE_EDITING 0
#define STATE_RESULT  4

// last_input_source
#define SRC_HARDWARE 0
#define SRC_API      1

// ── Variáveis da ULA ──────────────────────────────────────────────────────────
volatile uint8_t ula_x       = 0;
volatile uint8_t ula_y       = 0;
volatile uint8_t ula_result  = 0;
volatile uint8_t ula_carry   = 0;
volatile uint8_t ula_op      = 0;
volatile uint8_t ula_estado  = STATE_EDITING;

// Estado compartilhado API ↔ hardware
volatile uint8_t  ula_has_op  = 0;
volatile uint8_t  ula_has_x   = 0;
volatile uint8_t  ula_has_y   = 0;
volatile uint8_t  ula_focus   = FOCUS_OP;   // campo atual das chaves físicas
volatile uint8_t  ula_source  = SRC_HARDWARE;
volatile uint16_t ula_version = 0;

// ── Debounce do botão ─────────────────────────────────────────────────────────
static uint8_t       btn_raw_prev    = LOW;
static uint8_t       btn_debounced   = LOW;
static unsigned long btn_debounce_ms = 0;
static bool          btn_event       = false;

// ── Offsets de dump de memória (rotativos) ────────────────────────────────────
static uint16_t sram_offset   = 0;
static uint16_t eeprom_offset = 0;
static uint16_t flash_offset  = 0;

// ── ADC pré-lido ─────────────────────────────────────────────────────────────
static uint16_t adc_ch[6];

static unsigned long last_snapshot = 0;

// ── Buffer de comandos recebidos pela serial ──────────────────────────────────
#define CMD_BUF_SIZE 96
static char    cmd_buf[CMD_BUF_SIZE];
static uint8_t cmd_len = 0;

// ── Helpers ──────────────────────────────────────────────────────────────────

void printHex4(uint16_t v) {
    if (v < 0x1000) Serial.print('0');
    if (v < 0x0100) Serial.print('0');
    if (v < 0x0010) Serial.print('0');
    Serial.print(v, HEX);
}

uint8_t read_switches() {
    uint8_t val = 0;
    if (digitalRead(SW_B0) == LOW) val |= 0x01;
    if (digitalRead(SW_B1) == LOW) val |= 0x02;
    if (digitalRead(SW_B2) == LOW) val |= 0x04;
    if (digitalRead(SW_B3) == LOW) val |= 0x08;
    return val;
}

void handle_button() {
    unsigned long now = millis();
    uint8_t cur = digitalRead(BTN);
    if (cur != btn_raw_prev) btn_debounce_ms = now;
    btn_raw_prev = cur;
    if ((now - btn_debounce_ms) >= DEBOUNCE_MS) {
        if (cur == HIGH && btn_debounced == LOW) btn_event = true;
        btn_debounced = cur;
    }
}

void update_leds(uint8_t val4, uint8_t carry) {
    digitalWrite(LED_B0,    (val4 >> 0) & 1);
    digitalWrite(LED_B1,    (val4 >> 1) & 1);
    digitalWrite(LED_B2,    (val4 >> 2) & 1);
    digitalWrite(LED_B3,    (val4 >> 3) & 1);
    digitalWrite(LED_CARRY, carry & 1);
}

void compute_ula() {
    uint8_t x = ula_x & 0x0F;
    uint8_t y = ula_y & 0x0F;
    uint16_t tmp;
    switch (ula_op) {
        case 0: ula_result = (x & y) & 0x0F; ula_carry = 0; break;
        case 1: ula_result = (x | y) & 0x0F; ula_carry = 0; break;
        case 2: ula_result = (~y)    & 0x0F; ula_carry = 0; break;
        case 3: ula_result = (x ^ y) & 0x0F; ula_carry = 0; break;
        case 4:
            tmp = (uint16_t)x + y;
            ula_result = tmp & 0x0F; ula_carry = (tmp > 15) ? 1 : 0; break;
        case 5:
            ula_result = (x >= y) ? (x - y) & 0x0F : 0;
            ula_carry = 0; break;
        case 6:
            tmp = (uint16_t)x * y;
            ula_result = tmp & 0x0F; ula_carry = (tmp > 15) ? 1 : 0; break;
        case 7:
            ula_result = (y != 0) ? (x / y) & 0x0F : 0;
            ula_carry = 0; break;
        default:
            ula_result = 0; ula_carry = 0; break;
    }
}

void printOpName(uint8_t op) {
    switch (op) {
        case 0: Serial.print(F("AND")); break;
        case 1: Serial.print(F("OR"));  break;
        case 2: Serial.print(F("NOT")); break;
        case 3: Serial.print(F("XOR")); break;
        case 4: Serial.print(F("ADD")); break;
        case 5: Serial.print(F("SUB")); break;
        case 6: Serial.print(F("MUL")); break;
        case 7: Serial.print(F("DIV")); break;
        default: Serial.print(F("???")); break;
    }
}

// ── Avança focus para o próximo campo sem has_* ───────────────────────────────
// Tenta (focus+1)%3, (focus+2)%3; se todos tiverem has_*=true, retorna 255
// para sinalizar "pode calcular".
uint8_t next_unset_focus() {
    for (uint8_t i = 1; i <= 2; i++) {
        uint8_t candidate = (ula_focus + i) % 3;
        if (candidate == FOCUS_OP && !ula_has_op) return candidate;
        if (candidate == FOCUS_X  && !ula_has_x)  return candidate;
        if (candidate == FOCUS_Y  && !ula_has_y)  return candidate;
    }
    return 255;   // todos setados
}

// Confirma campo em foco e avança; se todos prontos, calcula.
// Retorna true se calculou.
bool confirm_focus_and_advance() {
    switch (ula_focus) {
        case FOCUS_OP: ula_has_op = 1; break;
        case FOCUS_X:  ula_has_x  = 1; break;
        case FOCUS_Y:  ula_has_y  = 1; break;
    }
    ula_version++;
    ula_source = SRC_HARDWARE;

    if (ula_has_op && ula_has_x && ula_has_y) {
        compute_ula();
        ula_estado = STATE_RESULT;
        update_leds(ula_result, ula_carry);
        return true;
    }
    uint8_t nxt = next_unset_focus();
    if (nxt != 255) ula_focus = nxt;
    return false;
}

void do_reset() {
    ula_x = 0; ula_y = 0; ula_op = 0;
    ula_result = 0; ula_carry = 0;
    ula_has_op = 0; ula_has_x = 0; ula_has_y = 0;
    ula_focus  = FOCUS_OP;
    ula_estado = STATE_EDITING;
    ula_source = SRC_HARDWARE;
    ula_version++;
    update_leds(0, 0);
}

// ── Parsing de JSON (manual, sem ArduinoJson) ─────────────────────────────────

bool extractJsonValue(const char* buf, const char* key, char* out, uint8_t outSize) {
    char pattern[20];
    snprintf(pattern, sizeof(pattern), "\"%s\"", key);
    const char* p = strstr(buf, pattern);
    if (!p) return false;
    p += strlen(pattern);
    while (*p == ' ') p++;
    if (*p != ':') return false;
    p++;
    while (*p == ' ') p++;
    bool quoted = (*p == '"');
    if (quoted) p++;
    uint8_t i = 0;
    while (*p && *p != ',' && *p != '}' && !(quoted && *p == '"') && i < outSize - 1)
        out[i++] = *p++;
    out[i] = '\0';
    return i > 0;
}

bool strEqualsIgnoreCase(const char* a, const char* b) {
    while (*a && *b) {
        char ca = (*a >= 'a' && *a <= 'z') ? *a - 32 : *a;
        char cb = (*b >= 'a' && *b <= 'z') ? *b - 32 : *b;
        if (ca != cb) return false;
        a++; b++;
    }
    return *a == '\0' && *b == '\0';
}

int8_t parseOp(const char* s) {
    if (s[0] >= '0' && s[0] <= '9') {
        int v = atoi(s);
        return (v >= 0 && v <= 7) ? (int8_t)v : -1;
    }
    const char* names[] = {"AND","OR","NOT","XOR","ADD","SUB","MUL","DIV"};
    for (int8_t i = 0; i < 8; i++)
        if (strEqualsIgnoreCase(s, names[i])) return i;
    return -1;
}

// ── ACKs ─────────────────────────────────────────────────────────────────────

void send_ack_error(const char* cmd, const __FlashStringHelper* err) {
    Serial.print(F("{\"type\":\"ack\",\"cmd\":\""));
    Serial.print(cmd);
    Serial.print(F("\",\"ok\":false,\"error\":\""));
    Serial.print(err);
    Serial.println(F("\"}"));
}

void send_ack_set_field(const char* field, int value) {
    Serial.print(F("{\"type\":\"ack\",\"cmd\":\"set_field\",\"ok\":true,\"field\":\""));
    Serial.print(field);
    Serial.print(F("\",\"value\":"));
    Serial.print(value);
    Serial.println(F("}"));
}

void send_ack_focus(const char* field) {
    Serial.print(F("{\"type\":\"ack\",\"cmd\":\"focus\",\"ok\":true,\"field\":\""));
    Serial.print(field);
    Serial.println(F("\"}"));
}

void send_ack_reset() {
    Serial.println(F("{\"type\":\"ack\",\"cmd\":\"reset\",\"ok\":true}"));
}

void send_ack_compute(bool ok, const __FlashStringHelper* err_p,
                      const __FlashStringHelper* missing_p) {
    if (ok) {
        Serial.print(F("{\"type\":\"ack\",\"cmd\":\"compute_current\",\"ok\":true"));
        Serial.print(F(",\"op\":"));      Serial.print(ula_op);
        Serial.print(F(",\"op_name\":\"")); printOpName(ula_op); Serial.print('"');
        Serial.print(F(",\"x\":"));       Serial.print(ula_x);
        Serial.print(F(",\"y\":"));       Serial.print(ula_y);
        Serial.print(F(",\"result\":"));  Serial.print(ula_result);
        Serial.print(F(",\"carry\":"));   Serial.print(ula_carry);
        Serial.println(F("}"));
    } else {
        Serial.print(F("{\"type\":\"ack\",\"cmd\":\"compute_current\",\"ok\":false,\"error\":\""));
        Serial.print(err_p);
        if (missing_p) {
            Serial.print(F("\",\"missing\":["));
            Serial.print(missing_p);
            Serial.print(F("]"));
        }
        Serial.println(F("}"));
    }
}

// ACK legado do comando "ula" (compat)
void send_ack_ula_ok() {
    Serial.print(F("{\"type\":\"ack\",\"cmd\":\"ula\",\"ok\":true"));
    Serial.print(F(",\"op\":"));        Serial.print(ula_op);
    Serial.print(F(",\"op_name\":\"")); printOpName(ula_op); Serial.print('"');
    Serial.print(F(",\"x\":"));         Serial.print(ula_x);
    Serial.print(F(",\"y\":"));         Serial.print(ula_y);
    Serial.print(F(",\"result\":"));    Serial.print(ula_result);
    Serial.print(F(",\"carry\":"));     Serial.print(ula_carry);
    Serial.println('}');
}

// ── Avanço de focus após set_field via API ────────────────────────────────────
// Se a API setou o campo que estava em foco, avança focus para o próximo unset.
void maybe_advance_focus_after_api_set(uint8_t set_field_idx) {
    if (ula_focus != set_field_idx) return;
    uint8_t nxt = next_unset_focus();
    if (nxt != 255) ula_focus = nxt;
    // se 255 (todos setados), mantém focus onde está — compute_current vai calcular
}

// ── Manipulação de comandos seriais ──────────────────────────────────────────

void handle_cmd_set_field(const char* line) {
    char fieldVal[4], valueStr[8];
    if (!extractJsonValue(line, "field", fieldVal, sizeof(fieldVal))) {
        send_ack_error("set_field", F("missing_field")); return;
    }
    if (!extractJsonValue(line, "value", valueStr, sizeof(valueStr))) {
        send_ack_error("set_field", F("missing_value")); return;
    }

    if (strEqualsIgnoreCase(fieldVal, "op")) {
        int8_t op = parseOp(valueStr);
        if (op < 0) { send_ack_error("set_field", F("invalid_op")); return; }
        ula_op     = (uint8_t)op;
        ula_has_op = 1;
        ula_source = SRC_API;
        ula_version++;
        maybe_advance_focus_after_api_set(FOCUS_OP);
        send_ack_set_field("op", ula_op);

    } else if (strEqualsIgnoreCase(fieldVal, "x")) {
        long v = atol(valueStr);
        if (v < 0 || v > 15) { send_ack_error("set_field", F("invalid_xy")); return; }
        ula_x     = (uint8_t)v;
        ula_has_x = 1;
        ula_source = SRC_API;
        ula_version++;
        maybe_advance_focus_after_api_set(FOCUS_X);
        send_ack_set_field("x", ula_x);

    } else if (strEqualsIgnoreCase(fieldVal, "y")) {
        long v = atol(valueStr);
        if (v < 0 || v > 15) { send_ack_error("set_field", F("invalid_xy")); return; }
        ula_y     = (uint8_t)v;
        ula_has_y = 1;
        ula_source = SRC_API;
        ula_version++;
        maybe_advance_focus_after_api_set(FOCUS_Y);
        send_ack_set_field("y", ula_y);

    } else {
        send_ack_error("set_field", F("unknown_field"));
    }
}

void handle_cmd_focus(const char* line) {
    char fieldVal[4];
    if (!extractJsonValue(line, "field", fieldVal, sizeof(fieldVal))) {
        send_ack_error("focus", F("missing_field")); return;
    }
    if (strEqualsIgnoreCase(fieldVal, "op")) {
        ula_focus = FOCUS_OP; send_ack_focus("op");
    } else if (strEqualsIgnoreCase(fieldVal, "x")) {
        ula_focus = FOCUS_X; send_ack_focus("x");
    } else if (strEqualsIgnoreCase(fieldVal, "y")) {
        ula_focus = FOCUS_Y; send_ack_focus("y");
    } else {
        send_ack_error("focus", F("unknown_field"));
    }
}

void handle_cmd_compute_current() {
    // "effective has": o campo em foco tem valor vivo das chaves, conta como setado
    uint8_t eff_op = ula_has_op || (ula_focus == FOCUS_OP && ula_estado == STATE_EDITING);
    uint8_t eff_x  = ula_has_x  || (ula_focus == FOCUS_X  && ula_estado == STATE_EDITING);
    uint8_t eff_y  = ula_has_y  || (ula_focus == FOCUS_Y  && ula_estado == STATE_EDITING);

    if (eff_op && eff_x && eff_y) {
        // Garante que todos os has_* ficam true (bloqueia reentrada das chaves)
        ula_has_op = 1; ula_has_x = 1; ula_has_y = 1;
        ula_source = SRC_API;
        ula_version++;
        compute_ula();
        ula_estado = STATE_RESULT;
        update_leds(ula_result, ula_carry);
        send_ack_compute(true, nullptr, nullptr);
    } else {
        // Monta lista dos campos faltantes
        // Usa buffer estático — evita alocação dinâmica no AVR
        char missing[20];
        missing[0] = '\0';
        bool first = true;
        if (!eff_op) { strcat(missing, "\"op\""); first = false; }
        if (!eff_x)  { if (!first) strcat(missing, ","); strcat(missing, "\"x\""); first = false; }
        if (!eff_y)  { if (!first) strcat(missing, ","); strcat(missing, "\"y\""); }

        // Imprime ACK de erro diretamente (send_ack_compute não suporta string dinâmica)
        Serial.print(F("{\"type\":\"ack\",\"cmd\":\"compute_current\",\"ok\":false"));
        Serial.print(F(",\"error\":\"missing_fields\",\"missing\":["));
        Serial.print(missing);
        Serial.println(F("]}"));
    }
}

void handle_cmd_reset() {
    do_reset();
    send_ack_reset();
}

// Comando legado {"cmd":"ula","op":...,"x":...,"y":...} — compat total
void handle_cmd_ula_compat(const char* line) {
    char opVal[8], xVal[8], yVal[8];
    if (!extractJsonValue(line, "op", opVal, sizeof(opVal))) {
        send_ack_error("ula", F("missing_op")); return;
    }
    int8_t op = parseOp(opVal);
    if (op < 0) { send_ack_error("ula", F("invalid_op")); return; }
    if (!extractJsonValue(line, "x", xVal, sizeof(xVal)) ||
        !extractJsonValue(line, "y", yVal, sizeof(yVal))) {
        send_ack_error("ula", F("missing_xy")); return;
    }
    long x = atol(xVal), y = atol(yVal);
    if (x < 0 || x > 15 || y < 0 || y > 15) {
        send_ack_error("ula", F("invalid_xy")); return;
    }
    ula_op = (uint8_t)op; ula_x = (uint8_t)x; ula_y = (uint8_t)y;
    ula_has_op = 1; ula_has_x = 1; ula_has_y = 1;
    ula_source = SRC_API;
    ula_version++;
    compute_ula();
    ula_estado = STATE_RESULT;
    update_leds(ula_result, ula_carry);
    send_ack_ula_ok();
}

void handle_command(const char* line) {
    char cmdVal[20];
    if (!extractJsonValue(line, "cmd", cmdVal, sizeof(cmdVal))) {
        send_ack_error("?", F("missing_cmd")); return;
    }
    if      (strEqualsIgnoreCase(cmdVal, "set_field"))      handle_cmd_set_field(line);
    else if (strEqualsIgnoreCase(cmdVal, "focus"))          handle_cmd_focus(line);
    else if (strEqualsIgnoreCase(cmdVal, "compute_current"))handle_cmd_compute_current();
    else if (strEqualsIgnoreCase(cmdVal, "reset"))          handle_cmd_reset();
    else if (strEqualsIgnoreCase(cmdVal, "ula"))            handle_cmd_ula_compat(line);
    else    send_ack_error(cmdVal, F("unknown_cmd"));
}

void poll_serial_commands() {
    while (Serial.available() > 0) {
        char c = (char)Serial.read();
        if (c == '\n') {
            cmd_buf[cmd_len] = '\0';
            if (cmd_len > 0) handle_command(cmd_buf);
            cmd_len = 0;
        } else if (c != '\r') {
            if (cmd_len < CMD_BUF_SIZE - 1) cmd_buf[cmd_len++] = c;
        }
    }
}

// ── Emissão de snapshot JSON ──────────────────────────────────────────────────

void sendEepromBlock() {
    Serial.print(F("\"eeprom\":{\"start\":\"0x"));
    printHex4(eeprom_offset);
    Serial.print(F("\",\"bytes\":["));
    for (uint16_t i = 0; i < BLOCK_SIZE; i++) {
        Serial.print(EEPROM.read(eeprom_offset + i));
        if (i < BLOCK_SIZE - 1) Serial.print(',');
    }
    Serial.print(F("]}"));
}

void sendFlashBlock() {
    Serial.print(F("\"flash\":{\"start\":\"0x"));
    printHex4(flash_offset);
    Serial.print(F("\",\"bytes\":["));
    for (uint16_t i = 0; i < BLOCK_SIZE; i++) {
        Serial.print(pgm_read_byte_near(flash_offset + i));
        if (i < BLOCK_SIZE - 1) Serial.print(',');
    }
    Serial.print(F("]}"));
}

void send_snapshot() {
    uint8_t sreg = SREG;

    Serial.print(F("{\"type\":\"snapshot\",\"timestamp_ms\":"));
    Serial.print(millis());

    Serial.print(F(",\"ports\":{\"PORTB\":")); Serial.print(PORTB);
    Serial.print(F(",\"PORTC\":"));            Serial.print(PORTC);
    Serial.print(F(",\"PORTD\":"));            Serial.print(PORTD);
    Serial.print(F("}"));

    Serial.print(F(",\"pins\":{\"PINB\":"));   Serial.print(PINB);
    Serial.print(F(",\"PINC\":"));             Serial.print(PINC);
    Serial.print(F(",\"PIND\":"));             Serial.print(PIND);
    Serial.print(F("}"));

    Serial.print(F(",\"ddr\":{\"DDRB\":"));    Serial.print(DDRB);
    Serial.print(F(",\"DDRC\":"));             Serial.print(DDRC);
    Serial.print(F(",\"DDRD\":"));             Serial.print(DDRD);
    Serial.print(F("}"));

    Serial.print(F(",\"timers\":{\"TCNT0\":")); Serial.print(TCNT0);
    Serial.print(F(",\"TCNT1\":"));             Serial.print(TCNT1);
    Serial.print(F(",\"TCNT2\":"));             Serial.print(TCNT2);
    Serial.print(F("}"));

    Serial.print(F(",\"adc\":{"));
    for (uint8_t ch = 0; ch < 6; ch++) {
        Serial.print(F("\"A")); Serial.print(ch); Serial.print(F("\":"));
        Serial.print(adc_ch[ch]);
        if (ch < 5) Serial.print(',');
    }
    Serial.print(F("}"));

    Serial.print(F(",\"flags\":{\"SREG\":")); Serial.print(sreg);
    Serial.print(F(",\"I\":"));  Serial.print((sreg >> 7) & 1);
    Serial.print(F(",\"T\":"));  Serial.print((sreg >> 6) & 1);
    Serial.print(F(",\"H\":"));  Serial.print((sreg >> 5) & 1);
    Serial.print(F(",\"S\":"));  Serial.print((sreg >> 4) & 1);
    Serial.print(F(",\"V\":"));  Serial.print((sreg >> 3) & 1);
    Serial.print(F(",\"N\":"));  Serial.print((sreg >> 2) & 1);
    Serial.print(F(",\"Z\":"));  Serial.print((sreg >> 1) & 1);
    Serial.print(F(",\"C\":"));  Serial.print((sreg >> 0) & 1);
    Serial.print(F("}"));

    uint16_t sram_phys = SRAM_BASE + sram_offset;
    uint8_t* sram_ptr  = (uint8_t*)sram_phys;
    Serial.print(F(",\"memory\":{\"sram\":{\"start\":\"0x"));
    printHex4(sram_phys);
    Serial.print(F("\",\"bytes\":["));
    for (uint16_t i = 0; i < BLOCK_SIZE; i++) {
        Serial.print(sram_ptr[i]);
        if (i < BLOCK_SIZE - 1) Serial.print(',');
    }
    Serial.print(F("]},"));
    sendEepromBlock();
    Serial.print(',');
    sendFlashBlock();
    Serial.print(F("}"));

    // ── Seção ULA ─────────────────────────────────────────────────────────────
    Serial.print(F(",\"ula\":{"));

    // estado
    Serial.print(F("\"estado\":"));
    Serial.print(ula_estado);
    Serial.print(F(",\"estado_name\":\""));
    Serial.print(ula_estado == STATE_RESULT ? F("RESULT") : F("EDITING"));
    Serial.print('"');

    // operação
    Serial.print(F(",\"op\":")); Serial.print(ula_op);
    Serial.print(F(",\"op_name\":\"")); printOpName(ula_op); Serial.print('"');
    Serial.print(F(",\"op_code\":\""));
    Serial.print((ula_op >> 2) & 1);
    Serial.print((ula_op >> 1) & 1);
    Serial.print((ula_op >> 0) & 1);
    Serial.print('"');

    // operandos e resultado
    Serial.print(F(",\"x\":")); Serial.print(ula_x);
    Serial.print(F(",\"y\":")); Serial.print(ula_y);
    Serial.print(F(",\"result\":")); Serial.print(ula_result);
    Serial.print(F(",\"carry\":")); Serial.print(ula_carry);

    // estado compartilhado
    Serial.print(F(",\"has_op\":")); Serial.print(ula_has_op ? F("true") : F("false"));
    Serial.print(F(",\"has_x\":"));  Serial.print(ula_has_x  ? F("true") : F("false"));
    Serial.print(F(",\"has_y\":"));  Serial.print(ula_has_y  ? F("true") : F("false"));

    Serial.print(F(",\"focus_field\":")); Serial.print(ula_focus);
    Serial.print(F(",\"focus_field_name\":\""));
    if      (ula_focus == FOCUS_OP) Serial.print(F("OP"));
    else if (ula_focus == FOCUS_X)  Serial.print(F("X"));
    else                            Serial.print(F("Y"));
    Serial.print('"');

    Serial.print(F(",\"last_input_source\":\""));
    Serial.print(ula_source == SRC_API ? F("api") : F("hardware"));
    Serial.print('"');

    Serial.print(F(",\"state_version\":")); Serial.print(ula_version);

    // endereços de memória
    Serial.print(F(",\"addr_estado\":\"0x")); printHex4((uint16_t)&ula_estado); Serial.print('"');
    Serial.print(F(",\"addr_x\":\"0x"));      printHex4((uint16_t)&ula_x);      Serial.print('"');
    Serial.print(F(",\"addr_y\":\"0x"));      printHex4((uint16_t)&ula_y);      Serial.print('"');
    Serial.print(F(",\"addr_result\":\"0x")); printHex4((uint16_t)&ula_result);  Serial.print('"');
    Serial.print(F(",\"addr_carry\":\"0x"));  printHex4((uint16_t)&ula_carry);   Serial.print('"');
    Serial.print(F(",\"addr_op\":\"0x"));     printHex4((uint16_t)&ula_op);      Serial.print('"');

    Serial.print('}');   // fecha "ula"
    Serial.println('}'); // fecha snapshot + '\n'

    sram_offset   = (sram_offset   + BLOCK_SIZE) % SRAM_SIZE;
    eeprom_offset = (eeprom_offset + BLOCK_SIZE) % EEPROM_SIZE;
    flash_offset  = (flash_offset  + BLOCK_SIZE) % FLASH_SIZE;
}

// ── Setup / Loop ──────────────────────────────────────────────────────────────

void setup() {
    Serial.begin(115200);

    pinMode(LED_B0,    OUTPUT);
    pinMode(LED_B1,    OUTPUT);
    pinMode(LED_B2,    OUTPUT);
    pinMode(LED_B3,    OUTPUT);
    pinMode(LED_CARRY, OUTPUT);
    update_leds(0, 0);

    pinMode(SW_B0, INPUT_PULLUP);
    pinMode(SW_B1, INPUT_PULLUP);
    pinMode(SW_B2, INPUT_PULLUP);
    pinMode(SW_B3, INPUT_PULLUP);
    pinMode(BTN, INPUT);

    while (!Serial) {}
    last_snapshot = millis();
}

void loop() {
    // 0. Processa comandos seriais pendentes (não bloqueante)
    poll_serial_commands();

    // 1. Lê ADC
    for (uint8_t ch = 0; ch < 6; ch++) adc_ch[ch] = analogRead(ch);

    // 2. Lê chaves e debounce do botão
    uint8_t sw_val = read_switches();
    handle_button();

    // 3. Máquina de estados
    if (ula_estado == STATE_EDITING) {
        // Atualiza live o campo em foco a partir das chaves (preview nos LEDs)
        switch (ula_focus) {
            case FOCUS_OP:
                ula_op = sw_val & 0x07;
                update_leds(ula_op, 0);
                break;
            case FOCUS_X:
                ula_x = sw_val & 0x0F;
                update_leds(ula_x, 0);
                break;
            case FOCUS_Y:
                ula_y = sw_val & 0x0F;
                update_leds(ula_y, 0);
                break;
        }

        if (btn_event) {
            btn_event = false;
            confirm_focus_and_advance();
        }

    } else {   // STATE_RESULT
        update_leds(ula_result, ula_carry);
        if (btn_event) {
            btn_event = false;
            do_reset();
        }
    }

    // 4. Snapshot periódico
    unsigned long now = millis();
    if (now - last_snapshot >= SNAPSHOT_MS) {
        last_snapshot = now;
        send_snapshot();
    }
}
