from __future__ import annotations

import time
from typing import List

import click
from rich.columns import Columns
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .formatters import (
    SREG_BIT_NAMES,
    format_hex_dump,
    parse_hex_addr,
    to_bits,
    ula_memory_check,
)
from .models import AVRSnapshot, ULASnapshot
from .serial_client import make_client

console = Console()

# ── Constantes ULA ────────────────────────────────────────────────────────────

_ULA_OPS = [
    ("AND", "000"), ("OR",  "001"), ("NOT", "010"), ("XOR", "011"),
    ("ADD", "100"), ("SUB", "101"), ("MUL", "110"), ("DIV", "111"),
]

_ULA_OP_SYMS = {
    "AND": "&", "OR": "|", "NOT": "~Y", "XOR": "^",
    "ADD": "+", "SUB": "-", "MUL": "*", "DIV": "/",
}

_ESTADO_COLORS = {0: "cyan", 1: "yellow", 2: "yellow", 3: "orange3", 4: "green"}


# ── Helpers de bits ───────────────────────────────────────────────────────────

def _bit_bar(value: int, width: int = 8) -> Text:
    bits = to_bits(value, width)
    text = Text()
    for b in bits:
        if b == "1":
            text.append("█", style="bold green")
        else:
            text.append("░", style="dim")
    return text


def _switches_display(val4: int) -> str:
    """Mostra switches D10-D7 como ■/— com legenda de pinos."""
    bits = f"{val4:04b}"   # bit3=D10 … bit0=D7
    syms = "  ".join("■" if b == "1" else "—" for b in bits)
    return f"  D10  D9   D8   D7\n   {syms}"


# ── Tabelas dos registradores AVR ─────────────────────────────────────────────

def _reg_table(snap: AVRSnapshot) -> Table:
    t = Table(title="Registradores PORT / PIN / DDR", show_header=True, header_style="bold cyan")
    t.add_column("Reg", style="bold yellow", width=6)
    t.add_column("Hex", width=6)
    t.add_column("Dec", width=5)
    t.add_column("Bits  [7..0]", width=18)

    def row(name: str, val: int) -> None:
        t.add_row(name, f"0x{val:02X}", str(val), _bit_bar(val))

    row("PORTB", snap.ports.PORTB)
    row("PORTC", snap.ports.PORTC)
    row("PORTD", snap.ports.PORTD)
    t.add_section()
    row("PINB", snap.pins.PINB)
    row("PINC", snap.pins.PINC)
    row("PIND", snap.pins.PIND)
    t.add_section()
    row("DDRB", snap.ddr.DDRB)
    row("DDRC", snap.ddr.DDRC)
    row("DDRD", snap.ddr.DDRD)
    return t


def _timer_table(snap: AVRSnapshot) -> Table:
    t = Table(title="Timers", header_style="bold cyan")
    t.add_column("Timer", style="bold yellow", width=8)
    t.add_column("Valor", width=7)
    t.add_column("Bits", width=18)

    t.add_row("TCNT0", str(snap.timers.TCNT0), _bit_bar(snap.timers.TCNT0, 8))
    t.add_row("TCNT1", str(snap.timers.TCNT1), _bit_bar(snap.timers.TCNT1, 16))
    t.add_row("TCNT2", str(snap.timers.TCNT2), _bit_bar(snap.timers.TCNT2, 8))
    return t


def _adc_table(snap: AVRSnapshot) -> Table:
    t = Table(title="ADC (0–1023)", header_style="bold cyan")
    t.add_column("Canal", style="bold yellow", width=6)
    t.add_column("Valor", width=6)
    t.add_column("Tensão (5V ref)", width=14)
    t.add_column("Barra", width=20)

    adc_vals = {
        "A0": snap.adc.A0, "A1": snap.adc.A1, "A2": snap.adc.A2,
        "A3": snap.adc.A3, "A4": snap.adc.A4, "A5": snap.adc.A5,
    }
    for ch, val in adc_vals.items():
        volts = f"{val * 5.0 / 1023:.3f} V"
        bar_len = int(val * 20 / 1023)
        bar = Text("█" * bar_len + "░" * (20 - bar_len), style="green")
        t.add_row(ch, str(val), volts, bar)
    return t


