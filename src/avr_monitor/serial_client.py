from __future__ import annotations

import json
import math
import queue
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from .models import (
    ADCSnapshot,
    AVRSnapshot,
    DDRSnapshot,
    FlagsSnapshot,
    MemoryBlock,
    MemorySnapshot,
    PinsSnapshot,
    PortsSnapshot,
    TimersSnapshot,
    UlaAck,
    ULASnapshot,
)

_ULA_OP_NAMES = ["AND", "OR", "NOT", "XOR", "ADD", "SUB", "MUL", "DIV"]
_ULA_OP_CODES = ["000", "001", "010", "011", "100", "101", "110", "111"]


def _compute_ula(x: int, y: int, op: int) -> "tuple[int, int]":
    """Calcula a ULA de 4 bits. Retorna (result & 0xF, carry). Espelha calcular() do ula_final_3.py."""
    x, y, carry = x & 0xF, y & 0xF, 0
    if op == 0:   res = x & y
    elif op == 1: res = x | y
    elif op == 2: res = (~y) & 0xF            # NOT Y (não X!)
    elif op == 3: res = x ^ y
    elif op == 4: s = x + y;  res = s; carry = 1 if s > 15 else 0
    elif op == 5: res = (x - y) if x >= y else 0   # SUB clampado, sem borrow
    elif op == 6: s = x * y;  res = s; carry = 1 if s > 15 else 0
    elif op == 7: res = x // y if y else 0
    else:         res = 0
    return res & 0xF, carry


def _normalize_op(op: "int | str") -> Optional[int]:
    """Converte op (int 0-7 ou nome 'ADD'/'add' etc.) em índice 0-7. None se inválido."""
    if isinstance(op, bool):
        return None
    if isinstance(op, int):
        return op if 0 <= op <= 7 else None
    if isinstance(op, str):
        s = op.strip()
        if s.isdigit():
            v = int(s)
            return v if 0 <= v <= 7 else None
        s = s.upper()
        if s in _ULA_OP_NAMES:
            return _ULA_OP_NAMES.index(s)
    return None

# Caminho do arquivo de controle (relativo ao diretório de trabalho atual)
FAKE_STATE_FILE = Path("fake_state.json")

SRAM_SIZE   = 2048
EEPROM_SIZE = 1024
FLASH_SIZE  = 32768
BLOCK_SIZE  = 64   # ↑ de 16 para 64: varredura completa da SRAM em ~16s (era ~64s), espelha o firmware
SRAM_BASE   = 0x0100   # endereço físico onde a SRAM de dados começa no ATmega328P


def _parse_addr(raw: Any) -> int:
    """Converte string hex '0x0110' ou inteiro em int."""
    if isinstance(raw, int):
        return raw
    try:
        return int(str(raw), 16)
    except (ValueError, TypeError):
        return 0


def _load_fake_state() -> Optional[Dict]:
    """
    Lê fake_state.json de forma segura.
    Retorna None se o arquivo não existir ou estiver temporariamente inválido.
    """
    try:
        return json.loads(FAKE_STATE_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, OSError):
        return None


def _flags_from_sreg(sreg: int) -> FlagsSnapshot:
    """Extrai as 8 flags do registrador SREG conforme datasheet ATmega328P."""
    return FlagsSnapshot(
        SREG=sreg,
        I=(sreg >> 7) & 1,
        T=(sreg >> 6) & 1,
        H=(sreg >> 5) & 1,
        S=(sreg >> 4) & 1,
        V=(sreg >> 3) & 1,
        N=(sreg >> 2) & 1,
        Z=(sreg >> 1) & 1,
        C=(sreg >> 0) & 1,
    )


