from __future__ import annotations

import json
import math
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

# Caminho do arquivo de controle (relativo ao diretório de trabalho atual)
FAKE_STATE_FILE = Path("fake_state.json")

SRAM_SIZE   = 2048
EEPROM_SIZE = 1024
FLASH_SIZE  = 32768
BLOCK_SIZE  = 16
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
        while True:
            try:
                raw = self._ser.readline()
                if not raw:
                    continue
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                data = json.loads(line)
                yield AVRSnapshot.model_validate(data)
            except json.JSONDecodeError:
                continue
            except Exception:
                continue

    def close(self) -> None:
        if self._ser and self._ser.is_open:
            self._ser.close()


class FakeSerialClient(BaseClient):
    """
    Gera snapshots simulados com memória fake real.

    Características:
    - SRAM  (2048 bytes): inicializada com zeros + marcadores conhecidos.
    - EEPROM (1024 bytes): inicializada com 0xFF (estado de fábrica).
    - FLASH (32768 bytes): primeiros 4 bytes = DE AD BE EF; resto zeros.
    - Lê fake_state.json a cada snapshot para sobrescrever valores e
      escrever bytes arbitrários nas arrays de memória.
    - Dump rotativo de 16 bytes por frame cobre toda a memória ao longo do tempo.
    """

    def __init__(self, interval: float = 0.5):
        self._interval = interval
        self._t0 = time.time()
        self._counter = 0

        # Índices de dump rotativo (relativos ao início de cada array)
        self._sram_offset   = 0
        self._eeprom_offset = 0
        self._flash_offset  = 0

        # ── Arrays de memória simulada ────────────────────────────────────────
        self._sram   = bytearray(SRAM_SIZE)
        self._eeprom = bytearray([0xFF] * EEPROM_SIZE)
        self._flash  = bytearray(FLASH_SIZE)

        # Layout SRAM simulado (espelha o firmware ULA):
        #   índice 0x00-0x05 (física 0x0100-0x0105) = variáveis ULA: x,y,result,carry,op,estado
        #   índice 0x10      (física 0x0110)         = contador variável
        # (bytearray já inicializado com zeros; atualizado em _next_snapshot)

        # Assinatura conhecida na FLASH
        self._flash[0] = 0xDE
        self._flash[1] = 0xAD
        self._flash[2] = 0xBE
        self._flash[3] = 0xEF

    # ── Métodos internos ──────────────────────────────────────────────────────

    def _apply_memory_writes(self, writes: List[Dict]) -> None:
        """Aplica memory_writes do fake_state.json nas arrays internas."""
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

    def _next_snapshot(self) -> AVRSnapshot:
        ms = int((time.time() - self._t0) * 1000)
        t  = ms / 1000.0

        # Contador variável no índice 0x10 da SRAM (endereço físico 0x0110)
        self._counter = (self._counter + 1) & 0xFF
        self._sram[0x10] = self._counter

        # ── Valores padrão simulados ──────────────────────────────────────────
        portb = 0x20          # pino 13 (LED interno) em saída, igual ao padrão do Arduino
        portc = 0x00
        portd = 0x00
        pinb, pinc, pind = portb, portc, portd
        ddrb  = 0x7E          # D2-D6 (LEDs) + D13 como saída
        ddrc  = 0x00
        ddrd  = 0x7C          # D2-D6 como saída
        adc: Dict[str, int] = {
            "A0": int(512 + 511 * math.sin(t)),
            "A1": int(512 + 511 * math.cos(t * 1.3)),
            "A2": 0,
            "A3": 0,
            "A4": 0,
            "A5": 0,
        }
        sreg_val = 0x80   # I=1, demais=0

        # ── ULA: estado simulado cycling ──────────────────────────────────────
        ula_op     = int(t * 0.15) % 8
        ula_x      = 7
        ula_y      = self._counter % 16
        ula_estado = 4   # sempre mostrando resultado em modo fake

        # ── Carrega e aplica fake_state.json ──────────────────────────────────
        state = _load_fake_state()
        if state is not None:
            self._apply_memory_writes(state.get("memory_writes", []))

            p = state.get("ports", {})
            portb = int(p.get("PORTB", portb)) & 0xFF
            portc = int(p.get("PORTC", portc)) & 0xFF
            portd = int(p.get("PORTD", portd)) & 0xFF

            pi = state.get("pins", {})
            pinb = int(pi.get("PINB", portb)) & 0xFF
            pinc = int(pi.get("PINC", portc)) & 0xFF
            pind = int(pi.get("PIND", portd)) & 0xFF

            d = state.get("ddr", {})
            ddrb = int(d.get("DDRB", ddrb)) & 0xFF
            ddrc = int(d.get("DDRC", ddrc)) & 0xFF
            ddrd = int(d.get("DDRD", ddrd)) & 0xFF

            a = state.get("adc", {})
            for ch in ("A0", "A1", "A2", "A3", "A4", "A5"):
                if ch in a:
                    adc[ch] = int(a[ch]) & 0x3FF

            f = state.get("flags", {})
            if "SREG" in f:
                sreg_val = int(f["SREG"]) & 0xFF

            u = state.get("ula", {})
            if u:
                ula_estado = int(u.get("estado", ula_estado))
                ula_op     = int(u.get("op",     ula_op)) % 8
                ula_x      = int(u.get("x",      ula_x)) & 0xF
                ula_y      = int(u.get("y",      ula_y)) & 0xF

        # ── Calcula resultado da ULA ──────────────────────────────────────────
        ula_result, ula_carry = _compute_ula(ula_x, ula_y, ula_op)

        # Atualiza SRAM fake com as variáveis da ULA (espelha layout do firmware)
        self._sram[0x00] = ula_x
        self._sram[0x01] = ula_y
        self._sram[0x02] = ula_result
        self._sram[0x03] = ula_carry
        self._sram[0x04] = ula_op
        self._sram[0x05] = ula_estado

        # Atualiza PORTD com estado dos LEDs (D2-D5 = resultado, D6 = carry)
        led_bits = ((ula_result & 0xF) << 2) | (ula_carry << 6)
        portd = (portd & 0x83) | led_bits   # preserva bits 0,1,7

        # ── Lê blocos reais das arrays ────────────────────────────────────────
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
                sram=MemoryBlock(
                    start=f"0x{sram_phys:04X}",
                    bytes=sram_block,
                ),
                eeprom=MemoryBlock(
                    start=f"0x{self._eeprom_offset:04X}",
                    bytes=eeprom_block,
                ),
                flash=MemoryBlock(
                    start=f"0x{self._flash_offset:04X}",
                    bytes=flash_block,
                ),
            ),
            ula=ULASnapshot(
                estado=ula_estado,
                op=ula_op,
                op_name=_ULA_OP_NAMES[ula_op],
                op_code=_ULA_OP_CODES[ula_op],
                x=ula_x,
                y=ula_y,
                result=ula_result,
                carry=ula_carry,
                addr_estado="0x0105",
                addr_x="0x0100",
                addr_y="0x0101",
                addr_result="0x0102",
                addr_carry="0x0103",
            ),
        )

        # Avança offsets rotativos para o próximo frame
        self._sram_offset   = (self._sram_offset   + BLOCK_SIZE) % SRAM_SIZE
        self._eeprom_offset = (self._eeprom_offset + BLOCK_SIZE) % EEPROM_SIZE
        self._flash_offset  = (self._flash_offset  + BLOCK_SIZE) % FLASH_SIZE

        return snap

    def snapshots(self) -> Iterator[AVRSnapshot]:
        while True:
            yield self._next_snapshot()
            time.sleep(self._interval)


def make_client(
    fake: bool = False,
    port: str = "/dev/ttyACM0",
    baud: int = 115200,
    interval: float = 0.5,
) -> BaseClient:
    if fake:
        return FakeSerialClient(interval=interval)
    return SerialClient(port=port, baud=baud)
