from contextlib import asynccontextmanager

from fastapi import FastAPI

from ht812_client import HT812Client
from router import router


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.ht812 = HT812Client()
    yield
    await app.state.ht812.aclose()


app = FastAPI(
    title="HT812V2 Control API",
    description="Full control API for the Grandstream HT812V2 ATA: config snapshot/patch, reboot, factory reset, SIP status.",
    version="1.0.0",
    lifespan=lifespan,
)

app.include_router(router)


@app.get("/health")
async def health():
    return {"status": "ok"}
