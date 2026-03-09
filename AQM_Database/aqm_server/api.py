"""
FastAPI endpoints for the AQM Server Coin Inventory.

Binary blobs (public keys, signatures) are base64-encoded in HTTP transport.
The API layer decodes b64 → bytes before passing to coin_inventory,
and encodes bytes → b64 in responses.
"""

import base64
from contextlib import asynccontextmanager
from uuid import UUID

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, field_validator

from AQM_Database.aqm_server import config, db
from AQM_Database.aqm_server.coin_inventory import CoinInventoryServer
from AQM_Database.aqm_shared.types import CoinUpload
from AQM_Database.aqm_shared.errors import (
    InvalidCoinCategoryError,
    ServerDatabaseError,
    FetchError,
    UploadError,
)


# ── Pydantic request/response models ──


class CoinUploadItem(BaseModel):
    key_id: str
    coin_category: str
    public_key_b64: str
    signature_b64: str

    @field_validator("coin_category")
    @classmethod
    def validate_category(cls, v):
        if v not in ("GOLD", "SILVER", "BRONZE"):
            raise ValueError(f"Invalid coin_category: {v}")
        return v


class UploadRequest(BaseModel):
    user_id: UUID
    coins: list[CoinUploadItem]


class UploadResponse(BaseModel):
    inserted: int


class CoinOut(BaseModel):
    key_id: str
    coin_category: str
    public_key_b64: str
    signature_b64: str


class FetchResponse(BaseModel):
    coins: list[CoinOut]


class CountResponse(BaseModel):
    gold: int
    silver: int
    bronze: int


class PurgeRequest(BaseModel):
    max_age_days: int = 30


class HardDeleteRequest(BaseModel):
    grace_hours: int = 1


class DeleteResponse(BaseModel):
    deleted: int


class HealthResponse(BaseModel):
    status: str
    db_connected: bool


# ── App lifecycle ──


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.create_pool(
        config.PG_DSN,
        min_size=config.PG_POOL_MIN_SIZE,
        max_size=config.PG_POOL_MAX_SIZE,
    )
    yield
    await db.close_pool()


app = FastAPI(title="AQM Coin Inventory", version="1.0.0", lifespan=lifespan)


def _get_inventory() -> CoinInventoryServer:
    if db.pool is None:
        raise HTTPException(status_code=503, detail="Database pool not initialized")
    return CoinInventoryServer(db.pool)


# ── Endpoints ──


@app.post("/v1/coins/upload", response_model=UploadResponse)
async def upload_coins(req: UploadRequest):
    inv = _get_inventory()
    coins = [
        CoinUpload(
            key_id=c.key_id,
            coin_category=c.coin_category,
            public_key_blob=base64.b64decode(c.public_key_b64),
            signature_blob=base64.b64decode(c.signature_b64),
        )
        for c in req.coins
    ]
    try:
        inserted = await inv.upload_coins(req.user_id, coins)
    except UploadError as e:
        raise HTTPException(status_code=500, detail=str(e))
    return UploadResponse(inserted=inserted)


@app.get("/v1/coins/fetch", response_model=FetchResponse)
async def fetch_coins(
    target_user_id: UUID = Query(...),
    requester_id: UUID = Query(...),
    coin_category: str = Query(...),
    count: int = Query(..., gt=0),
):
    inv = _get_inventory()
    try:
        records = await inv.fetch_coins(target_user_id, requester_id, coin_category, count)
    except InvalidCoinCategoryError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except FetchError as e:
        raise HTTPException(status_code=500, detail=str(e))

    coins_out = [
        CoinOut(
            key_id=r.key_id,
            coin_category=r.coin_category,
            public_key_b64=base64.b64encode(r.public_key_blob).decode(),
            signature_b64=base64.b64encode(r.signature_blob).decode(),
        )
        for r in records
    ]
    return FetchResponse(coins=coins_out)


@app.get("/v1/coins/count", response_model=CountResponse)
async def get_count(user_id: UUID = Query(...)):
    inv = _get_inventory()
    try:
        c = await inv.get_inventory_count(user_id)
    except ServerDatabaseError as e:
        raise HTTPException(status_code=500, detail=str(e))
    return CountResponse(gold=c.gold, silver=c.silver, bronze=c.bronze)


@app.post("/v1/admin/purge-stale", response_model=DeleteResponse)
async def purge_stale(req: PurgeRequest):
    inv = _get_inventory()
    try:
        deleted = await inv.purge_stale(max_age_days=req.max_age_days)
    except ServerDatabaseError as e:
        raise HTTPException(status_code=500, detail=str(e))
    return DeleteResponse(deleted=deleted)


@app.post("/v1/admin/hard-delete", response_model=DeleteResponse)
async def hard_delete(req: HardDeleteRequest):
    inv = _get_inventory()
    try:
        deleted = await inv.hard_delete_fetched(grace_hours=req.grace_hours)
    except ServerDatabaseError as e:
        raise HTTPException(status_code=500, detail=str(e))
    return DeleteResponse(deleted=deleted)


@app.get("/v1/health", response_model=HealthResponse)
async def health():
    connected = await db.health_check()
    return HealthResponse(
        status="ok" if connected else "degraded",
        db_connected=connected,
    )
