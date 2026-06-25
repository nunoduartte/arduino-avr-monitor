# AVR Monitor

Monitor em tempo real dos registradores internos do **Arduino Uno (ATmega328P)**.  
Exibe PORTB/C/D, PINB/C/D, DDRB/C/D, TCNT0/1/2, ADC A0–A5, SREG e dumps de SRAM/EEPROM/FLASH.

---

## Estrutura do projeto

```
arduino-avr-monitor/
├── arduino/
│   ├── avr_monitor_firmware/
│   │   └── avr_monitor_firmware.ino       ← firmware original (sem ULA)
│   └── ula_avr_monitor_firmware/
│       └── ula_avr_monitor_firmware.ino   ← firmware com ULA 4 bits + comando/ACK
├── src/
│   └── avr_monitor/
│       ├── __init__.py
│       ├── cli.py             ← modo terminal (Rich); também suporta --send-ula
│       ├── dashboard.py       ← dashboard interativo (Streamlit)
│       ├── ula_command.py     ← script de comando único (envia op e sai)
│       ├── serial_client.py   ← leitura serial + cliente simulado + send_ula_command/messages
│       ├── arduino_service.py ← único dono da porta serial; usado pela API HTTP
│       ├── api.py             ← API HTTP (FastAPI) por cima do ArduinoService
│       ├── models.py          ← modelos Pydantic (inclui UlaAck)
│       └── formatters.py      ← conversão de bits, hex dump etc.
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

## ULA — estado compartilhado API ↔ hardware

A ULA funciona como um **estado compartilhado** entre a API e o hardware
físico. Cada campo (`op`, `x`, `y`) pode ser definido por qualquer origem,
em qualquer ordem. O botão físico ou a API podem disparar o cálculo.

### Modelo de estado

| Campo | Tipo | Descrição |
|---|---|---|
| `op` / `x` / `y` | int | valores dos campos (4 bits cada; op = 0-7) |
| `has_op` / `has_x` / `has_y` | bool | indica se o campo foi confirmado |
| `focus_field` | 0/1/2 | qual campo as chaves físicas editam agora (OP/X/Y) |
| `estado` | 0/4 | 0=EDITING (campos editáveis), 4=RESULT (calculado) |
| `last_input_source` | string | `"hardware"` ou `"api"` |
| `state_version` | int | incrementa a cada mudança — útil para polling |

### Fluxo físico (3 pressionamentos, sem API)

```
OP  → ajusta chaves → pressiona botão → has_op=true, focus→X
X   → ajusta chaves → pressiona botão → has_x=true,  focus→Y
Y   → ajusta chaves → pressiona botão → has_y=true → CALCULA → RESULT
RESULT → pressiona botão → reset, volta ao início
```

> **Nota:** o fluxo físico passa de 4 para **3 pressionamentos** em relação
> à versão anterior. O antigo estado de "confirmação de Y" foi eliminado;
> confirmar o último campo faltante dispara o cálculo automaticamente.

### Regra de cálculo (hardware ou API)

O cálculo só executa quando `has_op && has_x && has_y`. Se o campo em foco
ainda não tem `has_*=true`, o valor ao vivo das chaves é contado como
"effective has" — então `compute_current` pela API funciona mesmo que o
usuário ainda não tenha pressionado o botão para aquele campo.

### Regra de conflito

**Última escrita vence.** Se API define `x=1` e depois hardware redefine
`x=5` (ao confirmar com o botão com as chaves em 5), `x` passa a ser 5.
`state_version` permite ao cliente detectar qualquer mudança.

### Protocolo serial — comandos e ACKs

O Arduino aceita comandos JSON Lines pela serial. Cada comando gera um ACK:

| Comando | Exemplo |
|---|---|
| Setar campo | `{"cmd":"set_field","field":"op","value":"ADD"}` |
| Setar campo | `{"cmd":"set_field","field":"x","value":1}` |
| Mudar focus | `{"cmd":"focus","field":"y"}` |
| Calcular | `{"cmd":"compute_current"}` |
| Reset | `{"cmd":"reset"}` |
| Compat (tudo de uma vez) | `{"cmd":"ula","op":4,"x":1,"y":2}` |

ACK de `set_field`:
```json
{"type":"ack","cmd":"set_field","ok":true,"field":"x","value":1}
```

ACK de `compute_current` (sucesso):
```json
{"type":"ack","cmd":"compute_current","ok":true,"op":4,"op_name":"ADD","x":1,"y":2,"result":3,"carry":0}
```

ACK de `compute_current` (campos faltando):
```json
{"type":"ack","cmd":"compute_current","ok":false,"error":"missing_fields","missing":["x","y"]}
```

### Snapshot expandido (seção `ula`)

```json
{
  "estado": 0,
  "estado_name": "EDITING",
  "op": 4, "op_name": "ADD", "op_code": "100",
  "x": 1, "y": 0, "result": 0, "carry": 0,
  "has_op": true, "has_x": true, "has_y": false,
  "focus_field": 2, "focus_field_name": "Y",
  "last_input_source": "api",
  "state_version": 5
}
```

---

## API HTTP (FastAPI)

A API mantém a porta serial aberta continuamente (via `ArduinoService`)
e expõe endpoints REST para qualquer cliente (curl, frontend, outro serviço).

**O Arduino é a única fonte de verdade** — não há estado "pending" em Python.
Cada chamada de `/api/ula/field` envia um comando ao Arduino e aguarda o ACK.
`GET /api/state` só lê o último snapshot em memória, sem tocar a serial.

### `ArduinoService` — único dono da porta serial

- Abre a serial **uma vez** no startup.
- Uma única thread (`_reader_loop`) lê continuamente via `client.messages()`.
- Snapshots → gravados em `_latest_snapshot` (lock).
- ACKs → resolvem o `threading.Event` do comando em voo.
- Um `_cmd_lock` garante um único comando "em voo" por vez, mesmo com requisições concorrentes.

### Endpoints

| Método | Rota | Descrição |
|---|---|---|
| GET | `/api/health` | `{"ok":true}` |
| GET | `/api/state` | Último snapshot — nunca lê a serial |
| POST | `/api/ula/field` | Define um campo: `{"field":"op"\|"x"\|"y","value":...}` |
| POST | `/api/ula/focus` | Muda o focus do hardware: `{"field":"op"\|"x"\|"y"}` |
| POST | `/api/ula/compute-current` | Dispara cálculo (exige has_op+has_x+has_y) |
| POST | `/api/ula/reset` | Zera tudo |
| POST | `/api/ula` | Compat: `{"op":...,"x":...,"y":...}` → seta tudo + calcula |

### Subir a API

```bash
cd arduino-avr-monitor
source .venv/bin/activate

