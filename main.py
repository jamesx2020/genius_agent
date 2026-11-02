"""Run the single-process engineering Agent server."""
import uvicorn
from backend.config import settings

if __name__ == '__main__':
    cfg = settings()
    uvicorn.run('backend.app:app', host=cfg.app_host, port=cfg.app_port, workers=1, access_log=False)
