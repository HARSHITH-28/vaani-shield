from fastapi import FastAPI

from backend.agent_routes import router as agent_router
from services.database import initialize_database

app = FastAPI(
    title="VaaniShield Gnani Gateway",
    description="Authenticated banking actions for Gnani agents",
    version="1.0.0",
)

app.include_router(agent_router)


@app.on_event("startup")
def initialize_database_on_startup():
    """Create or migrate the schema without seeding or resetting data."""
    initialize_database()


@app.get("/")
def health():
    return {
        "application": "VaaniShield Gnani Gateway",
        "status": "healthy",
    }
