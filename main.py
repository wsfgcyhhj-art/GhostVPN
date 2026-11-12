from __future__ import annotations

import uvicorn

from app.core.config import settings
from app.web.app import app

if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=settings.PORT,
        workers=1,
        log_level=settings.LOG_LEVEL.lower(),
    )
