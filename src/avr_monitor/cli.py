from __future__ import annotations

import time

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
)
from .models import AVRSnapshot
from .serial_client import make_client

console = Console()


def _bit_bar(value: int, width: int = 8) -> Text:
    bits = to_bits(value, width)
    text = Text()
    for b in bits:
        if b == "1":
            text.append("█", style="bold green")
        else:
            text.append("░", style="dim")
    return text


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
        "A0": snap.adc.A0,
        "A1": snap.adc.A1,
        "A2": snap.adc.A2,
        "A3": snap.adc.A3,
        "A4": snap.adc.A4,
        "A5": snap.adc.A5,
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
        "I": snap.flags.I,
        "T": snap.flags.T,
        "H": snap.flags.H,
        "S": snap.flags.S,
        "V": snap.flags.V,
        "N": snap.flags.N,
        "Z": snap.flags.Z,
        "C": snap.flags.C,
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
    sram_addr = parse_hex_addr(snap.memory.sram.start)
    eeprom_addr = parse_hex_addr(snap.memory.eeprom.start)
    flash_addr = parse_hex_addr(snap.memory.flash.start)

    sram_dump = format_hex_dump(sram_addr, snap.memory.sram.bytes)
    eeprom_dump = format_hex_dump(eeprom_addr, snap.memory.eeprom.bytes)
    flash_dump = format_hex_dump(flash_addr, snap.memory.flash.bytes)

    content = (
        "[bold yellow]SRAM[/bold yellow]\n"
        f"[dim]{sram_dump}[/dim]\n\n"
        "[bold yellow]EEPROM[/bold yellow]\n"
        f"[dim]{eeprom_dump}[/dim]\n\n"
        "[bold yellow]FLASH[/bold yellow]\n"
        f"[dim]{flash_dump}[/dim]"
    )
    return Panel(content, title="Dump de Memória", border_style="blue")


def build_layout(snap: AVRSnapshot, fake: bool) -> Table:
    root = Table.grid(padding=1)
    root.add_column()

    mode_tag = "[bold red][SIMULADO][/bold red]" if fake else "[bold green][SERIAL][/bold green]"
    header = Panel(
        f"{mode_tag}  t={snap.timestamp_ms} ms",
        title="[bold]AVR Monitor — ATmega328P[/bold]",
        border_style="bright_blue",
    )
    root.add_row(header)
    root.add_row(Columns([_reg_table(snap), _timer_table(snap)]))
    root.add_row(_adc_table(snap))
    root.add_row(_sreg_table(snap))
    root.add_row(_mem_panel(snap))
    return root


@click.command()
@click.option("--port", default="/dev/ttyACM0", show_default=True, help="Porta serial.")
@click.option("--baud", default=115200, show_default=True, help="Baud rate.")
@click.option("--fake", is_flag=True, default=False, help="Modo simulado (sem Arduino).")
@click.option("--interval", default=0.5, show_default=True, help="Intervalo de atualização (s).")
def main(port: str, baud: int, fake: bool, interval: float) -> None:
    """Monitor de registradores AVR do Arduino Uno."""
    client = make_client(fake=fake, port=port, baud=baud, interval=interval)

    try:
        with Live(console=console, refresh_per_second=4, screen=True) as live:
            for snap in client.snapshots():
                live.update(build_layout(snap, fake))
    except KeyboardInterrupt:
        pass
    finally:
        client.close()
        console.print("[dim]Encerrado.[/dim]")


if __name__ == "__main__":
    main()
