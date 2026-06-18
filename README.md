# AVR Monitor

Monitor em tempo real dos registradores internos do **Arduino Uno (ATmega328P)**.  
Exibe PORTB/C/D, PINB/C/D, DDRB/C/D, TCNT0/1/2, ADC A0–A5, SREG e dumps de SRAM/EEPROM/FLASH.

---

## Estrutura do projeto

```
arduino-avr-monitor/
├── arduino/
│   └── avr_monitor_firmware/
│       └── avr_monitor_firmware.ino   ← firmware para o Arduino Uno
├── src/
│   └── avr_monitor/
│       ├── __init__.py
│       ├── cli.py           ← modo terminal (Rich)
│       ├── dashboard.py     ← dashboard interativo (Streamlit)
│       ├── serial_client.py ← leitura serial + cliente simulado
│       ├── models.py        ← modelos Pydantic
│       └── formatters.py    ← conversão de bits, hex dump etc.
├── fake_state.example.json  ← exemplo para controlar o modo fake
├── fake_state.json          ← (criado por você; ignorar no git)
├── requirements.txt
└── README.md
```

---

## Pré-requisitos Python no Ubuntu

```bash
sudo apt update
sudo apt install python3 python3-venv python3-pip -y
```

---

## Instalação do ambiente Python

```bash
cd arduino-avr-monitor

python3 -m venv .venv
source .venv/bin/activate

pip install -r requirements.txt
```

---

## Carregar o firmware no Arduino

1. Abra o **Arduino IDE** (1.8+ ou 2.x).
2. Vá em **File → Open** e selecione `arduino/avr_monitor_firmware/avr_monitor_firmware.ino`.
3. Selecione a placa: **Tools → Board → Arduino Uno**.
4. Selecione a porta: **Tools → Port → /dev/ttyACM0** (ou similar).
5. Clique em **Upload** (→).

O firmware enviará snapshots JSON pela serial a cada **500 ms** em **115200 baud**.

---

## Descobrir a porta serial no Linux

```bash
# Lista portas USB/serial disponíveis
ls /dev/ttyACM* /dev/ttyUSB* 2>/dev/null

# Alternativa com dmesg (após conectar o Arduino)
dmesg | tail -20 | grep -i tty
```

A porta costuma ser `/dev/ttyACM0` ou `/dev/ttyUSB0`.

---

## Resolver permissão de acesso à serial

Se você receber `Permission denied` ao acessar a porta serial:

```bash
# Adiciona seu usuário ao grupo dialout
sudo usermod -aG dialout $USER

# Recarregue o grupo sem fazer logout (alternativa temporária)
newgrp dialout
```

> Após `usermod`, é necessário **fazer logout e login** para o grupo ser aplicado de forma permanente.

---

## Rodar o modo terminal (Rich)

```bash
source .venv/bin/activate

# Com Arduino conectado
python -m src.avr_monitor.cli --port /dev/ttyACM0 --baud 115200

# Com intervalo personalizado
python -m src.avr_monitor.cli --port /dev/ttyACM0 --baud 115200 --interval 0.5

# Modo simulado (sem Arduino)
python -m src.avr_monitor.cli --fake

# Modo simulado com intervalo mais rápido
python -m src.avr_monitor.cli --fake --interval 0.2
```

**Pressione `Ctrl+C` para sair.**

---

## Rodar o dashboard (Streamlit)

> **Importante:** execute sempre a partir da **raiz do projeto** (`arduino-avr-monitor/`).  
> O dashboard usa `Path(__file__).resolve()` para localizar a raiz e adicioná-la ao `sys.path`  
> automaticamente, mas o diretório de trabalho deve ser a raiz para que `fake_state.json`  
> seja encontrado no lugar certo.

```bash
cd /home/nuno/projects/python-test/arduino-avr-monitor
source .venv/bin/activate

streamlit run src/avr_monitor/dashboard.py
```

O navegador abrirá automaticamente em `http://localhost:8501`.

Na **barra lateral**:
- Marque **"Modo simulado"** para testar sem Arduino.
- Configure porta, baud rate e intervalo de refresh.
- Clique em **"Conectar / Reiniciar"** após alterar as configurações.

---

## Modo simulado (--fake)

O modo simulado não requer Arduino conectado.  
Dados são gerados com funções matemáticas (`sin`, `cos`), cobrindo todos os campos do snapshot: portas, timers, ADC, SREG e blocos de memória com rotação automática de endereços.

```bash
# Terminal
python -m src.avr_monitor.cli --fake

# Dashboard (marque o checkbox na sidebar)
streamlit run src/avr_monitor/dashboard.py
```

---

## Modo fake controlável

O `FakeSerialClient` mantém arrays de memória reais em Python:

| Espaço  | Tamanho  | Estado inicial                          |
|---------|----------|-----------------------------------------|
| SRAM    | 2048 B   | zeros; 0x0100=AA, 0x0101=BB, 0x0102=CC |
| EEPROM  | 1024 B   | 0xFF em todos os bytes                  |
| FLASH   | 32768 B  | zeros; 0x0000–0x0003 = DE AD BE EF      |

Você pode controlar qualquer valor do modo fake sem reiniciar a aplicação, simplesmente editando um arquivo JSON.

### Configuração inicial

```bash
cp fake_state.example.json fake_state.json
```

### Rodar com fake_state.json ativo

```bash
# Terminal
python -m src.avr_monitor.cli --fake

# Dashboard (checkbox "Modo simulado" na sidebar)
streamlit run src/avr_monitor/dashboard.py
```

### Formato do fake_state.json

> **Atenção:** JSON não aceita comentários. O arquivo real deve ser JSON válido.  
> Use `fake_state.example.json` como referência e copie apenas o que precisar.

