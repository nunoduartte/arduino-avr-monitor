/*
  ula_avr_monitor_firmware.ino
  ULA 4 bits + monitor de registradores AVR — ATmega328P (Arduino Uno)

  Pinagem:
    D2  (PD2) — LED B0   : bit 0 do resultado
    D3  (PD3) — LED B1   : bit 1 do resultado
    D4  (PD4) — LED B2   : bit 2 do resultado
    D5  (PD5) — LED B3   : bit 3 do resultado
    D6  (PD6) — LED Carry
    D7  (PD7) — Chave B0 : INPUT_PULLUP, LOW = ativo
    D8  (PB0) — Chave B1 : INPUT_PULLUP, LOW = ativo
    D9  (PB1) — Chave B2 : INPUT_PULLUP, LOW = ativo
    D10 (PB2) — Chave B3 : INPUT_PULLUP, LOW = ativo
    D11 (PB3) — Botão    : INPUT + pull-down EXTERNO (HIGH = pressionado)
    A0        — Potenciômetro (lido e enviado no JSON; não seleciona operação)

  Estados da máquina:
    0 = Seleção de operação (chaves B0-B2 → op 0-7; LEDs mostram op atual)
    1 = Entrada de X       (chaves B0-B3 → X; LEDs mostram preview de X)
    2 = Entrada de Y       (chaves B0-B3 → Y; LEDs mostram preview de Y)
    3 = Y nos LEDs         (aguarda confirmação para calcular)
    4 = Resultado          (LEDs mostram resultado + carry; confirmar reinicia)

  Operações (op 0-7):
    0=AND  1=OR  2=NOT(Y)  3=XOR  4=ADD  5=SUB  6=MUL  7=DIV
    NOT opera sobre Y (não X), igual ao código original ula_final_3.py.
    SUB clampado em 0 (sem borrow); MUL com carry se produto > 15.

  Protocolo serial:
    JSON Lines, 115200 baud, um snapshot por linha a cada 500 ms.
*/

#include <avr/pgmspace.h>
#include <EEPROM.h>

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
#define BLOCK_SIZE    16u
#define SRAM_SIZE   2048u
#define EEPROM_SIZE 1024u
#define FLASH_SIZE 32768u
#define SRAM_BASE   0x0100u
#define SNAPSHOT_MS   500u
#define DEBOUNCE_MS    50u

// ── Variáveis da ULA (volatile — visíveis na SRAM mesmo com otimização) ──────
// Declaradas na ordem em que aparecem na SRAM para facilitar leitura do dump.
volatile uint8_t ula_x      = 0;   // operando A (4 bits)
volatile uint8_t ula_y      = 0;   // operando B (4 bits)
volatile uint8_t ula_result = 0;   // resultado  (4 bits)
volatile uint8_t ula_carry  = 0;   // carry/overflow (1 bit)
volatile uint8_t ula_op     = 0;   // índice de operação (0-7)
volatile uint8_t ula_estado = 0;   // estado da máquina  (0-4)

// ── Debounce do botão ─────────────────────────────────────────────────────────
static uint8_t       btn_raw_prev     = LOW;
static uint8_t       btn_debounced    = LOW;
static unsigned long btn_debounce_ms  = 0;
static bool          btn_event        = false;

// ── Offsets de dump de memória (rotativos) ────────────────────────────────────
static uint16_t sram_offset   = 0;
static uint16_t eeprom_offset = 0;
static uint16_t flash_offset  = 0;

// ── ADC pré-lido ─────────────────────────────────────────────────────────────
static uint16_t adc_ch[6];

static unsigned long last_snapshot = 0;

// ── Helpers ──────────────────────────────────────────────────────────────────

// Imprime endereço de 16 bits em HEX com 4 dígitos, sem alocação dinâmica.
void printHex4(uint16_t v) {
    if (v < 0x1000) Serial.print('0');
    if (v < 0x0100) Serial.print('0');
    if (v < 0x0010) Serial.print('0');
    Serial.print(v, HEX);
}

// Lê as quatro chaves (INPUT_PULLUP: LOW = pressionada = bit 1).
uint8_t read_switches() {
    uint8_t val = 0;
    if (digitalRead(SW_B0) == LOW) val |= 0x01;
    if (digitalRead(SW_B1) == LOW) val |= 0x02;
    if (digitalRead(SW_B2) == LOW) val |= 0x04;
    if (digitalRead(SW_B3) == LOW) val |= 0x08;
    return val;
}

// Debounce do botão com detecção de rising edge (LOW→HIGH).
void handle_button() {
    unsigned long now = millis();
    uint8_t cur = digitalRead(BTN);
    if (cur != btn_raw_prev) {
        btn_debounce_ms = now;
    }
    btn_raw_prev = cur;
    if ((now - btn_debounce_ms) >= DEBOUNCE_MS) {
        if (cur == HIGH && btn_debounced == LOW) {
            btn_event = true;
        }
        btn_debounced = cur;
    }
}

