import pytest
from app.core.database import engine

@pytest.fixture(autouse=True)
async def dispose_engine_pool():
    yield
    await engine.dispose()