def _sreg_table(snap: AVRSnapshot) -> Table:
    t = Table(title=f"SREG = 0x{snap.flags.SREG:02X}", header_style="bold cyan")
    flag_vals = {
        "I": snap.flags.I, "T": snap.flags.T, "H": snap.flags.H, "S": snap.flags.S,
        "V": snap.flags.V, "N": snap.flags.N, "Z": snap.flags.Z, "C": snap.flags.C,
    }
    for flag in flag_vals:
        t.add_column(flag, width=4, justify="center")

    t.add_row(
        *[
            Text("1", style="bold green") if v else Text("0", style="dim")
            for v in flag_vals.values()
        ]
    )
    return t


def _mem_panel(snap: AVRSnapshot) -> Panel:
    sram_addr  = parse_hex_addr(snap.memory.sram.start)
    eeprom_addr = parse_hex_addr(snap.memory.eeprom.start)
    flash_addr  = parse_hex_addr(snap.memory.flash.start)

    content = (
        "[bold yellow]SRAM[/bold yellow]\n"
        f"[dim]{format_hex_dump(sram_addr,   snap.memory.sram.bytes)}[/dim]\n\n"
        "[bold yellow]EEPROM[/bold yellow]\n"
        f"[dim]{format_hex_dump(eeprom_addr, snap.memory.eeprom.bytes)}[/dim]\n\n"
        "[bold yellow]FLASH[/bold yellow]\n"
        f"[dim]{format_hex_dump(flash_addr,  snap.memory.flash.bytes)}[/dim]"
    )
    return Panel(content, title="Dump de Memória", border_style="blue")


# ── Painel ULA ────────────────────────────────────────────────────────────────

def _ula_transition_msg(ula: ULASnapshot, prev_estado: int) -> str:
    """Gera uma mensagem legível para uma transição de estado."""
    estado = ula.estado
    sym = _ULA_OP_SYMS.get(ula.op_name, ula.op_name)

    if prev_estado == 0 and estado == 1:
        return f"✔  Operação confirmada: {ula.op_name}  (código {ula.op_code}b)"
    if prev_estado == 1 and estado == 2:
        return f"✔  X confirmado: {ula.x}  ({ula.x:04b}b)"
    if prev_estado == 2 and estado == 3:
        return f"✔  Y confirmado: {ula.y}  ({ula.y:04b}b)"
    if prev_estado == 3 and estado == 4:
        eq    = f"~{ula.y}" if ula.op_name == "NOT" else f"{ula.x} {sym} {ula.y}"
        carry = "  CARRY!" if ula.carry else ""
        return f"✔  Resultado: {eq} = {ula.result}{carry}"
    if prev_estado == 4 and estado == 0:
        return "↺  Reiniciando — nova operação"
    return f"Estado {prev_estado} → {estado}"


