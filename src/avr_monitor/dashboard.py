"""
Dashboard Streamlit para o AVR Monitor.

Execução (a partir da raiz do projeto):
    streamlit run src/avr_monitor/dashboard.py
"""

from __future__ import annotations

import queue
import sys
import threading
import time
from pathlib import Path
from typing import Optional

# Garante que a raiz do projeto esteja em sys.path quando o Streamlit
# executa este arquivo diretamente (sem contexto de pacote).
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pandas as pd
import streamlit as st

from src.avr_monitor.formatters import format_hex_dump, parse_hex_addr, to_bits
from src.avr_monitor.models import AVRSnapshot
from src.avr_monitor.serial_client import (
    FAKE_STATE_FILE,
    FakeSerialClient,
    _load_fake_state,
    make_client,
)

# ── Modo FAKE: sem thread ─────────────────────────────────────────────────────
#
# FakeSerialClient e seu generator são guardados em st.session_state.
# A cada rerun chamamos next(iter) de forma síncrona — simples, sem globals,
# sem risco de reset por hot-reload. fake_state.json é relido dentro de
# _next_snapshot() a cada chamada, então mudanças aparecem no próximo rerun.


def _fake_init() -> None:
    """Cria um novo FakeSerialClient e seu generator no session_state."""
    client = FakeSerialClient(interval=0)   # interval=0 → next() retorna imediatamente
    st.session_state["fake_client"] = client
    st.session_state["fake_iter"]   = client.snapshots()


def _fake_next() -> AVRSnapshot:
    """Retorna o próximo snapshot fake, criando o cliente se necessário."""
    if "fake_iter" not in st.session_state:
        _fake_init()
    return next(st.session_state["fake_iter"])


# ── Modo REAL: thread + queue ─────────────────────────────────────────────────
#
# A thread lê snapshots da serial e os coloca numa queue.Queue.
# A cada rerun, drenamos a queue e atualizamos session_state["latest_snap"].
# Thread e queue vivem em session_state — não dependem de globals de módulo.


def _real_thread_fn(port: str, baud: int, q: queue.Queue, stop: threading.Event) -> None:
    """Função da thread: lê snapshots e enfileira. Erros viram Exception na queue."""
    try:
        client = make_client(fake=False, port=port, baud=baud)
        try:
            for snap in client.snapshots():
                if stop.is_set():
                    break
                q.put(snap)         # queue ilimitada → nunca bloqueia a thread
        finally:
            client.close()
    except Exception as exc:        # porta não encontrada, permissão negada etc.
        q.put(exc)                  # sentinela de erro para o UI detectar


def _real_start(port: str, baud: int) -> None:
    """Para thread anterior (se houver) e inicia uma nova."""
    _real_stop()
    q    = queue.Queue()
    stop = threading.Event()
    t    = threading.Thread(target=_real_thread_fn, args=(port, baud, q, stop), daemon=True)
    t.start()
    st.session_state.update({
        "serial_queue":  q,
        "serial_stop":   stop,
        "serial_thread": t,
        "connected":     True,
        "serial_error":  None,
        "latest_snap":   None,      # descarta dados da conexão anterior
    })


def _real_stop() -> None:
    """Sinaliza a thread atual para parar (sem bloquear o render)."""
    stop: Optional[threading.Event] = st.session_state.get("serial_stop")
    if stop is not None:
        stop.set()


def _real_drain() -> None:
    """Drena a queue, detecta erros e atualiza session_state["latest_snap"]."""
    q: Optional[queue.Queue] = st.session_state.get("serial_queue")
    if q is None:
        return
    latest = None
    while True:
        try:
            item = q.get_nowait()
        except queue.Empty:
            break
        if isinstance(item, Exception):
            st.session_state["serial_error"] = str(item)
            st.session_state["connected"]    = False
            break
        latest = item
    if latest is not None:
        st.session_state["latest_snap"] = latest


def _real_thread_alive() -> bool:
    t: Optional[threading.Thread] = st.session_state.get("serial_thread")
    return t is not None and t.is_alive()


# ── Helpers de visualização ───────────────────────────────────────────────────