```json
{
  "ports": { "PORTB": 32, "PORTC": 0, "PORTD": 128 },
  "pins":  { "PINB": 32,  "PINC": 0,  "PIND": 128  },
  "ddr":   { "DDRB": 32,  "DDRC": 0,  "DDRD": 0    },
  "adc":   { "A0": 700, "A1": 300, "A2": 0, "A3": 0, "A4": 0, "A5": 0 },
  "flags": { "SREG": 2 },
  "memory_writes": [
    { "space": "sram",   "address": "0x0110", "value": 66  },
    { "space": "eeprom", "address": "0x0000", "value": 17  },
    { "space": "flash",  "address": "0x0000", "value": 222 }
  ]
}
```

Campos ausentes são ignorados — você pode colocar apenas o que quiser sobrescrever.

### Regras de endereçamento

- **SRAM**: o endereço físico começa em `0x0100`.  
  `"address": "0x0110"` → índice `0x0110 − 0x0100 = 16` no array interno.
- **EEPROM** e **FLASH**: endereço começa em `0x0000`.  
  `"address": "0x0000"` → índice `0`.
- `value` é truncado para byte: `value & 0xFF`.

### Exemplos de experimentos

**Mudar PORTB para 32 (0x20):**
```json
{ "ports": { "PORTB": 32 } }
```
Resultado: coluna Hex mostra `0x20`, bits `00100000`.

**Mudar A0 para 700 (≈ 3.42 V):**
```json
{ "adc": { "A0": 700 } }
```
Resultado: tensão calculada `700 × 5 / 1023 = 3.421 V`.

**Ativar flag Z (SREG = 2 = 0b00000010):**
```json
{ "flags": { "SREG": 2 } }
```
Resultado: SREG exibe `0x02`; flag **Z = 1**, demais = 0.

**Escrever 0x42 no endereço SRAM 0x0110:**
```json
{ "memory_writes": [{ "space": "sram", "address": "0x0110", "value": 66 }] }
```
Resultado: quando o dump rotativo passar pelo endereço `0x0110`, o byte aparece como `42` no hex dump.

**Sobrescrever assinatura da FLASH:**
```json
{ "memory_writes": [
  { "space": "flash", "address": "0x0000", "value": 222 },
  { "space": "flash", "address": "0x0001", "value": 173 }
]}
```
Resultado: primeiros dois bytes da FLASH viram `DE AD`.

### Comportamento de robustez

O `FakeSerialClient` relê `fake_state.json` a cada snapshot:

- Arquivo não existe → usa valores padrão simulados, sem erro.
- JSON inválido (você está editando no meio) → ignora silenciosamente, mantém último estado válido.
- Campo ausente → usa o valor padrão ou simulado para aquele campo.
- Valor fora de range → truncado: `& 0xFF` para bytes, `& 0x3FF` para ADC.

---

## Opções da CLI

| Opção        | Padrão          | Descrição                          |
|--------------|-----------------|------------------------------------|
| `--port`     | `/dev/ttyACM0`  | Porta serial do Arduino            |
| `--baud`     | `115200`        | Baud rate da comunicação serial    |
| `--fake`     | desligado       | Ativa modo simulado                |
| `--interval` | `0.5`           | Intervalo de atualização em segundos |

---

## Formato JSON enviado pelo Arduino

Cada linha é um JSON completo (JSON Lines):

```json
{
  "timestamp_ms": 12345,
  "ports": { "PORTB": 5, "PORTC": 0, "PORTD": 128 },
  "pins":  { "PINB": 5, "PINC": 0, "PIND": 128 },
  "ddr":   { "DDRB": 32, "DDRC": 0, "DDRD": 0 },
  "timers": { "TCNT0": 200, "TCNT1": 15300, "TCNT2": 87 },
  "adc": { "A0": 512, "A1": 340, "A2": 0, "A3": 0, "A4": 0, "A5": 0 },
  "flags": { "SREG": 128, "I": 1, "T": 0, "H": 0, "S": 0, "V": 0, "N": 0, "Z": 0, "C": 0 },
  "memory": {
    "sram":   { "start": "0x0100", "bytes": [0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15] },
    "eeprom": { "start": "0x0000", "bytes": [255,255,255,255,255,255,255,255,255,255,255,255,255,255,255,255] },
    "flash":  { "start": "0x0000", "bytes": [12,148,95,0,12,148,95,0,12,148,95,0,12,148,95,0] }
  }
}
```

---

## Decisões técnicas

- **JSON Lines pela serial**: cada snapshot é uma linha JSON terminada em `\n`. O Python descarta linhas inválidas sem interromper a leitura.
- **Blocos de memória rotativos**: enviar toda a SRAM (2 KB) a cada 500 ms saturaria a serial. O firmware envia 16 bytes por frame e avança o offset a cada ciclo, cobrindo toda a memória ao longo do tempo.
- **Strings `F()`**: o firmware usa `F("...")` para manter strings constantes na FLASH em vez da SRAM, economizando os 2 KB de RAM do ATmega328P.
- **`FakeSerialClient`**: gera dados realistas com `sin`/`cos` no ADC e rotação de endereços de memória, permitindo desenvolver e testar o Python sem hardware.
- **Pydantic v2**: validação robusta dos snapshots; campos ausentes recebem valores padrão (0), então uma linha com dados parciais ainda é aceita.
- **Thread de leitura no Streamlit**: o Streamlit re-executa o script a cada refresh; uma thread `daemon` mantém a leitura serial contínua e um lock protege o acesso ao snapshot mais recente.
- **`Rich Live`**: o modo terminal usa `Live` com `screen=True` para atualizar a tela inteira sem flickering, sem dependência de curses.
