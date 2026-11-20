from contextlib import asynccontextmanager

from fastapi import FastAPI

from backend.broker import BrokerConnector


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with BrokerConnector.from_env() as broker:
        app.state.broker = broker
        yield


app = FastAPI(title="FinGuard AI", lifespan=lifespan)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
