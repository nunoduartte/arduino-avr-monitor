from __future__ import annotations
from typing import List, Optional
from pydantic import BaseModel, Field


class ULASnapshot(BaseModel):
    # Resultado / operandos
    estado: int = 0           # 0=EDITING  4=RESULT
    estado_name: str = "EDITING"
    op: int = 0               # índice da operação (0-7)
    op_name: str = "AND"      # AND OR NOT XOR ADD SUB MUL DIV
    op_code: str = "000"      # código binário de 3 bits (string, ex: "100")
    x: int = 0                # operando A (4 bits)
    y: int = 0                # operando B (4 bits)
    result: int = 0           # resultado (4 bits)
    carry: int = 0            # carry/overflow (1 bit)

    # Estado compartilhado API ↔ hardware
    has_op: bool = False      # campo foi confirmado (API ou botão)
    has_x: bool = False
    has_y: bool = False
    focus_field: int = 0      # 0=OP  1=X  2=Y  (hardware edita este campo agora)
    focus_field_name: str = "OP"
    last_input_source: str = "hardware"   # "hardware" | "api"
    state_version: int = 0    # incrementa a cada mudança de campo

    # Endereços de memória (debug)
    addr_estado: str = "0x0000"
    addr_x: str = "0x0000"
    addr_y: str = "0x0000"
    addr_result: str = "0x0000"
    addr_carry: str = "0x0000"
    addr_op: str = "0x0000"


class UlaAck(BaseModel):
    """
    Resposta genérica a qualquer comando enviado pela serial.

    `cmd` indica qual comando gerou este ACK ("ula", "set_field", "focus",
    "compute_current", "reset"). Campos opcionais são preenchidos conforme
    o tipo de resposta.
    """
    type: str = "ack"
    cmd: str = "ula"
    ok: bool = False
    # campos de resultado (compute / ula)
    op: Optional[int] = None
    op_name: Optional[str] = None
    x: Optional[int] = None
    y: Optional[int] = None
    result: Optional[int] = None
    carry: Optional[int] = None
    # campos de set_field / focus
    field: Optional[str] = None
    value: Optional[int] = None
    # estado completo incluído nos ACKs de set_field e compute_current
    has_op: Optional[bool] = None
    has_x: Optional[bool] = None
    has_y: Optional[bool] = None
    focus_field_name: Optional[str] = None
    state_version: Optional[int] = None
    # erro
    error: Optional[str] = None
    missing: Optional[List[str]] = None


class PortsSnapshot(BaseModel):
    PORTB: int = 0
    PORTC: int = 0
    PORTD: int = 0


class PinsSnapshot(BaseModel):
    PINB: int = 0
    PINC: int = 0
    PIND: int = 0


class DDRSnapshot(BaseModel):
    DDRB: int = 0
    DDRC: int = 0
    DDRD: int = 0


class TimersSnapshot(BaseModel):
    TCNT0: int = 0
    TCNT1: int = 0
    TCNT2: int = 0


class ADCSnapshot(BaseModel):
    A0: int = 0
    A1: int = 0
    A2: int = 0
    A3: int = 0
    A4: int = 0
    A5: int = 0


class FlagsSnapshot(BaseModel):
    SREG: int = 0
    I: int = 0
    T: int = 0
    H: int = 0
    S: int = 0
    V: int = 0
    N: int = 0
    Z: int = 0
    C: int = 0


class MemoryBlock(BaseModel):
    start: str = "0x0000"
    bytes: List[int] = Field(default_factory=list)


class MemorySnapshot(BaseModel):
    sram: MemoryBlock = Field(default_factory=MemoryBlock)
    eeprom: MemoryBlock = Field(default_factory=MemoryBlock)
    flash: MemoryBlock = Field(default_factory=MemoryBlock)


class AVRSnapshot(BaseModel):
    timestamp_ms: int = 0
    ports: PortsSnapshot = Field(default_factory=PortsSnapshot)
    pins: PinsSnapshot = Field(default_factory=PinsSnapshot)
    ddr: DDRSnapshot = Field(default_factory=DDRSnapshot)
    timers: TimersSnapshot = Field(default_factory=TimersSnapshot)
    adc: ADCSnapshot = Field(default_factory=ADCSnapshot)
    flags: FlagsSnapshot = Field(default_factory=FlagsSnapshot)
    memory: MemorySnapshot = Field(default_factory=MemorySnapshot)
    ula: Optional[ULASnapshot] = None   # presente apenas no firmware ULA
