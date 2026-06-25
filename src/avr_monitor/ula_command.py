"""
ula_command.py — envia UM comando de operação para a ULA e imprime o resultado.

Diferente de `cli.py` (que abre um monitor contínuo em tela cheia), este
script é de "tiro único": conecta, envia o comando, espera o ACK, imprime
o resultado e sai. Pensado para testar o protocolo de comando/ACK isoladamente,
sem o ruído do monitor de registradores, e para ser chamado por outros
scripts/automação (código de saída 0 = sucesso, 1 = erro).

Exemplos:
    python -m src.avr_monitor.ula_command --port /dev/ttyACM0 --op ADD --x 7 --y 3
    python -m src.avr_monitor.ula_command --fake --op 6 --x 4 --y 5

Saída esperada (sucesso):
    ADD 7 3 = 10 carry=0
"""

from __future__ import annotations

import sys

import click

from .serial_client import make_client


@click.command()
@click.option("--port", default="/dev/ttyACM0", show_default=True, help="Porta serial.")
@click.option("--baud", default=115200, show_default=True, help="Baud rate.")
@click.option("--fake", is_flag=True, default=False, help="Modo simulado (sem Arduino).")
@click.option(
    "--op", required=True,
    help="Operação: AND, OR, NOT, XOR, ADD, SUB, MUL, DIV, ou índice 0-7.",
)
@click.option("--x", "x_val", required=True, type=int, help="Operando X (0-15).")
@click.option("--y", "y_val", required=True, type=int, help="Operando Y (0-15).")
@click.option("--timeout", default=2.0, show_default=True, help="Timeout aguardando ACK (segundos).")
def main(port: str, baud: int, fake: bool, op: str, x_val: int, y_val: int, timeout: float) -> None:
    """Envia um comando ULA pela serial (ou modo fake) e imprime o resultado."""
    client = make_client(fake=fake, port=port, baud=baud)
    try:
        ack = client.send_ula_command(op, x_val, y_val, timeout=timeout)
    finally:
        client.close()

    if not ack.ok:
        click.echo(f"Erro: {ack.error}", err=True)
        sys.exit(1)

    op_name = ack.op_name or str(ack.op)
    click.echo(f"{op_name} {ack.x} {ack.y} = {ack.result} carry={ack.carry}")


if __name__ == "__main__":
    main()
