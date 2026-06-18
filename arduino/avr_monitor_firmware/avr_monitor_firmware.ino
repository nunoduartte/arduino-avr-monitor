/*
  avr_monitor_firmware.ino
  Envia snapshots JSON dos registradores internos do ATmega328P pela serial.
  Baud: 115200, intervalo: 500 ms
*/

#include <avr/pgmspace.h>
#include <EEPROM.h>

#define BLOCK_SIZE 16
#define INTERVAL_MS 500

static uint16_t sramOffset = 0;
static uint16_t eepromOffset = 0;
static uint16_t flashOffset = 0;

// SRAM começa em 0x0100 (após registradores e I/O)
#define SRAM_START 0x0100
#define SRAM_SIZE  2048

#define EEPROM_SIZE 1024
#define FLASH_SIZE  32768

void sendUint8AsHex(uint8_t v) {
  if (v < 0x10) Serial.print('0');
  Serial.print(v, HEX);
}

void sendMemBlock(const char* key, uint16_t baseAddr, uint8_t* ptr, uint16_t blockLen) {
  // Formato: "key":{"start":"0xXXXX","bytes":[...]}
  Serial.print(F("\""));
  Serial.print(key);
  Serial.print(F("\":{\"start\":\"0x"));
  sendUint8AsHex((uint8_t)(baseAddr >> 8));
  sendUint8AsHex((uint8_t)(baseAddr & 0xFF));
  Serial.print(F("\",\"bytes\":["));
  for (uint16_t i = 0; i < blockLen; i++) {
    Serial.print(ptr[i]);
    if (i < blockLen - 1) Serial.print(',');
  }
  Serial.print(F("]}"));
}

void sendFlashBlock(uint16_t startAddr) {
  Serial.print(F("\"flash\":{\"start\":\"0x"));
  sendUint8AsHex((uint8_t)(startAddr >> 8));
  sendUint8AsHex((uint8_t)(startAddr & 0xFF));
  Serial.print(F("\",\"bytes\":["));
  for (uint16_t i = 0; i < BLOCK_SIZE; i++) {
    Serial.print(pgm_read_byte_near(startAddr + i));
    if (i < BLOCK_SIZE - 1) Serial.print(',');
  }
  Serial.print(F("]}"));
}

void sendEepromBlock(uint16_t startAddr) {
  Serial.print(F("\"eeprom\":{\"start\":\"0x"));
  sendUint8AsHex((uint8_t)(startAddr >> 8));
  sendUint8AsHex((uint8_t)(startAddr & 0xFF));
  Serial.print(F("\",\"bytes\":["));
  for (uint16_t i = 0; i < BLOCK_SIZE; i++) {
    Serial.print(EEPROM.read(startAddr + i));
    if (i < BLOCK_SIZE - 1) Serial.print(',');
  }
  Serial.print(F("]}"));
}

void setup() {
  Serial.begin(115200);
  while (!Serial) {}
}