# modo fake (sem Arduino):
AVR_MONITOR_FAKE=1 uvicorn src.avr_monitor.api:app --reload --port 8000

# com Arduino real:
AVR_MONITOR_PORT=/dev/ttyACM0 uvicorn src.avr_monitor.api:app --reload --port 8000
```

### Testar endpoints básicos

```bash
curl http://localhost:8000/api/health
curl -s http://localhost:8000/api/state | python3 -m json.tool

# Operação completa em uma chamada (compat):
curl -s -X POST http://localhost:8000/api/ula \
  -H 'Content-Type: application/json' \
  -d '{"op":"ADD","x":1,"y":2}' | python3 -m json.tool

# Reset:
curl -X POST http://localhost:8000/api/ula/reset
```

---

## Fluxos de aceite (API + hardware híbrido)

### Fluxo A — API define op e x; hardware define y e computa

```bash
# 1. Definir op e x pela API
curl -X POST http://localhost:8000/api/ula/field \
  -H 'Content-Type: application/json' \
  -d '{"field":"op","value":"ADD"}'

curl -X POST http://localhost:8000/api/ula/field \
  -H 'Content-Type: application/json' \
  -d '{"field":"x","value":1}'

# 2. Direcionar o focus do hardware para Y
curl -X POST http://localhost:8000/api/ula/focus \
  -H 'Content-Type: application/json' \
  -d '{"field":"y"}'

# 3. No Arduino físico: ajustar chaves para Y=2, pressionar botão
#    → has_y=true, todos confirmados → calcula automaticamente

# 4. Verificar resultado
curl -s http://localhost:8000/api/state | python3 -m json.tool
# Esperado: op_name=ADD, x=1, y=2, result=3, carry=0
```

### Fluxo B — hardware confirma op; API define x; hardware define y e computa

```bash
# 1. No Arduino físico:
#    chaves em op=ADD (bits 0-2 = 100), pressionar botão
#    → has_op=true, focus avança para X

# 2. Definir x pela API (focus estava em X → avança automaticamente para Y)
curl -X POST http://localhost:8000/api/ula/field \
  -H 'Content-Type: application/json' \
  -d '{"field":"x","value":1}'

# 3. No Arduino físico: chaves em Y=2, pressionar botão
#    → has_y=true, todos confirmados → calcula

# 4. Verificar
curl -s http://localhost:8000/api/state | python3 -m json.tool
# Esperado: result=3
```

### Fluxo C — API define y primeiro; hardware define op e x, e computa

```bash
# 1. Definir y pela API (focus fica em OP — não avança pois focus≠y)
curl -X POST http://localhost:8000/api/ula/field \
  -H 'Content-Type: application/json' \
  -d '{"field":"y","value":2}'

# 2. No Arduino físico:
#    chaves em op=ADD, pressionar botão → has_op=true, focus→X
#    chaves em x=1,   pressionar botão → has_x=true, has_y já true → CALCULA

# 3. Verificar
curl -s http://localhost:8000/api/state | python3 -m json.tool
# Esperado: result=3
```

### Fluxo D — hardware define todos os campos; API computa

```bash
# 1. No Arduino físico:
#    chaves em op=ADD, pressionar botão → focus→X
#    chaves em x=1,   pressionar botão → focus→Y
#    ajustar chaves para y=2 (sem pressionar botão)

# 2. API computa (focus=Y com valor vivo = 2 → effective has_y=true)
curl -s -X POST http://localhost:8000/api/ula/compute-current | python3 -m json.tool
# Esperado: ok=true, result=3, carry=0

# 3. Verificar estado
curl -s http://localhost:8000/api/state | python3 -m json.tool
```

### Importante: não rode a API e `cli.py`/`ula_command.py` na mesma porta

A API resolve a disputa entre suas próprias requisições (um único
`_reader_loop`, um único `_cmd_lock`). Não rode a API e outros scripts
Python na mesma porta serial ao mesmo tempo.

---

## Opções da CLI

| Opção         | Padrão          | Descrição                          |
|---------------|-----------------|------------------------------------|
| `--port`      | `/dev/ttyACM0`  | Porta serial do Arduino            |
| `--baud`      | `115200`        | Baud rate da comunicação serial    |
| `--fake`      | desligado       | Ativa modo simulado                |
| `--interval`  | `0.5`           | Intervalo de atualização em segundos |
| `--ula-only`  | desligado       | Mostra só o painel da ULA (sem registradores/ADC/memória) |
| `--send-ula`  | —               | `OP X Y` — envia um comando ULA, imprime o resultado e sai (não abre o monitor) |

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
