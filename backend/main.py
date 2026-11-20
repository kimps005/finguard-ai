from fastapi import FastAPI

app = FastAPI(title="FinGuard AI")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
