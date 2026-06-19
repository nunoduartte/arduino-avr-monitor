# Explicação Técnica — AVR Monitor + ULA 4 bits

Este documento explica as decisões de implementação do projeto: quais bibliotecas
foram usadas, como a interface foi construída, e como cada parte do monitor de
registradores se conecta (ou não) à ULA implementada no Arduino.

---

## 1. Arquitetura geral

O projeto tem duas partes que rodam em processos/dispositivos diferentes:

```
┌─────────────────────────┐         JSON Lines          ┌──────────────────────────┐
│   Arduino Uno            │   ── serial 115200 baud ──▶ │   Computador (Python)    │
│   (ATmega328P)           │                              │                          │
│                          │                              │  serial_client.py        │
│  ula_avr_monitor_        │                              │      ↓ parse JSON         │
│  firmware.ino            │                              │  models.py (Pydantic)    │
│                          │                              │      ↓ valida e tipa      │
│  - roda a máquina de     │                              │  cli.py  /  dashboard.py │
│    estados da ULA        │                              │      ↓ renderiza          │
│  - lê registradores AVR  │                              │  Terminal (Rich)  ou      │
│  - monta o JSON e envia  │                              │  Navegador (Streamlit)   │
└─────────────────────────┘                              └──────────────────────────┘
```

**Importante**: a lógica da ULA roda inteiramente **dentro do Arduino**. O Python
nunca envia comandos para o Arduino — ele só lê o que o firmware manda e exibe.
Essa é uma diferença fundamental em relação ao protótipo anterior em Pygame +
PyFirmata, onde a máquina de estados rodava no computador e o Arduino era só um
"controlador remoto" de LEDs/chaves.

---

## 2. Bibliotecas e ferramentas usadas

### No Arduino (C++)

| Biblioteca | Para que serve |
|---|---|
| `avr/pgmspace.h` | Permite usar a macro `F("texto")`, que mantém strings literais na memória **FLASH** em vez de copiá-las para a **SRAM**. O ATmega328P só tem 2 KB de SRAM, então isso evita desperdiçar essa memória escassa com textos fixos do JSON. |
| `EEPROM.h` | Biblioteca padrão do Arduino para ler a EEPROM (1 KB de memória não-volátil), usada no dump de memória. |
| *(nenhuma lib de JSON)* | O JSON é montado **manualmente** com `Serial.print()`. Não usamos `ArduinoJson` de propósito — isso obriga a entender exatamente a estrutura de cada campo, e evita overhead de uma lib externa em um firmware que já lê muitos registradores. |

### No Python

| Biblioteca | Para que serve |
|---|---|
| `pyserial` | Abre a porta serial (`/dev/ttyACM0`, `COMx`) e lê linha por linha o que o Arduino envia. |
| `pydantic` | Define os "modelos" de dados (`AVRSnapshot`, `ULASnapshot` etc. em `models.py`). Cada linha JSON recebida é validada e convertida em objetos Python tipados — se um campo vier corrompido ou faltando, o Pydantic usa o valor padrão em vez de quebrar o programa. |
| `click` | Cria a interface de linha de comando (`python -m src.avr_monitor.cli --port ... --fake --ula-only`), incluindo `--help` automático e validação de opções. |
| `rich` | Constrói a interface do terminal: painéis (`Panel`), tabelas (`Table`), texto colorido (`Text`), e o modo "tela cheia" que atualiza sem piscar (`Live`). |
| `streamlit` | Framework que transforma um script Python em uma página web interativa — é o que gera o dashboard no navegador, sem precisar escrever HTML/JS/CSS. |
| `pandas` | Usado só para montar tabelas (`DataFrame`) de forma fácil dentro do Streamlit (`st.dataframe`). |

---

## 3. Como a interface foi construída

### 3.1 — CLI (terminal), em `src/avr_monitor/cli.py`

Usa a biblioteca **Rich**, especificamente o componente `Live`:

```python
with Live(console=console, refresh_per_second=4, screen=True) as live:
    for snap in client.snapshots():
        live.update(build_layout(snap, fake, ula_log))
```

- `client.snapshots()` é um **generator** Python (`yield`) que produz um `AVRSnapshot`
  por vez, infinitamente, lendo da serial real ou simulando (modo fake).
- `Live(... screen=True)` redesenha a tela inteira a cada atualização, sem deixar
  rastro de linhas antigas no terminal (como um `htop`).