def _ula_panel(snap: AVRSnapshot, log: List[str]) -> Panel:
    ula   = snap.ula
    if ula is None:
        return Panel("sem dados", title="ULA")

    estado = ula.estado
    color  = _ESTADO_COLORS.get(estado, "white")
    sym    = _ULA_OP_SYMS.get(ula.op_name, ula.op_name)
    parts: List[str] = []

    # ── Cabeçalho do estado ───────────────────────────────────────────────────
    estado_labels = [
        "Selecionando operação",
        "Entrando X",
        "Entrando Y",
        "Aguardando cálculo",
        "Resultado",
    ]
    label = estado_labels[estado] if 0 <= estado < 5 else f"Estado {estado}"
    parts.append(f"[bold {color}]◆ Estado {estado} — {label}[/bold {color}]")
    parts.append("")

    # ── Conteúdo específico de cada estado ────────────────────────────────────
    if estado == 0:
        parts.append("Configure [bold]D9 D8 D7[/bold] com o código da operação e pressione [bold]D11[/bold]:")
        parts.append("")
        parts.append(f"  {'Op':<6}{'Cód':<7}D9   D8   D7")
        parts.append(f"  {'─'*6}{'─'*7}{'─'*5}{'─'*5}{'─'*5}")
        for i, (name, code) in enumerate(_ULA_OPS):
            d = ["■" if c == "1" else "—" for c in code]
            is_cur = (i == ula.op)
            arrow = "  ◄ atual" if is_cur else ""
            row = f"  {name:<6}{code:<7}{d[0]:<5}{d[1]:<5}{d[2]}{arrow}"
            if is_cur:
                parts.append(f"[bold green]{row}[/bold green]")
            else:
                parts.append(row)

    elif estado in (1, 2):
        val  = ula.x if estado == 1 else ula.y
        name = "X" if estado == 1 else "Y"
        parts.append(f"Configure [bold]D10 D9 D8 D7[/bold] com {name} (4 bits) e pressione [bold]D11[/bold]:")
        parts.append("")
        parts.append(f"[dim]{_switches_display(val)}[/dim]")
        parts.append("")
        parts.append(f"  {name} = [bold green]{val}[/bold green]  "
                     f"({val:04b}b  |  0x{val:X}  |  decimal {val})")

    elif estado == 3:
        parts.append(f"Y = [bold]{ula.y}[/bold] ({ula.y:04b}b) travado nos LEDs [bold]D2-D5[/bold].")
        parts.append("")
        parts.append("Pressione [bold]D11[/bold] para calcular o resultado.")

    elif estado == 4:
        eq = f"~{ula.y}" if ula.op_name == "NOT" else f"{ula.x} {sym} {ula.y}"
        carry_tag = "  [bold red]CARRY (D6 aceso)[/bold red]" if ula.carry else ""
        parts.append(f"  [bold green]{eq} = {ula.result}[/bold green]{carry_tag}")
        parts.append("")
        # LEDs individuais
        leds = []
        for bit in range(3, -1, -1):
            pin = bit + 2       # D2=bit0 … D5=bit3
            on  = (ula.result >> bit) & 1
            leds.append(f"D{pin}={'■' if on else '—'}")
        parts.append("  LEDs: " + "  ".join(leds) + f"  D6(carry)={'■' if ula.carry else '—'}")
        parts.append("")
        parts.append("[dim]Pressione D11 para reiniciar.[/dim]")

    # ── Histórico de transições ───────────────────────────────────────────────
    if log:
        parts.append("")
        parts.append("[dim]─── Histórico ─────────────────────────────────────────────[/dim]")
        for entry in log[-6:]:
            parts.append(f"[dim]  {entry}[/dim]")

    content = "\n".join(parts)
    return Panel(content, title="[bold]ULA 4 bits[/bold]", border_style=color)


def _ula_memcheck_panel(snap: AVRSnapshot) -> Panel:
    """Confere se a janela atual do dump de SRAM cobre os endereços da ULA."""
    check = ula_memory_check(snap)
    if check is None:
        return Panel("[dim]sem dados[/dim]", title="Verificação SRAM ↔ ULA")

    win = f"0x{check['window_start']:04X} – 0x{check['window_end']:04X}"

    if not check["any_in_window"]:
        body = (
            f"[dim]Janela atual do dump: {win}[/dim]\n\n"
            "[yellow]⏳ Nenhum endereço da ULA está na janela atual.[/yellow]\n"
            "Aguarde a varredura rotativa alcançar [bold]0x0100–0x0105[/bold]."
        )
        return Panel(body, title="[bold]Verificação SRAM ↔ ULA[/bold]", border_style="yellow")

    t = Table(show_header=True, header_style="bold cyan", box=None, padding=(0, 1))
    t.add_column("Variável", width=8)
    t.add_column("Endereço", width=9)
    t.add_column("Campo ula.*", width=12, justify="right")
    t.add_column("Lido na SRAM", width=13, justify="right")
    t.add_column("Status", width=10)

    all_ok = True
    for f in check["fields"]:
        if f["in_window"]:
            ok = f["match"]
            all_ok = all_ok and ok
            status = Text("✔ OK", style="bold green") if ok else Text("✘ DIVERGE", style="bold red")
            lido = str(f["actual"])
        else:
            status = Text("fora da janela", style="dim")
            lido = "—"
        t.add_row(f["name"], f"0x{f['addr']:04X}", str(f["expected"]), lido, status)

    color = "green" if all_ok else "red"
    panel = Panel(t, title="[bold]Verificação SRAM ↔ ULA[/bold]", border_style=color,
                  subtitle=f"[dim]janela: {win}[/dim]")
    return panel


