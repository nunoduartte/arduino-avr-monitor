"""
api.py — API HTTP local (FastAPI) para operar a ULA remotamente.

O ArduinoService mantém a porta serial aberta e é a única camada que
escreve/lê bytes. Cada endpoint aqui delega para um método do service.

Endpoints:
  GET  /api/health
  GET  /api/state              → último snapshot (fonte de verdade = Arduino)
  POST /api/ula/field          → set_field: define um campo (op/x/y)
  POST /api/ula/focus          → altera qual campo as chaves físicas editam
  POST /api/ula/compute-current→ dispara cálculo (exige has_op+has_x+has_y)
  POST /api/ula/reset          → zera tudo
  POST /api/ula                → compat: seta op+x+y e calcula numa chamada

Variáveis de ambiente:
  AVR_MONITOR_FAKE=1            → usa FakeSerialClient (sem Arduino)
  AVR_MONITOR_PORT=/dev/ttyACM0 → porta serial
  AVR_MONITOR_BAUD=115200        → baud rate
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Literal, Optional, Union

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel

from .arduino_service import ArduinoService
from .sram_report import generate_sram_pdf_report, _parse_addr

_FAKE = os.environ.get("AVR_MONITOR_FAKE", "0") == "1"
_PORT = os.environ.get("AVR_MONITOR_PORT", "/dev/ttyACM0")
_BAUD = int(os.environ.get("AVR_MONITOR_BAUD", "115200"))

service: Optional[ArduinoService] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global service
    service = ArduinoService(fake=_FAKE, port=_PORT, baud=_BAUD)
    service.start()
    try:
        yield
    finally:
        service.stop()


app = FastAPI(title="AVR Monitor API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Modelos de request ────────────────────────────────────────────────────────

class UlaFieldRequest(BaseModel):
    field: Literal["op", "x", "y"]
    value: Union[int, str]


class UlaFocusRequest(BaseModel):
    field: Literal["op", "x", "y"]


class UlaCompatRequest(BaseModel):
    op: Union[int, str]
    x: int
    y: int


# ── Helper ────────────────────────────────────────────────────────────────────

def _service() -> ArduinoService:
    if service is None:
        raise HTTPException(status_code=503, detail="service_not_started")
    return service


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/api/health")
def health() -> dict:
    return {"ok": True}


@app.get("/api/state")
def get_state() -> dict:
    """
    Retorna o último snapshot recebido do Arduino.

    O snapshot já inclui has_op/x/y, focus_field, state_version e
    last_input_source — tudo o que um frontend precisa para saber o
    estado atual sem fazer polling pesado.
    """
    snap = _service().get_state()
    return {"snapshot": snap.model_dump() if snap is not None else None}


@app.post("/api/ula/field")
def ula_field(req: UlaFieldRequest) -> dict:
    """
    Define um único campo da ULA (op, x ou y).

    Envia {"cmd":"set_field","field":..,"value":..} ao Arduino.
    Se o campo setado coincidir com o focus atual, o Arduino avança o
    focus automaticamente para o próximo campo não definido.
    """
    svc = _service()
    try:
        ack = svc.set_field(req.field, req.value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not ack.ok:
        raise HTTPException(status_code=400, detail=ack.error or "error")
    return ack.model_dump()


@app.post("/api/ula/focus")
def ula_focus(req: UlaFocusRequest) -> dict:
    """
    Altera qual campo as chaves físicas estão editando.

    Envia {"cmd":"focus","field":..} ao Arduino.
    O hardware passa a mostrar o valor desse campo nos LEDs como preview
    e confirma o campo ao próximo pressionamento do botão.
    """
    ack = _service().set_focus(req.field)
    if not ack.ok:
        raise HTTPException(status_code=400, detail=ack.error or "error")
    return ack.model_dump()


@app.post("/api/ula/compute-current")
def ula_compute_current() -> dict:
    """
    Dispara o cálculo com os campos já definidos no Arduino.

    Requer has_op && has_x && has_y (ou campo em foco = effective has).
    Em caso de campos faltantes, retorna 400 com a lista em `missing`.
    """
    ack = _service().compute_current()
    if not ack.ok:
        detail = {"error": ack.error, "missing": ack.missing}
        raise HTTPException(status_code=400, detail=detail)
    return ack.model_dump()


@app.post("/api/ula/reset")
def ula_reset() -> dict:
    """Zera todos os campos e volta ao estado EDITING com focus=OP."""
    ack = _service().reset()
    return ack.model_dump()


@app.get("/api/report/sram-dump")
def sram_dump_json(
    start: int = Query(0x0100, ge=0x0100, le=0x08FF),
    length: int = Query(2048, ge=1, le=2048),
    chunk_size: int = Query(32, ge=1, le=64),
    timeout: float = Query(10.0, gt=0),
) -> dict:
    """
    Coleta a SRAM e retorna resumo JSON (sem os bytes brutos).
    Útil para validar a coleta antes de gerar o PDF.
    """
    svc = _service()
    try:
        sram_map = svc.request_sram_dump(
            start=start, length=length, chunk_size=chunk_size,
            timeout_seconds=timeout,
        )
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    snap = svc.get_state()
    ula  = snap.ula if snap else None

    special: dict = {}
    if ula:
        for field in ("addr_x", "addr_y", "addr_result", "addr_carry", "addr_op", "addr_estado"):
            raw = getattr(ula, field, "0x0000")
            addr = _parse_addr(raw)
            label = field.replace("addr_", "")
            special[label] = f"0x{addr:04X}"

    return {
        "start":            f"0x{start:04X}",
        "length":           length,
        "bytes_collected":  len(sram_map),
        "special_addresses": special,
    }


@app.get("/api/report/sram.pdf")
def sram_pdf(
    start: int = Query(0x0100, ge=0x0100, le=0x08FF),
    length: int = Query(2048, ge=1, le=2048),
    chunk_size: int = Query(32, ge=1, le=64),
    timeout: float = Query(10.0, gt=0),
) -> Response:
    """
    Gera e retorna o relatório PDF da SRAM.
    Os bytes da ULA são destacados com cores no dump hexadecimal.
    """
    svc = _service()
    try:
        sram_map = svc.request_sram_dump(
            start=start, length=length, chunk_size=chunk_size,
            timeout_seconds=timeout,
        )
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    snap = svc.get_state()
    ula  = snap.ula if snap else None

    ula_addresses: dict = {}
    if ula:
        for field in ("addr_x", "addr_y", "addr_result", "addr_carry", "addr_op", "addr_estado"):
            raw = getattr(ula, field, "0x0000")
            ula_addresses[field] = _parse_addr(raw)

    try:
        pdf_bytes = generate_sram_pdf_report(
            sram_map=sram_map,
            ula_addresses=ula_addresses,
            latest_snapshot=snap,
            start=start,
            length=length,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"PDF generation failed: {exc}")

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": 'attachment; filename="sram-ula-report.pdf"'},
    )


@app.post("/api/ula")
def ula_compat(req: UlaCompatRequest) -> dict:
    """
    Endpoint de compatibilidade: seta op+x+y e calcula numa única chamada.
    Equivalente a três /api/ula/field seguidos de /api/ula/compute-current.
    """
    svc = _service()
    ack = svc.send_ula_compat(req.op, req.x, req.y)
    snap = svc.get_state()
    return {
        "ok": ack.ok,
        "op": req.op,
        "x": req.x,
        "y": req.y,
        "result": ack.result,
        "carry": ack.carry,
        "error": ack.error,
        "snapshot": snap.model_dump() if snap is not None else None,
    }
