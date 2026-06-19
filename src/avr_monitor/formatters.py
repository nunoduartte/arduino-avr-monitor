from __future__ import annotations
from typing import TYPE_CHECKING, List, Optional

if TYPE_CHECKING:
    from .models import AVRSnapshot


def to_bits(value: int, width: int = 8) -> str:
    """Converte inteiro em string de bits com largura fixa."""
    return format(value & ((1 << width) - 1), f"0{width}b")


def register_repr(name: str, value: int, bit_names: List[str] | None = None) -> str:
    """
    Retorna string no formato:
      SREG = 0x82 = 10000010
             I T H S V N Z C
             1 0 0 0 0 0 1 0
    """
    bits = to_bits(value, 8)
    hex_str = f"0x{value:02X}"
    lines = [f"{name} = {hex_str} = {bits}"]
    if bit_names and len(bit_names) == 8:
        header = "  " + " ".join(f"{b:>1}" for b in bit_names)
        values = "  " + " ".join(b for b in bits)
        lines.append(header)
        lines.append(values)
    return "\n".join(lines)


def format_hex_dump(start_addr: int, data: List[int], row_width: int = 8) -> str:
    """Formata bloco de bytes estilo hex dump."""
    lines = []
    for i in range(0, len(data), row_width):
        chunk = data[i : i + row_width]
        addr = f"0x{(start_addr + i):04X}"
        hex_part = " ".join(f"{b:02X}" for b in chunk)
        ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"{addr}  {hex_part:<{row_width * 3}}  {ascii_part}")
    return "\n".join(lines)


def parse_hex_addr(addr_str: str) -> int:
    """Converte string '0x0100' em inteiro."""
    try:
        return int(addr_str, 16)
    except (ValueError, TypeError):
        return 0


SREG_BIT_NAMES = ["I", "T", "H", "S", "V", "N", "Z", "C"]


def ula_memory_check(snap: "AVRSnapshot") -> Optional[dict]:
    """
    Verifica se a janela atual do dump rotativo de SRAM cobre os endereços das
    variáveis da ULA (x, y, result, carry, op, estado) e, quando cobre, compara
    byte a byte o valor lido no dump com o valor reportado nos campos `ula.*`.

    Não interfere na varredura — só observa o que já está em `snap.memory.sram`
    e `snap.ula` e calcula a sobreposição. Retorna None se o snapshot não tiver
    seção ULA (ex: firmware avr_monitor_firmware original).
    """
    if snap.ula is None:
        return None

    ula = snap.ula
    win_start = parse_hex_addr(snap.memory.sram.start)
    win_bytes = snap.memory.sram.bytes
    win_end   = win_start + len(win_bytes) - 1

    fields = [
        ("x",      ula.addr_x,      ula.x),
        ("y",      ula.addr_y,      ula.y),
        ("result", ula.addr_result, ula.result),
        ("carry",  ula.addr_carry,  ula.carry),
        ("op",     ula.addr_op,     ula.op),
        ("estado", ula.addr_estado, ula.estado),
    ]

    rows = []
    any_in_window = False
    for name, addr_str, expected in fields:
        addr = parse_hex_addr(addr_str)
        in_window = win_start <= addr <= win_end
        actual = None
        match = None
        if in_window:
            any_in_window = True
            actual = win_bytes[addr - win_start]
            match = actual == expected
        rows.append({
            "name": name,
            "addr": addr,
            "in_window": in_window,
            "expected": expected,
            "actual": actual,
            "match": match,
        })

    return {
        "window_start": win_start,
        "window_end": win_end,
        "any_in_window": any_in_window,
        "fields": rows,
    }