# ── Layouts ───────────────────────────────────────────────────────────────────

def _header_panel(snap: AVRSnapshot, fake: bool) -> Panel:
    mode_tag = "[bold red][SIMULADO][/bold red]" if fake else "[bold green][SERIAL][/bold green]"
    return Panel(
        f"{mode_tag}  t={snap.timestamp_ms} ms",
        title="[bold]AVR Monitor — ATmega328P[/bold]",
        border_style="bright_blue",
    )


def build_layout(snap: AVRSnapshot, fake: bool, ula_log: List[str] | None = None) -> Table:
    """Layout completo: ULA (se presente) + todas as tabelas de registradores."""
    root = Table.grid(padding=1)
    root.add_column()
    root.add_row(_header_panel(snap, fake))
    if snap.ula is not None:
        root.add_row(_ula_panel(snap, ula_log or []))
        root.add_row(_ula_memcheck_panel(snap))
    root.add_row(Columns([_reg_table(snap), _timer_table(snap)]))
    root.add_row(_adc_table(snap))
    root.add_row(_sreg_table(snap))
    root.add_row(_mem_panel(snap))
    return root


def build_ula_only(snap: AVRSnapshot, fake: bool, ula_log: List[str] | None = None) -> Table:
    """Layout reduzido: só o painel da ULA (sem registradores, ADC, memória)."""
    root = Table.grid(padding=1)
    root.add_column()
    root.add_row(_header_panel(snap, fake))
    if snap.ula is not None:
        root.add_row(_ula_panel(snap, ula_log or []))
        root.add_row(_ula_memcheck_panel(snap))
    else:
        root.add_row(Panel(
            "[dim]Aguardando dados da ULA...\n"
            "Certifique-se de que o firmware ula_avr_monitor_firmware está carregado.[/dim]",
            title="[bold]ULA 4 bits[/bold]",
            border_style="dim",
        ))
    return root


# ── Comando principal ─────────────────────────────────────────────────────────

@click.command()
@click.option("--port",      default="/dev/ttyACM0", show_default=True, help="Porta serial.")
@click.option("--baud",      default=115200,          show_default=True, help="Baud rate.")
@click.option("--fake",      is_flag=True, default=False,                help="Modo simulado (sem Arduino).")
@click.option("--interval",  default=0.5,             show_default=True, help="Intervalo de atualização (s).")
@click.option("--ula-only",  is_flag=True, default=False,
              help="Mostra apenas o painel ULA (sem registradores/ADC/memória).")
def main(port: str, baud: int, fake: bool, interval: float, ula_only: bool) -> None:
    """Monitor de registradores AVR do Arduino Uno."""
    if not fake:
        console.print(f"[dim]Conectando à porta {port} @ {baud} baud...[/dim]")
    try:
        client = make_client(fake=fake, port=port, baud=baud, interval=interval)
    except Exception as exc:
        console.print(f"[bold red]Erro ao abrir a porta {port}:[/bold red] {exc}")
        console.print(
            "[dim]Causas comuns: porta errada, Arduino IDE/Monitor Serial ainda "
            "aberto na mesma porta, ou permissão (grupo dialout).[/dim]"
        )
        raise SystemExit(1)

    if not fake:
        console.print("[dim]Conectado. Aguardando primeiro snapshot...[/dim]")

    ula_log: List[str] = []
    prev_estado: int   = -1   # -1 = ainda não recebeu nenhum frame

    try:
        with Live(console=console, refresh_per_second=4, screen=True) as live:
            for snap in client.snapshots():
                # Detecta transição de estado e registra no histórico
                if snap.ula is not None:
                    estado = snap.ula.estado
                    if prev_estado >= 0 and estado != prev_estado:
                        msg = _ula_transition_msg(snap.ula, prev_estado)
                        ula_log.append(f"[t={snap.timestamp_ms}ms]  {msg}")
                        if len(ula_log) > 8:
                            ula_log.pop(0)
                    prev_estado = estado

                if ula_only:
                    live.update(build_ula_only(snap, fake, ula_log))
                else:
                    live.update(build_layout(snap, fake, ula_log))
    except KeyboardInterrupt:
        pass
    finally:
        client.close()
        console.print("[dim]Encerrado.[/dim]")


if __name__ == "__main__":
    main()
