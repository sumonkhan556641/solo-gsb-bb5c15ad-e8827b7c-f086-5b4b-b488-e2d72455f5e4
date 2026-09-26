from fastapi import APIRouter, Depends, HTTPException
from sqlite3 import Connection

from .. import crud
from ..database import get_db
from ..schemas import TraceEdgeIn, TraceEdgeOut

router = APIRouter(prefix="/api/edges", tags=["edges"])


def _row(r):
    return {k: r[k] for k in (
        "id", "child_cert_id", "parent_cert_id", "valid_from", "valid_until",
        "note",
    )}


@router.post("", response_model=TraceEdgeOut, status_code=201)
def create_or_update_edge(body: TraceEdgeIn, db: Connection = Depends(get_db)):
    try:
        edge_id = crud.upsert_edge(db, body)
    except crud.ValidationError as exc:
        raise HTTPException(422, str(exc))
    row = db.execute("SELECT * FROM trace_edges WHERE id=?", (edge_id,)).fetchone()
    return _row(row)


@router.get("", response_model=list[TraceEdgeOut])
def list_edges(db: Connection = Depends(get_db)):
    return [_row(r) for r in crud.list_edges(db)]
