"""
Entry point: python -m src.main
"""

import logging
import uvicorn

from src.api.server import create_app
from src.config import get_settings

app = create_app()


def main():
    settings = get_settings()

    print(f"""
+--------------------------------------------------------------+
|              TELEGRAM → AVANTIS BOT                          |
|                                                              |
|  Server: {settings.api_host}:{settings.api_port:<43}  |
|  Dry Run: {str(settings.dry_run):<46} |
|  Dashboard: http://localhost:{settings.api_port}/dashboard{' ' * 20} |
+--------------------------------------------------------------+
    """)

    uvicorn.run(
        app,
        host=settings.api_host,
        port=settings.api_port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