def _bits_html(value: int, width: int = 8) -> str:
    return "".join(
        f'<span style="color:{"#00e676" if b == "1" else "#444"};'
        f'font-family:monospace;font-size:1.15em;letter-spacing:1px">{b}</span>'
        for b in to_bits(value, width)
    )


def _render_regs(snap: AVRSnapshot) -> None:
    st.subheader("Registradores PORT / PIN / DDR")
    regs = {
        "PORTB": snap.ports.PORTB, "PORTC": snap.ports.PORTC, "PORTD": snap.ports.PORTD,
        "PINB":  snap.pins.PINB,   "PINC":  snap.pins.PINC,   "PIND":  snap.pins.PIND,
        "DDRB":  snap.ddr.DDRB,    "DDRC":  snap.ddr.DDRC,    "DDRD":  snap.ddr.DDRD,
    }
    st.dataframe(
        pd.DataFrame([
            {"Reg": name, "Hex": f"0x{v:02X}", "Dec": v, "Bits [7..0]": to_bits(v)}
            for name, v in regs.items()
        ]),
        use_container_width=True,
        hide_index=True,
    )
    st.markdown("**Visualização de bits (verde = 1):**")
    cols = st.columns(len(regs))
    for col, (name, val) in zip(cols, regs.items()):
        with col:
            st.markdown(f"**{name}**")
            st.markdown(_bits_html(val), unsafe_allow_html=True)
            st.caption(f"0x{val:02X}")


def _render_timers(snap: AVRSnapshot) -> None:
    st.subheader("Timers")
    c1, c2, c3 = st.columns(3)
    c1.metric("TCNT0 (8-bit)",  snap.timers.TCNT0)
    c1.markdown(_bits_html(snap.timers.TCNT0, 8),  unsafe_allow_html=True)
    c2.metric("TCNT1 (16-bit)", snap.timers.TCNT1)
    c2.markdown(_bits_html(snap.timers.TCNT1, 16), unsafe_allow_html=True)
    c3.metric("TCNT2 (8-bit)",  snap.timers.TCNT2)
    c3.markdown(_bits_html(snap.timers.TCNT2, 8),  unsafe_allow_html=True)


def _render_adc(snap: AVRSnapshot) -> None:
    st.subheader("ADC (0–1023  |  ref 5 V)")
    vals = {
        "A0": snap.adc.A0, "A1": snap.adc.A1, "A2": snap.adc.A2,
        "A3": snap.adc.A3, "A4": snap.adc.A4, "A5": snap.adc.A5,
    }
    st.dataframe(
        pd.DataFrame([
            {"Canal": ch, "Valor ADC": v, "Tensão (V)": round(v * 5.0 / 1023, 3)}
            for ch, v in vals.items()
        ]),
        use_container_width=True,
        hide_index=True,
    )
    st.bar_chart(pd.Series(vals), use_container_width=True, height=200)


def _render_sreg(snap: AVRSnapshot) -> None:
    st.subheader(f"SREG = 0x{snap.flags.SREG:02X}  ({to_bits(snap.flags.SREG)})")
    flags = {
        "I": snap.flags.I, "T": snap.flags.T, "H": snap.flags.H, "S": snap.flags.S,
        "V": snap.flags.V, "N": snap.flags.N, "Z": snap.flags.Z, "C": snap.flags.C,
    }
    cols = st.columns(8)
    for col, (flag, val) in zip(cols, flags.items()):
        color = "#00e676" if val else "#444"
        col.markdown(
            f'<div style="text-align:center;font-size:1.5em;color:{color};'
            f'border:1px solid #333;border-radius:6px;padding:6px 2px">'
            f"<b>{flag}</b><br>{val}</div>",
            unsafe_allow_html=True,
        )


def _render_memory(snap: AVRSnapshot) -> None:
    st.subheader("Dump de Memória")
    t_sram, t_eeprom, t_flash = st.tabs(["SRAM", "EEPROM", "FLASH"])
    with t_sram:
        st.code(
            format_hex_dump(parse_hex_addr(snap.memory.sram.start),   snap.memory.sram.bytes),
            language=None,
        )
    with t_eeprom:
        st.code(
            format_hex_dump(parse_hex_addr(snap.memory.eeprom.start), snap.memory.eeprom.bytes),
            language=None,
        )
    with t_flash:
        st.code(
            format_hex_dump(parse_hex_addr(snap.memory.flash.start),  snap.memory.flash.bytes),
            language=None,
        )


