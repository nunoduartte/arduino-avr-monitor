"""
arduino_service.py — único dono da conexão serial com o Arduino.

Arquitetura:
  Uma única thread de leitura (`_reader_loop`) consome `client.messages()`,
  roteia snapshots para `_latest_snapshot` e ACKs para quem está esperando.
  Toda escrita serial passa por `_send_raw` + Event para sincronização.

Estado compartilhado:
  O Arduino é a fonte de verdade. Não existe mais "pending" local em Python —
  cada chamada de set_field/focus/compute_current envia um comando ao Arduino e
  aguarda o ACK. O `GET /api/state` lê o último snapshot conhecido.

Comandos disponíveis:
  set_field(field, value)  → {"cmd":"set_field","field":..,"value":..}
  set_focus(field)         → {"cmd":"focus","field":..}
  compute_current()        → {"cmd":"compute_current"}
  reset()                  → {"cmd":"reset"}
  send_ula_compat(op,x,y)  → {"cmd":"ula","op":..,"x":..,"y":..}  (compat)
"""
from __future__ import annotations

import threading
from typing import Any, Dict, Optional

from .models import AVRSnapshot, UlaAck
from .serial_client import BaseClient, _normalize_op, make_client


class ArduinoService:
    def __init__(
        self,
        fake: bool = False,
        port: str = "/dev/ttyACM0",
        baud: int = 115200,
        interval: float = 0.5,
    ):
        self._client: BaseClient = make_client(fake=fake, port=port, baud=baud, interval=interval)

        self._lock = threading.Lock()       # protege _latest_snapshot e _inflight
        self._cmd_lock = threading.Lock()   # garante um único comando "em voo" por vez

        self._latest_snapshot: Optional[AVRSnapshot] = None
        # _inflight: {"event": Event, "result": dict|None}
        self._inflight: Optional[Dict[str, Any]] = None

        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._reader_loop, daemon=True, name="arduino-reader"
        )

    # ── Ciclo de vida ────────────────────────────────────────────────────────

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._client.close()

    # ── Thread de leitura ────────────────────────────────────────────────────

    def _reader_loop(self) -> None:
        for msg in self._client.messages():
            if self._stop.is_set():
                return
            msg_type = msg.get("type")
            if msg_type == "snapshot":
                self._handle_snapshot(msg)
            elif msg_type == "ack":
                self._handle_ack(msg)

    def _handle_snapshot(self, msg: Dict) -> None:
        try:
            snap = AVRSnapshot.model_validate(msg)
        except Exception:
            return
        with self._lock:
            self._latest_snapshot = snap

    def _handle_ack(self, msg: Dict) -> None:
        with self._lock:
            inflight = self._inflight
        if inflight is not None:
            inflight["result"] = msg
            inflight["event"].set()

    # ── Leitura de estado (nunca toca a serial) ──────────────────────────────

    def get_state(self) -> Optional[AVRSnapshot]:
        with self._lock:
            return self._latest_snapshot

    # ── Envio de comando genérico ────────────────────────────────────────────

    def _send(self, cmd: Dict, timeout: float = 2.0) -> UlaAck:
        """
        Envia `cmd` pela serial e aguarda qualquer ACK.
        Garante exclusão mútua: um único comando por vez.
        """
        with self._cmd_lock:
            event = threading.Event()
            inflight: Dict[str, Any] = {"event": event, "result": None}
            with self._lock:
                self._inflight = inflight
            self._client.send_raw_command(cmd)
            got = event.wait(timeout)
            with self._lock:
                self._inflight = None
            if not got or inflight["result"] is None:
                return UlaAck(ok=False, cmd=cmd.get("cmd", "?"), error="timeout")
            return UlaAck.model_validate(inflight["result"])

    # ── Comandos de campo individual ─────────────────────────────────────────

    def set_field(self, field: str, value: "int | str", timeout: float = 2.0) -> UlaAck:
        """Envia set_field para o Arduino. field = 'op'|'x'|'y'."""
        if field == "op":
            v = _normalize_op(value)
            if v is None:
                return UlaAck(ok=False, cmd="set_field", error="invalid_op")
            payload: Any = v
        elif field in ("x", "y"):
            try:
                payload = int(value)
            except (TypeError, ValueError):
                return UlaAck(ok=False, cmd="set_field", error="invalid_value")
            if not (0 <= payload <= 15):
                return UlaAck(ok=False, cmd="set_field", error="invalid_value")
        else:
            return UlaAck(ok=False, cmd="set_field", error="invalid_field")
        return self._send({"cmd": "set_field", "field": field, "value": payload}, timeout)

    def set_focus(self, field: str, timeout: float = 2.0) -> UlaAck:
        """Envia focus para o Arduino. field = 'op'|'x'|'y'."""
        if field not in ("op", "x", "y"):
            return UlaAck(ok=False, cmd="focus", error="invalid_field")
        return self._send({"cmd": "focus", "field": field}, timeout)

    def compute_current(self, timeout: float = 2.0) -> UlaAck:
        """Dispara compute_current no Arduino."""
        return self._send({"cmd": "compute_current"}, timeout)

    def reset(self, timeout: float = 2.0) -> UlaAck:
        """Reseta o estado do Arduino."""
        return self._send({"cmd": "reset"}, timeout)

    def send_ula_compat(self, op: "int | str", x: int, y: int, timeout: float = 2.0) -> UlaAck:
        """Comando legado: seta op/x/y e calcula num único comando."""
        return self._send({"cmd": "ula", "op": op, "x": x, "y": y}, timeout)
