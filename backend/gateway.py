from fastapi import FastAPI
from backend.agent_routes import router as agent_router

app = FastAPI(
    title="VaaniShield Gnani Gateway",
    description="Authenticated banking actions for Gnani agents",
    version="1.0.0",
)

app.include_router(agent_router)


@app.get("/")
def health():
    return {
        "application": "VaaniShield Gnani Gateway",
        "status": "healthy",
    }