- `build_layout()` monta uma árvore de objetos Rich (`Table.grid`, `Panel`, `Columns`)
  que representam visualmente os registradores.
- Existe uma flag `--ula-only` que troca `build_layout()` por `build_ula_only()`,
  mostrando só o painel da ULA (sem as tabelas de registradores gerais).

### 3.2 — Dashboard (navegador), em `src/avr_monitor/dashboard.py`

Usa **Streamlit**. O modelo de execução do Streamlit é diferente de um app
tradicional: **o script inteiro roda de novo a cada interação** (clique, refresh).
Por isso, qualquer estado que precise persistir entre execuções (conexão serial
aberta, generator do modo fake) é guardado em `st.session_state` — um dicionário
que sobrevive entre reruns da mesma sessão do navegador.

```python
time.sleep(refresh)
st.rerun()   # força o script a rodar de novo, criando o "live update"
```

Para o **modo real** (Arduino conectado), uma `threading.Thread` fica lendo a
serial em paralelo e empilhando os snapshots numa `queue.Queue`; a cada rerun do
Streamlit, a fila é "drenada" e o snapshot mais recente é exibido. Isso evita que
a leitura bloqueante da serial trave a interface.

---

## 4. Como cada seção do monitor se conecta à ULA

Esta é a parte mais importante para a apresentação: **nem todo registrador
monitorado tem relação real com a ULA**. Abaixo, a ligação de cada um, com o
trecho de código relevante.

### 4.1 — ADC ↔ Potenciômetro (ligação real, mas independente da ULA)

No firmware (`ula_avr_monitor_firmware.ino`), o potenciômetro em A0 é lido a
cada iteração do `loop()`:

```cpp
for (uint8_t ch = 0; ch < 6; ch++) {
    adc_ch[ch] = analogRead(ch);
}
```

e enviado no JSON em `adc.A0`. Girar o potenciômetro altera a tensão no pino, o
`analogRead()` devolve um valor de 0–1023 proporcional a essa tensão, e isso
aparece imediatamente no monitor. **Porém**: isso não tem nenhuma relação com a
lógica da ULA — é só mais um canal analógico sendo monitorado, do mesmo jeito
que no protótipo original (Pygame), onde o pot só alimentava o osciloscópio.

### 4.2 — PORTD / DDRD ↔ LEDs da ULA (ligação real e direta)

Os LEDs de resultado (D2-D5) e carry (D6) ficam todos no registrador `PORTD`,
bits 2 a 6. A direção desses pinos (`OUTPUT`) é configurada no `setup()`:

```cpp
pinMode(LED_B0, OUTPUT);   // D2 = PD2
pinMode(LED_B1, OUTPUT);   // D3 = PD3
...
```

Isso faz `DDRD` (Data Direction Register D) ter os bits 2-6 = 1. E sempre que o
firmware atualiza os LEDs:

```cpp
void update_leds(uint8_t val4, uint8_t carry) {
    digitalWrite(LED_B0, (val4 >> 0) & 1);
    digitalWrite(LED_B1, (val4 >> 1) & 1);
    digitalWrite(LED_B2, (val4 >> 2) & 1);
    digitalWrite(LED_B3, (val4 >> 3) & 1);
    digitalWrite(LED_CARRY, carry & 1);
}
```

essa chamada está literalmente escrevendo nos bits de `PORTD` por baixo dos
panos (é assim que `digitalWrite` funciona no Arduino). Por isso o monitor
consegue mostrar, lendo só o registrador `PORTD`, exatamente o mesmo valor que
está aceso nos LEDs físicos — sem nenhuma "tradução" feita pelo Python.

No dashboard (`dashboard.py`, função `_render_ula`), essa comparação é feita
automaticamente:

```python
expected_bits = ((ula.result & 0xF) << 2) | (ula.carry << 6)
actual_bits   = portd & 0x7C
leds_ok = actual_bits == expected_bits
```

### 4.3 — Dump de SRAM ↔ Variáveis da ULA (a ligação mais forte)

As variáveis da ULA são declaradas como `volatile` logo no topo do firmware:

```cpp
volatile uint8_t ula_x      = 0;
volatile uint8_t ula_y      = 0;
volatile uint8_t ula_result = 0;
volatile uint8_t ula_carry  = 0;
volatile uint8_t ula_op     = 0;
volatile uint8_t ula_estado = 0;
```