void loop() {
  // Captura SREG antes de qualquer operação
  uint8_t sreg = SREG;

  // Registradores de porta
  uint8_t portb = PORTB, portc = PORTC, portd = PORTD;
  uint8_t pinb  = PINB,  pinc  = PINC,  pind  = PIND;
  uint8_t ddrb  = DDRB,  ddrc  = DDRC,  ddrd  = DDRD;

  // Timers
  uint8_t  tcnt0 = TCNT0;
  uint16_t tcnt1 = TCNT1;
  uint8_t  tcnt2 = TCNT2;

  // ADC
  uint16_t adc[6];
  for (uint8_t ch = 0; ch < 6; ch++) {
    adc[ch] = analogRead(ch);
  }

  // SREG flags (bit positions conforme datasheet ATmega328P)
  uint8_t flag_c = (sreg >> 0) & 1;
  uint8_t flag_z = (sreg >> 1) & 1;
  uint8_t flag_n = (sreg >> 2) & 1;
  uint8_t flag_v = (sreg >> 3) & 1;
  uint8_t flag_s = (sreg >> 4) & 1;
  uint8_t flag_h = (sreg >> 5) & 1;
  uint8_t flag_t = (sreg >> 6) & 1;
  uint8_t flag_i = (sreg >> 7) & 1;

  // Bloco SRAM atual
  uint16_t sramAddr = SRAM_START + sramOffset;
  uint8_t* sramPtr  = (uint8_t*)sramAddr;

  // Monta JSON linha
  Serial.print(F("{"));

  // timestamp
  Serial.print(F("\"timestamp_ms\":"));
  Serial.print(millis());
  Serial.print(F(","));

  // ports
  Serial.print(F("\"ports\":{"));
  Serial.print(F("\"PORTB\":")); Serial.print(portb); Serial.print(F(","));
  Serial.print(F("\"PORTC\":")); Serial.print(portc); Serial.print(F(","));
  Serial.print(F("\"PORTD\":")); Serial.print(portd);
  Serial.print(F("},"));

  // pins
  Serial.print(F("\"pins\":{"));
  Serial.print(F("\"PINB\":")); Serial.print(pinb); Serial.print(F(","));
  Serial.print(F("\"PINC\":")); Serial.print(pinc); Serial.print(F(","));
  Serial.print(F("\"PIND\":")); Serial.print(pind);
  Serial.print(F("},"));

  // ddr
  Serial.print(F("\"ddr\":{"));
  Serial.print(F("\"DDRB\":")); Serial.print(ddrb); Serial.print(F(","));
  Serial.print(F("\"DDRC\":")); Serial.print(ddrc); Serial.print(F(","));
  Serial.print(F("\"DDRD\":")); Serial.print(ddrd);
  Serial.print(F("},"));

  // timers
  Serial.print(F("\"timers\":{"));
  Serial.print(F("\"TCNT0\":")); Serial.print(tcnt0); Serial.print(F(","));
  Serial.print(F("\"TCNT1\":")); Serial.print(tcnt1); Serial.print(F(","));
  Serial.print(F("\"TCNT2\":")); Serial.print(tcnt2);
  Serial.print(F("},"));

  // adc
  Serial.print(F("\"adc\":{"));
  for (uint8_t ch = 0; ch < 6; ch++) {
    Serial.print(F("\"A")); Serial.print(ch); Serial.print(F("\":"));
    Serial.print(adc[ch]);
    if (ch < 5) Serial.print(',');
  }
  Serial.print(F("},"));

  // flags
  Serial.print(F("\"flags\":{"));
  Serial.print(F("\"SREG\":")); Serial.print(sreg); Serial.print(F(","));
  Serial.print(F("\"I\":")); Serial.print(flag_i); Serial.print(F(","));
  Serial.print(F("\"T\":")); Serial.print(flag_t); Serial.print(F(","));
  Serial.print(F("\"H\":")); Serial.print(flag_h); Serial.print(F(","));
  Serial.print(F("\"S\":")); Serial.print(flag_s); Serial.print(F(","));
  Serial.print(F("\"V\":")); Serial.print(flag_v); Serial.print(F(","));
  Serial.print(F("\"N\":")); Serial.print(flag_n); Serial.print(F(","));
  Serial.print(F("\"Z\":")); Serial.print(flag_z); Serial.print(F(","));
  Serial.print(F("\"C\":")); Serial.print(flag_c);
  Serial.print(F("},"));

  // memory
  Serial.print(F("\"memory\":{"));
  sendMemBlock("sram", sramAddr, sramPtr, BLOCK_SIZE);
  Serial.print(F(","));
  sendEepromBlock(eepromOffset);
  Serial.print(F(","));
  sendFlashBlock(flashOffset);
  Serial.print(F("}"));

  Serial.println(F("}"));

  // Avança blocos de memória (rotação)
  sramOffset   = (sramOffset   + BLOCK_SIZE) % SRAM_SIZE;
  eepromOffset = (eepromOffset + BLOCK_SIZE) % EEPROM_SIZE;
  flashOffset  = (flashOffset  + BLOCK_SIZE) % FLASH_SIZE;

  delay(INTERVAL_MS);
}
