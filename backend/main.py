from contextlib import asynccontextmanager
import os

from fastapi import FastAPI

from backend.broker import BrokerConnector
from backend.connectors import ExternalConnectors
from backend.dart_etl import DartETL


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with BrokerConnector.from_env() as broker:
        app.state.broker = broker
        app.state.dart_etl = DartETL(broker.redis, broker.http, os.getenv("DART_API_KEY"))
        app.state.external_connectors = ExternalConnectors.from_env(broker.http, broker.redis)
        yield


app = FastAPI(title="FinGuard AI", lifespan=lifespan)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