No ATmega328P, variáveis globais ficam fisicamente na SRAM, a partir do
endereço `0x0100`. Como essas são as primeiras variáveis globais do programa,
elas caem exatamente nos primeiros bytes da SRAM de dados.

O firmware descobre o endereço real de cada uma em tempo de execução com o
operador `&` (endereço de memória) e manda isso no JSON:

```cpp
Serial.print(F(",\"addr_x\":\"0x")); printHex4((uint16_t)&ula_x); Serial.print('"');
```

O dump de memória (enviado em blocos de 16 bytes que vão "rodando" por toda a
SRAM a cada snapshot) eventualmente passa por esses endereços — e quando isso
acontece, **os bytes mostrados no dump são literalmente os valores de x, y,
result, carry, op e estado**, sem qualquer interpretação adicional. Isso prova
que o monitor está lendo memória RAM real do programa em execução, não um valor
fabricado.

### 4.4 — Timers (TCNT0/1/2) ↔ NÃO relacionados à ULA

```cpp
Serial.print(F(",\"timers\":{\"TCNT0\":")); Serial.print(TCNT0);
```

`TCNT0`, `TCNT1` e `TCNT2` são contadores de hardware que incrementam sozinhos,
movidos pelo clock interno do microcontrolador (são a base do `millis()` e do
PWM do Arduino). Eles avançam o tempo todo, independente de qualquer operação
da ULA — por isso, **não há causa-efeito para demonstrar aqui**. Eles aparecem
no monitor só porque fazem parte do conjunto de registradores do ATmega328P que
o projeto se propôs a expor.

### 4.5 — SREG ↔ NÃO relacionado ao carry da ULA (atenção a esse ponto)

Este é o ponto mais sutil, e importante esclarecer para o professor: o carry
mostrado no painel da ULA (`ula.carry`) é uma **variável de software**,
calculada manualmente em C:

```cpp
case 4:   // ADD
    tmp = (uint16_t)x + y;
    ula_result = tmp & 0x0F;
    ula_carry  = (tmp > 15) ? 1 : 0;
    break;
```

O bit `C` do registrador `SREG`, por outro lado, é a flag de carry **real do
hardware da ALU do AVR**, atualizada automaticamente a cada instrução de
soma/subtração executada pelo processador — inclusive instruções geradas pelo
compilador para coisas totalmente alheias à ULA (laço do `loop()`,
`Serial.print()`, etc). No momento em que o firmware lê `SREG` para montar o
JSON, esse bit já foi sobrescrito várias vezes por essas outras instruções.
**Não existe relação confiável entre o carry da ULA e o bit C do SREG** — são
dois conceitos que só coincidem de nome.

### 4.6 — EEPROM / FLASH ↔ NÃO relacionados à ULA

A EEPROM nunca é escrita pelo firmware (fica no valor de fábrica, `0xFF`). O
dump de FLASH lê o próprio código compilado do programa via
`pgm_read_byte_near()` — é estático, definido em tempo de compilação, e não
muda com a execução da ULA.

---

## 5. Resumo para a apresentação

| Seção do monitor | Relação com a ULA | O que demonstrar |
|---|---|---|
| **ADC (A0)** | Real, mas independente | Girar o pot e ver o valor mudar em tempo real |
| **PORTD / DDRD** | Real e direta | Resultado calculado aparece nos bits 2-6 de PORTD, igual ao LED físico |
| **Dump de SRAM** | Real e direta (a mais forte) | Bytes no endereço `addr_x`/`addr_y`/etc. são os valores reais das variáveis da ULA |
| **Timers** | Nenhuma | Contadores de hardware livres, usados internamente pelo Arduino |
| **SREG** | Nenhuma (cuidado, é pegadinha comum) | Carry da ULA é variável de software; bit C do SREG é flag de hardware não relacionada |
| **EEPROM / FLASH** | Nenhuma | Memória não-volátil intocada / bytecode estático do programa |

---

## 6. Modo simulado (bônus, para testar sem Arduino)

Para permitir testar a interface sem o hardware, existe `FakeSerialClient` em
`serial_client.py`, que gera os mesmos campos do JSON real, incluindo arrays de
memória (`bytearray`) que realmente armazenam os valores simulados — inclusive
as variáveis da ULA, nos mesmos índices que ocupariam no Arduino real. Isso é
controlado por um arquivo `fake_state.json` (não versionado no Git), que permite
sobrescrever qualquer campo sem reiniciar o programa.