def _render_snap(snap: AVRSnapshot) -> None:
    _render_regs(snap)
    st.divider()
    _render_timers(snap)
    st.divider()
    _render_adc(snap)
    st.divider()
    _render_sreg(snap)
    st.divider()
    _render_memory(snap)


# ── Layout principal ──────────────────────────────────────────────────────────

def main() -> None:
    st.set_page_config(page_title="AVR Monitor", page_icon="⚡", layout="wide")
    st.title("⚡ AVR Monitor — ATmega328P")

    # ── Sidebar ───────────────────────────────────────────────────────────────
    with st.sidebar:
        st.header("Configuração")

        fake    = st.checkbox("Modo simulado (fake)", value=True)
        port    = st.text_input("Porta serial", value="/dev/ttyACM0", disabled=fake)
        baud    = st.selectbox("Baud rate", [9600, 57600, 115200], index=2, disabled=fake)
        refresh = st.slider("Refresh UI (s)", 0.5, 5.0, 2.0, 0.5)

        st.divider()
        connect_clicked = st.button("Conectar / Reiniciar leitura", use_container_width=True)

        # Status de conexão
        st.divider()
        st.markdown("**Status**")
        if fake:
            st.success("Modo fake ativo — atualização automática")
        elif st.session_state.get("connected") and _real_thread_alive():
            st.success(f"Conectado à serial {port}")
        elif st.session_state.get("serial_error"):
            st.error(f"Erro: {st.session_state['serial_error']}")
        else:
            st.warning("Aguardando conexão — clique no botão acima")

        # Status do fake_state.json (só em modo fake)
        if fake:
            st.divider()
            st.markdown("**fake_state.json**")
            fs = _load_fake_state()
            if fs is not None:
                active = [
                    k for k in ("ports", "pins", "ddr", "adc", "flags", "memory_writes")
                    if k in fs
                ]
                st.success(f"Ativo — {', '.join(active) or 'sem campos'}")
            elif FAKE_STATE_FILE.exists():
                st.warning("Arquivo existe mas JSON está inválido.")
            else:
                st.caption(
                    "Não encontrado.  \n"
                    "`cp fake_state.example.json fake_state.json`"
                )

    # ── Resposta ao clique em "Conectar / Reiniciar" ──────────────────────────
    if connect_clicked:
        if fake:
            _fake_init()                    # recria cliente (reseta memória interna)
        else:
            _real_start(port, baud)         # inicia/reinicia thread serial

    # ── Obtém snapshot do frame atual ─────────────────────────────────────────
    #
    # FAKE: chama next() sincronamente — sem thread, sem globals de módulo.
    #       fake_state.json é relido dentro de _fake_next() a cada chamada.
    #       O snapshot está sempre disponível (generator infinito).
    #
    # REAL: drena a queue preenchida pela thread e retorna o item mais recente.
    #       Se a thread ainda não produziu dados, retorna None.

    snap: Optional[AVRSnapshot]

    if fake:
        snap = _fake_next()
    else:
        _real_drain()
        snap = st.session_state.get("latest_snap")

    # ── Cabeçalho de status (área principal) ──────────────────────────────────
    if snap is not None:
        label = "🟡 SIMULADO" if fake else f"🟢 SERIAL  {port} @ {baud}"
        st.caption(f"{label}  |  t = {snap.timestamp_ms} ms")
    else:
        st.caption("⚫ Sem dados")
    st.divider()

    # ── Conteúdo principal ────────────────────────────────────────────────────
    if snap is None:
        st.info(
            "Sem dados. Verifique a conexão e clique em "
            "**Conectar / Reiniciar leitura** na barra lateral."
        )
    else:
        _render_snap(snap)

    # ── Auto-refresh ──────────────────────────────────────────────────────────
    # Aguarda `refresh` segundos e força novo rerun.
    # Em modo fake: próximo rerun chama _fake_next() → lê fake_state.json atualizado.
    # Em modo real: próximo rerun drena a queue com dados frescos da thread.
    time.sleep(refresh)
    st.rerun()


main()
