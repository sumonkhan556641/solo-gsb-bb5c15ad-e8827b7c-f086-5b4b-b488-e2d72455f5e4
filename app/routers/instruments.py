from fastapi import APIRouter, Depends, HTTPException
from sqlite3 import Connection

from .. import crud
from ..database import get_db
from ..schemas import InstrumentIn, InstrumentOut

router = APIRouter(prefix="/api/instruments", tags=["instruments"])


def _row(r):
    return {
        "code": r["code"],
        "name": r["name"],
        "manufacturer": r["manufacturer"],
        "model": r["model"],
        "serial_no": r["serial_no"],
        "is_national_standard": bool(r["is_national_standard"]),
        "created_at": r["created_at"],
    }


@router.post("", response_model=InstrumentOut, status_code=201)
def create_or_update_instrument(
    body: InstrumentIn, db: Connection = Depends(get_db)
):
    crud.upsert_instrument(db, body)
    return _row(crud.get_instrument(db, body.code))


@router.get("", response_model=list[InstrumentOut])
def list_instruments(db: Connection = Depends(get_db)):
    return [_row(r) for r in crud.list_instruments(db)]


@router.get("/{code}", response_model=InstrumentOut)
def get_instrument(code: str, db: Connection = Depends(get_db)):
    row = crud.get_instrument(db, code)
    if row is None:
        raise HTTPException(404, f"仪器不存在: {code}")
    return _row(row)