class BaseClient(ABC):
    @abstractmethod
    def snapshots(self) -> Iterator[AVRSnapshot]:
        """Gera snapshots continuamente."""

    def send_ula_command(self, op: "int | str", x: int, y: int, timeout: float = 2.0) -> UlaAck:
        """
        Envia um comando de operação para a ULA e retorna o ACK (sucesso ou erro).

        Esta é a base da "API": substitui as chaves físicas D7-D10 como forma
        de operar a ULA — quem chama isso está fazendo, por software, o mesmo
        que fechar switches e apertar o botão no protoboard.

        Faz sua PRÓPRIA leitura da serial — não use isto enquanto outra
        thread/processo estiver lendo a mesma porta (ver `messages()` abaixo
        para o caso de uso com leitor em background, como o ArduinoService).
        """
        raise NotImplementedError

    def messages(self) -> Iterator[Dict]:
        """
        Itera mensagens cruas (dict, com o campo "type" original) da serial,
        sem filtrar snapshot/ack. Pensado para um único leitor em background
        (ex: `ArduinoService`) que precisa ver as duas categorias de linha na
        mesma timeline e rotear cada uma para o destino certo. Quem só quer
        o monitor contínuo deve usar `snapshots()`, não isto.
        """
        raise NotImplementedError

    def send_raw_command(self, cmd: Dict) -> None:
        """
        Escreve um comando (dict) na serial e retorna imediatamente, SEM
        esperar o ACK. A resposta chega mais tarde por `messages()` — quem
        chama isto precisa já ter um leitor consumindo `messages()` em
        paralelo, senão o ACK nunca é observado.
        """
        raise NotImplementedError

    def close(self) -> None:
        pass


class SerialClient(BaseClient):
    """Lê JSON Lines de uma porta serial real."""

    def __init__(self, port: str = "/dev/ttyACM0", baud: int = 115200, timeout: float = 2.0):
        import serial  # importação tardia para não falhar em modo fake

        self._port = port
        self._baud = baud
        self._timeout = timeout
        self._ser = serial.Serial(port, baud, timeout=timeout)

    def snapshots(self) -> Iterator[AVRSnapshot]:
        # Esta é a leitura CONTÍNUA do monitor (registradores, memória, ULA).
        # Linhas com "type":"ack" são respostas a comandos enviados por
        # send_ula_command() — não são snapshots, então são ignoradas aqui.
        # Quem precisa do ACK deve usar send_ula_command(), que lê sua própria
        # resposta diretamente da serial (ver docstring do método).
        while True:
            try:
                raw = self._ser.readline()
                if not raw:
                    continue
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                data = json.loads(line)
                if data.get("type") == "ack":
                    continue
                yield AVRSnapshot.model_validate(data)
            except json.JSONDecodeError:
                continue
            except Exception:
                continue

    def send_ula_command(self, op: "int | str", x: int, y: int, timeout: float = 2.0) -> UlaAck:
        """
        Escreve `{"cmd":"ula","op":...,"x":...,"y":...}\\n` na serial e
        aguarda a linha de resposta `{"type":"ack",...}` do Arduino.

        Importante: este método faz sua PRÓPRIA leitura da serial (chama
        `self._ser.readline()` diretamente). Não deve ser chamado ao mesmo
        tempo, na mesma thread, que um loop consumindo `snapshots()` — os
        dois disputariam os mesmos bytes da porta serial. Para uso isolado
        (ex: `ula_command.py`, ou uma chamada pontual antes/depois do loop
        de snapshots) isso é seguro. Linhas de snapshot que chegarem
        enquanto se espera o ACK são simplesmente descartadas aqui.
        """
        op_idx = _normalize_op(op)
        if op_idx is None:
            return UlaAck(ok=False, error="invalid_op")
        try:
            x_int, y_int = int(x), int(y)
        except (TypeError, ValueError):
            return UlaAck(ok=False, error="invalid_xy")
        if not (0 <= x_int <= 15 and 0 <= y_int <= 15):
            return UlaAck(ok=False, error="invalid_xy")

        cmd = json.dumps({"cmd": "ula", "op": op_idx, "x": x_int, "y": y_int})
        self._ser.write((cmd + "\n").encode("utf-8"))

        deadline = time.time() + timeout
        while time.time() < deadline:
            raw = self._ser.readline()   # respeita o timeout configurado no Serial()
            if not raw:
                continue
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if data.get("type") == "ack" and data.get("cmd") == "ula":
                return UlaAck.model_validate(data)
            # Linha de snapshot chegando enquanto esperamos o ACK: ignora e continua.
        return UlaAck(ok=False, error="timeout")

    def messages(self) -> Iterator[Dict]:
        # Única leitura contínua da porta para quem coordena snapshot + ack
        # na mesma thread (ArduinoService). Diferente de snapshots(), não
        # filtra nada — quem consome decide o que fazer com "type".
        while True:
            try:
                raw = self._ser.readline()
                if not raw:
                    continue
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                yield json.loads(line)
            except json.JSONDecodeError:
                continue
            except Exception:
                continue

    def send_raw_command(self, cmd: Dict) -> None:
        self._ser.write((json.dumps(cmd) + "\n").encode("utf-8"))

    def close(self) -> None:
        if self._ser and self._ser.is_open:
            self._ser.close()