// Atualiza os 5 LEDs de saída (4 bits de valor + carry).
void update_leds(uint8_t val4, uint8_t carry) {
    digitalWrite(LED_B0,    (val4 >> 0) & 1);
    digitalWrite(LED_B1,    (val4 >> 1) & 1);
    digitalWrite(LED_B2,    (val4 >> 2) & 1);
    digitalWrite(LED_B3,    (val4 >> 3) & 1);
    digitalWrite(LED_CARRY, carry & 1);
}

// Calcula a operação selecionada e atualiza ula_result / ula_carry.
void compute_ula() {
    uint8_t x = ula_x & 0x0F;
    uint8_t y = ula_y & 0x0F;
    uint16_t tmp;
    switch (ula_op) {
        case 0:                                                     // AND
            ula_result = (x & y) & 0x0F;   ula_carry = 0; break;
        case 1:                                                     // OR
            ula_result = (x | y) & 0x0F;   ula_carry = 0; break;
        case 2:                                                     // NOT Y
            ula_result = (~y)    & 0x0F;   ula_carry = 0; break;
        case 3:                                                     // XOR
            ula_result = (x ^ y) & 0x0F;   ula_carry = 0; break;
        case 4:                                                     // ADD
            tmp = (uint16_t)x + y;
            ula_result = tmp & 0x0F;  ula_carry = (tmp > 15) ? 1 : 0; break;
        case 5:                                                     // SUB (clamped)
            ula_result = (x >= y) ? (x - y) & 0x0F : 0;
            ula_carry = 0; break;
        case 6:                                                     // MUL
            tmp = (uint16_t)x * y;
            ula_result = tmp & 0x0F;  ula_carry = (tmp > 15) ? 1 : 0; break;
        case 7:                                                     // DIV
            ula_result = (y != 0) ? (x / y) & 0x0F : 0;
            ula_carry = 0; break;
        default:
            ula_result = 0; ula_carry = 0; break;
    }
}

// Imprime o nome da operação (string armazenada em FLASH via F()).
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

// ── Emissão de JSON ───────────────────────────────────────────────────────────

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
    uint8_t sreg = SREG;   // captura SREG antes de qualquer operação

    Serial.print(F("{\"timestamp_ms\":"));
    Serial.print(millis());

    // ports
    Serial.print(F(",\"ports\":{\"PORTB\":"));  Serial.print(PORTB);
    Serial.print(F(",\"PORTC\":"));             Serial.print(PORTC);
    Serial.print(F(",\"PORTD\":"));             Serial.print(PORTD);
    Serial.print(F("}"));

    // pins
    Serial.print(F(",\"pins\":{\"PINB\":"));    Serial.print(PINB);
    Serial.print(F(",\"PINC\":"));              Serial.print(PINC);
    Serial.print(F(",\"PIND\":"));              Serial.print(PIND);
    Serial.print(F("}"));

    // ddr
    Serial.print(F(",\"ddr\":{\"DDRB\":"));     Serial.print(DDRB);
    Serial.print(F(",\"DDRC\":"));              Serial.print(DDRC);
    Serial.print(F(",\"DDRD\":"));              Serial.print(DDRD);
    Serial.print(F("}"));

    // timers
    Serial.print(F(",\"timers\":{\"TCNT0\":")); Serial.print(TCNT0);
    Serial.print(F(",\"TCNT1\":"));             Serial.print(TCNT1);
    Serial.print(F(",\"TCNT2\":"));             Serial.print(TCNT2);
    Serial.print(F("}"));

    // adc (usa array pré-lido para não bloquear Serial durante leitura)
    Serial.print(F(",\"adc\":{"));
    for (uint8_t ch = 0; ch < 6; ch++) {
        Serial.print(F("\"A")); Serial.print(ch); Serial.print(F("\":"));
        Serial.print(adc_ch[ch]);
        if (ch < 5) Serial.print(',');
    }
    Serial.print(F("}"));

    // flags (SREG capturado no início do snapshot)
    Serial.print(F(",\"flags\":{\"SREG\":")   ); Serial.print(sreg);
    Serial.print(F(",\"I\":"));  Serial.print((sreg >> 7) & 1);
    Serial.print(F(",\"T\":"));  Serial.print((sreg >> 6) & 1);
    Serial.print(F(",\"H\":"));  Serial.print((sreg >> 5) & 1);
    Serial.print(F(",\"S\":"));  Serial.print((sreg >> 4) & 1);
    Serial.print(F(",\"V\":"));  Serial.print((sreg >> 3) & 1);
    Serial.print(F(",\"N\":"));  Serial.print((sreg >> 2) & 1);
    Serial.print(F(",\"Z\":"));  Serial.print((sreg >> 1) & 1);
    Serial.print(F(",\"C\":"));  Serial.print((sreg >> 0) & 1);
    Serial.print(F("}"));

    // memory (dump rotativo de 16 bytes por seção)
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

    // ula
    Serial.print(F(",\"ula\":{\"estado\":"));   Serial.print(ula_estado);
    Serial.print(F(",\"op\":"));                Serial.print(ula_op);
    Serial.print(F(",\"op_name\":\""));         printOpName(ula_op); Serial.print('"');
    // op_code: string de 3 bits em binário
    Serial.print(F(",\"op_code\":\""));
    Serial.print((ula_op >> 2) & 1);
    Serial.print((ula_op >> 1) & 1);
    Serial.print((ula_op >> 0) & 1);
    Serial.print('"');
    Serial.print(F(",\"x\":"));                 Serial.print(ula_x);
    Serial.print(F(",\"y\":"));                 Serial.print(ula_y);
    Serial.print(F(",\"result\":"));            Serial.print(ula_result);
    Serial.print(F(",\"carry\":"));             Serial.print(ula_carry);
    Serial.print(F(",\"addr_estado\":\"0x")); printHex4((uint16_t)&ula_estado); Serial.print('"');
    Serial.print(F(",\"addr_x\":\"0x"));      printHex4((uint16_t)&ula_x);      Serial.print('"');
    Serial.print(F(",\"addr_y\":\"0x"));      printHex4((uint16_t)&ula_y);      Serial.print('"');
    Serial.print(F(",\"addr_result\":\"0x")); printHex4((uint16_t)&ula_result);  Serial.print('"');
    Serial.print(F(",\"addr_carry\":\"0x"));  printHex4((uint16_t)&ula_carry);   Serial.print('"');
    Serial.print('}');

    Serial.println('}');   // fecha o JSON principal e emite '\n' (JSON Lines)

    // Avança offsets rotativos
    sram_offset   = (sram_offset   + BLOCK_SIZE) % SRAM_SIZE;
    eeprom_offset = (eeprom_offset + BLOCK_SIZE) % EEPROM_SIZE;
    flash_offset  = (flash_offset  + BLOCK_SIZE) % FLASH_SIZE;
}