class FakeSerialClient(BaseClient):
    """
    Gera snapshots simulados espelhando o novo modelo de estado compartilhado.

    O fake mantém o mesmo estado que o firmware real manteria:
    has_op/x/y, focus_field, estado (EDITING/RESULT), state_version.
    Comandos via send_raw_command() são processados e entregues como ACKs
    via messages(), com o mesmo padrão assíncrono do SerialClient real.

    Quando nenhum campo foi setado, gera uma demo cíclica para que o monitor
    mostre atividade mesmo sem Arduino físico.
    """

    # Constantes de foco (espelham o firmware)
    _FOCUS_OP, _FOCUS_X, _FOCUS_Y = 0, 1, 2
    _FOCUS_NAMES = {0: "OP", 1: "X", 2: "Y"}

    def __init__(self, interval: float = 0.5):
        self._interval = interval
        self._t0 = time.time()
        self._counter = 0

        self._sram_offset   = 0
        self._eeprom_offset = 0
        self._flash_offset  = 0

        self._sram   = bytearray(SRAM_SIZE)
        self._eeprom = bytearray([0xFF] * EEPROM_SIZE)
        self._flash  = bytearray(FLASH_SIZE)

        self._flash[0] = 0xDE
        self._flash[1] = 0xAD
        self._flash[2] = 0xBE
        self._flash[3] = 0xEF

        # ── Estado ULA (espelha variáveis do firmware) ────────────────────────
        self._ula_op:      int = 0
        self._ula_x:       int = 0
        self._ula_y:       int = 0
        self._ula_result:  int = 0
        self._ula_carry:   int = 0
        self._ula_estado:  int = 0   # 0=EDITING  4=RESULT
        self._ula_has_op:  bool = False
        self._ula_has_x:   bool = False
        self._ula_has_y:   bool = False
        self._ula_focus:   int = self._FOCUS_OP
        self._ula_source:  str = "hardware"
        self._ula_version: int = 0
        self._demo_mode:   bool = True   # true até o primeiro comando chegar

        self._ack_queue: "queue.Queue[Dict]" = queue.Queue()

    # ── Helpers internos ──────────────────────────────────────────────────────

    def _apply_memory_writes(self, writes: List[Dict]) -> None:
        for w in writes:
            try:
                space = str(w.get("space", "")).lower()
                addr  = _parse_addr(w.get("address", 0))
                value = int(w.get("value", 0)) & 0xFF
                if space == "sram":
                    idx = addr - SRAM_BASE
                    if 0 <= idx < SRAM_SIZE:
                        self._sram[idx] = value
                elif space == "eeprom":
                    if 0 <= addr < EEPROM_SIZE:
                        self._eeprom[addr] = value
                elif space == "flash":
                    if 0 <= addr < FLASH_SIZE:
                        self._flash[addr] = value
            except (TypeError, ValueError):
                continue

    def _next_unset_focus(self) -> Optional[int]:
        """Retorna o próximo focus sem has_*, ou None se todos estiverem setados."""
        for i in range(1, 3):
            c = (self._ula_focus + i) % 3
            if c == self._FOCUS_OP and not self._ula_has_op: return c
            if c == self._FOCUS_X  and not self._ula_has_x:  return c
            if c == self._FOCUS_Y  and not self._ula_has_y:  return c
        return None  # todos setados

    def _do_compute(self) -> None:
        self._ula_result, self._ula_carry = _compute_ula(
            self._ula_x, self._ula_y, self._ula_op
        )
        self._ula_estado = 4  # RESULT

    def _do_reset(self) -> None:
        self._ula_op = 0; self._ula_x = 0; self._ula_y = 0
        self._ula_result = 0; self._ula_carry = 0
        self._ula_has_op = False; self._ula_has_x = False; self._ula_has_y = False
        self._ula_focus = self._FOCUS_OP
        self._ula_estado = 0
        self._ula_source = "hardware"
        self._ula_version += 1
        self._demo_mode = True

    def _maybe_advance_focus(self, set_field_idx: int) -> None:
        """Se o campo que foi setado é o focus atual, avança para o próximo unset."""
        if self._ula_focus != set_field_idx:
            return
        nxt = self._next_unset_focus()
        if nxt is not None:
            self._ula_focus = nxt

    # ── Processamento de comandos ─────────────────────────────────────────────

    def _process_command(self, cmd: Dict) -> Dict:
        """Despacha o comando e retorna o dict de ACK correspondente."""
        cmd_name = str(cmd.get("cmd", "")).lower()

        if cmd_name == "set_field":
            return self._cmd_set_field(cmd)
        if cmd_name == "focus":
            return self._cmd_focus(cmd)
        if cmd_name == "compute_current":
            return self._cmd_compute_current()
        if cmd_name == "reset":
            return self._cmd_reset()
        if cmd_name == "ula":
            return self._cmd_ula_compat(cmd)
        return {"type": "ack", "cmd": cmd_name, "ok": False, "error": "unknown_cmd"}

    def _cmd_set_field(self, cmd: Dict) -> Dict:
        field = str(cmd.get("field", "")).lower()
        value = cmd.get("value")
        self._demo_mode = False
        self._ula_source = "api"

        if field == "op":
            op_idx = _normalize_op(value)
            if op_idx is None:
                return {"type": "ack", "cmd": "set_field", "ok": False, "error": "invalid_op"}
            self._ula_op = op_idx
            self._ula_has_op = True
            self._ula_version += 1
            self._maybe_advance_focus(self._FOCUS_OP)
            return {"type": "ack", "cmd": "set_field", "ok": True, "field": "op", "value": op_idx}

        if field in ("x", "y"):
            try:
                v = int(value)
            except (TypeError, ValueError):
                return {"type": "ack", "cmd": "set_field", "ok": False, "error": "invalid_xy"}
            if not (0 <= v <= 15):
                return {"type": "ack", "cmd": "set_field", "ok": False, "error": "invalid_xy"}
            if field == "x":
                self._ula_x = v; self._ula_has_x = True
                self._ula_version += 1
                self._maybe_advance_focus(self._FOCUS_X)
                return {"type": "ack", "cmd": "set_field", "ok": True, "field": "x", "value": v}
            else:
                self._ula_y = v; self._ula_has_y = True
                self._ula_version += 1
                self._maybe_advance_focus(self._FOCUS_Y)
                return {"type": "ack", "cmd": "set_field", "ok": True, "field": "y", "value": v}

        return {"type": "ack", "cmd": "set_field", "ok": False, "error": "unknown_field"}

    def _cmd_focus(self, cmd: Dict) -> Dict:
        field = str(cmd.get("field", "")).lower()
        mapping = {"op": self._FOCUS_OP, "x": self._FOCUS_X, "y": self._FOCUS_Y}
        if field not in mapping:
            return {"type": "ack", "cmd": "focus", "ok": False, "error": "unknown_field"}
        self._ula_focus = mapping[field]
        self._demo_mode = False
        return {"type": "ack", "cmd": "focus", "ok": True, "field": field}

    def _cmd_compute_current(self) -> Dict:
        eff_op = self._ula_has_op or (self._ula_focus == self._FOCUS_OP and self._ula_estado == 0)
        eff_x  = self._ula_has_x  or (self._ula_focus == self._FOCUS_X  and self._ula_estado == 0)
        eff_y  = self._ula_has_y  or (self._ula_focus == self._FOCUS_Y  and self._ula_estado == 0)

        if eff_op and eff_x and eff_y:
            self._ula_has_op = self._ula_has_x = self._ula_has_y = True
            self._ula_source = "api"
            self._ula_version += 1
            self._demo_mode = False
            self._do_compute()
            return {
                "type": "ack", "cmd": "compute_current", "ok": True,
                "op": self._ula_op, "op_name": _ULA_OP_NAMES[self._ula_op],
                "x": self._ula_x, "y": self._ula_y,
                "result": self._ula_result, "carry": self._ula_carry,
            }

        missing = []
        if not eff_op: missing.append("op")
        if not eff_x:  missing.append("x")
        if not eff_y:  missing.append("y")
        return {
            "type": "ack", "cmd": "compute_current", "ok": False,
            "error": "missing_fields", "missing": missing,
        }

    def _cmd_reset(self) -> Dict:
        self._do_reset()
        return {"type": "ack", "cmd": "reset", "ok": True}

    def _cmd_ula_compat(self, cmd: Dict) -> Dict:
        op_idx = _normalize_op(cmd.get("op"))
        if op_idx is None:
            return {"type": "ack", "cmd": "ula", "ok": False, "error": "invalid_op"}
        try:
            x_int, y_int = int(cmd.get("x")), int(cmd.get("y"))
        except (TypeError, ValueError):
            return {"type": "ack", "cmd": "ula", "ok": False, "error": "invalid_xy"}
        if not (0 <= x_int <= 15 and 0 <= y_int <= 15):
            return {"type": "ack", "cmd": "ula", "ok": False, "error": "invalid_xy"}

        self._ula_op = op_idx; self._ula_x = x_int; self._ula_y = y_int
        self._ula_has_op = self._ula_has_x = self._ula_has_y = True
        self._ula_source = "api"
        self._ula_version += 1
        self._demo_mode = False
        self._do_compute()
        return {
            "type": "ack", "cmd": "ula", "ok": True,
            "op": op_idx, "op_name": _ULA_OP_NAMES[op_idx],
            "x": x_int, "y": y_int,
            "result": self._ula_result, "carry": self._ula_carry,
        }

    # ── Geração de snapshot ───────────────────────────────────────────────────

    def _next_snapshot(self) -> AVRSnapshot:
        ms = int((time.time() - self._t0) * 1000)
        t  = ms / 1000.0

        self._counter = (self._counter + 1) & 0xFF
        self._sram[0x10] = self._counter

        portb = 0x07; portc = 0x00; portd = 0x82
        pinb  = 0x07; pinc  = 0x00; pind  = 0x83
        ddrb  = 0x00; ddrc  = 0x00; ddrd  = 0x7C
        adc: Dict[str, int] = {
            "A0": int(512 + 511 * math.sin(t)),
            "A1": int(512 + 511 * math.cos(t * 1.3)),
            "A2": 0, "A3": 0, "A4": 0, "A5": 0,
        }
        sreg_val = 0x80

        # Demo cíclica quando nenhum campo foi setado por comando
        if self._demo_mode:
            self._ula_op = int(t * 0.15) % 8
            self._ula_x  = 7
            self._ula_y  = self._counter % 16
            self._ula_result, self._ula_carry = _compute_ula(
                self._ula_x, self._ula_y, self._ula_op
            )
            self._ula_estado = 4

        # fake_state.json pode sobrescrever portas, ADC, flags e memória.
        # A seção "ula" só é aplicada em demo_mode — quando comandos foram
        # enviados, o estado da ULA é mantido pelos próprios comandos.
        state = _load_fake_state()
        if state is not None:
            self._apply_memory_writes(state.get("memory_writes", []))
            p  = state.get("ports", {})
            portb = int(p.get("PORTB", portb)) & 0xFF
            portc = int(p.get("PORTC", portc)) & 0xFF
            portd = int(p.get("PORTD", portd)) & 0xFF
            pi = state.get("pins", {})
            pinb = int(pi.get("PINB", portb)) & 0xFF
            pinc = int(pi.get("PINC", portc)) & 0xFF
            pind = int(pi.get("PIND", portd)) & 0xFF
            d  = state.get("ddr", {})
            ddrb = int(d.get("DDRB", ddrb)) & 0xFF
            ddrc = int(d.get("DDRC", ddrc)) & 0xFF
            ddrd = int(d.get("DDRD", ddrd)) & 0xFF
            a  = state.get("adc", {})
            for ch in ("A0", "A1", "A2", "A3", "A4", "A5"):
                if ch in a:
                    adc[ch] = int(a[ch]) & 0x3FF
            f  = state.get("flags", {})
            if "SREG" in f:
                sreg_val = int(f["SREG"]) & 0xFF
            if self._demo_mode:
                u = state.get("ula", {})
                if u:
                    self._ula_estado = int(u.get("estado", self._ula_estado))
                    self._ula_op     = int(u.get("op",     self._ula_op)) % 8
                    self._ula_x      = int(u.get("x",      self._ula_x)) & 0xF
                    self._ula_y      = int(u.get("y",      self._ula_y)) & 0xF
                    self._ula_result, self._ula_carry = _compute_ula(
                        self._ula_x, self._ula_y, self._ula_op
                    )

        # Atualiza SRAM e LEDs
        self._sram[0x00] = self._ula_x
        self._sram[0x01] = self._ula_y
        self._sram[0x02] = self._ula_result
        self._sram[0x03] = self._ula_carry
        self._sram[0x04] = self._ula_op
        self._sram[0x05] = self._ula_estado
        led_bits = ((self._ula_result & 0xF) << 2) | (self._ula_carry << 6)
        portd = (portd & 0x83) | led_bits
        pind  = (pind  & 0x83) | led_bits

        sram_phys    = SRAM_BASE + self._sram_offset
        sram_block   = list(self._sram  [self._sram_offset   : self._sram_offset   + BLOCK_SIZE])
        eeprom_block = list(self._eeprom[self._eeprom_offset : self._eeprom_offset + BLOCK_SIZE])
        flash_block  = list(self._flash [self._flash_offset  : self._flash_offset  + BLOCK_SIZE])

        snap = AVRSnapshot(
            timestamp_ms=ms,
            ports=PortsSnapshot(PORTB=portb, PORTC=portc, PORTD=portd),
            pins =PinsSnapshot (PINB=pinb,   PINC=pinc,   PIND=pind),
            ddr  =DDRSnapshot  (DDRB=ddrb,   DDRC=ddrc,   DDRD=ddrd),
            timers=TimersSnapshot(
                TCNT0=int(ms % 256),
                TCNT1=int(abs(32767 * (t % 1.0))),
                TCNT2=int(ms % 256),
            ),
            adc=ADCSnapshot(**adc),
            flags=_flags_from_sreg(sreg_val),
            memory=MemorySnapshot(
                sram=MemoryBlock(start=f"0x{sram_phys:04X}",           bytes=sram_block),
                eeprom=MemoryBlock(start=f"0x{self._eeprom_offset:04X}", bytes=eeprom_block),
                flash=MemoryBlock(start=f"0x{self._flash_offset:04X}",  bytes=flash_block),
            ),
            ula=ULASnapshot(
                estado=self._ula_estado,
                estado_name="RESULT" if self._ula_estado == 4 else "EDITING",
                op=self._ula_op,
                op_name=_ULA_OP_NAMES[self._ula_op],
                op_code=_ULA_OP_CODES[self._ula_op],
                x=self._ula_x,
                y=self._ula_y,
                result=self._ula_result,
                carry=self._ula_carry,
                has_op=self._ula_has_op,
                has_x=self._ula_has_x,
                has_y=self._ula_has_y,
                focus_field=self._ula_focus,
                focus_field_name=self._FOCUS_NAMES[self._ula_focus],
                last_input_source=self._ula_source,
                state_version=self._ula_version,
                addr_estado="0x0105",
                addr_x="0x0100",
                addr_y="0x0101",
                addr_result="0x0102",
                addr_carry="0x0103",
                addr_op="0x0104",
            ),
        )

        self._sram_offset   = (self._sram_offset   + BLOCK_SIZE) % SRAM_SIZE
        self._eeprom_offset = (self._eeprom_offset + BLOCK_SIZE) % EEPROM_SIZE
        self._flash_offset  = (self._flash_offset  + BLOCK_SIZE) % FLASH_SIZE
        return snap

    # ── Interface pública ─────────────────────────────────────────────────────

    def snapshots(self) -> Iterator[AVRSnapshot]:
        while True:
            yield self._next_snapshot()
            time.sleep(self._interval)

    def send_ula_command(self, op: "int | str", x: int, y: int, timeout: float = 2.0) -> UlaAck:
        ack_dict = self._cmd_ula_compat({"cmd": "ula", "op": op, "x": x, "y": y})
        return UlaAck.model_validate(ack_dict)

    def messages(self) -> Iterator[Dict]:
        while True:
            try:
                yield self._ack_queue.get_nowait()
                continue
            except queue.Empty:
                pass
            snap_dict = self._next_snapshot().model_dump(mode="json")
            snap_dict["type"] = "snapshot"
            yield snap_dict
            time.sleep(self._interval)

    def send_raw_command(self, cmd: Dict) -> None:
        self._ack_queue.put(self._process_command(cmd))


def make_client(
    fake: bool = False,
    port: str = "/dev/ttyACM0",
    baud: int = 115200,
    interval: float = 0.5,
) -> BaseClient:
    if fake:
        return FakeSerialClient(interval=interval)
    return SerialClient(port=port, baud=baud)