// ── Setup / Loop ──────────────────────────────────────────────────────────────

void setup() {
    Serial.begin(115200);

    // Saídas: LEDs
    pinMode(LED_B0,    OUTPUT);
    pinMode(LED_B1,    OUTPUT);
    pinMode(LED_B2,    OUTPUT);
    pinMode(LED_B3,    OUTPUT);
    pinMode(LED_CARRY, OUTPUT);
    update_leds(0, 0);

    // Entradas: chaves com pull-up interno (LOW = pressionada)
    pinMode(SW_B0, INPUT_PULLUP);
    pinMode(SW_B1, INPUT_PULLUP);
    pinMode(SW_B2, INPUT_PULLUP);
    pinMode(SW_B3, INPUT_PULLUP);

    // Entrada: botão com pull-down externo (HIGH = pressionado)
    pinMode(BTN, INPUT);

    while (!Serial) {}   // aguarda USB-serial (necessário em Leonardo; inofensivo no Uno)
    last_snapshot = millis();
}

void loop() {
    // 1. Lê ADC (todos os canais antes do snapshot para não bloquear)
    for (uint8_t ch = 0; ch < 6; ch++) {
        adc_ch[ch] = analogRead(ch);
    }

    // 2. Lê chaves e atualiza debounce do botão
    uint8_t sw_val = read_switches();
    handle_button();

    // 3. Máquina de estados da ULA
    switch (ula_estado) {
        case 0:   // Seleção de operação (B0-B2 das chaves)
            if (sw_val != 0) ula_op = sw_val & 0x07;
            update_leds(ula_op, 0);
            if (btn_event) {
                btn_event = false;
                ula_estado = 1;
            }
            break;

        case 1:   // Entrada de X
            ula_x = sw_val;
            update_leds(ula_x, 0);
            if (btn_event) {
                btn_event = false;
                ula_estado = 2;
            }
            break;

        case 2:   // Entrada de Y
            ula_y = sw_val;
            update_leds(ula_y, 0);
            if (btn_event) {
                btn_event = false;
                ula_estado = 3;
            }
            break;

        case 3:   // Y exibido nos LEDs; aguarda calcular
            update_leds(ula_y, 0);
            if (btn_event) {
                btn_event = false;
                compute_ula();
                ula_estado = 4;
            }
            break;

        case 4:   // Resultado; confirmar reinicia
            update_leds(ula_result, ula_carry);
            if (btn_event) {
                btn_event  = false;
                ula_x      = 0;
                ula_y      = 0;
                ula_result = 0;
                ula_carry  = 0;
                ula_estado = 0;
                update_leds(0, 0);
            }
            break;

        default:
            ula_estado = 0;
            break;
    }

    // 4. Snapshot JSON a cada SNAPSHOT_MS
    unsigned long now = millis();
    if (now - last_snapshot >= SNAPSHOT_MS) {
        last_snapshot = now;
        send_snapshot();
    }
}
