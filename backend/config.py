from functools import lru_cache
from pathlib import Path
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / '.env', extra='ignore')
    deepseek_api_key: str = ''
    deepseek_base_url: str = 'https://api.deepseek.com'
    deepseek_model: str = 'deepseek-flash'
    model_temperature: float = 0.1
    model_timeout_seconds: int = Field(90, ge=1, le=600)
    model_max_tokens: int = Field(4096, ge=256, le=32768)
    agent_recursion_limit: int = Field(80, ge=8, le=200)
    agent_task_timeout_seconds: int = Field(600, ge=5, le=1800)
    app_host: str = '127.0.0.1'
    app_port: int = 8000
    data_dir: Path = ROOT / 'data'
    session_cookie_name: str = 'session'
    cookie_secure: bool = False
    session_hours: int = 12
    pm_password: str = 'demo123'
    buyer_password: str = 'demo123'
    finance_password: str = 'demo123'
    max_upload_mb: int = 10

@lru_cache
def settings():
    cfg = Settings()
    if not cfg.data_dir.is_absolute():
        cfg.data_dir = (ROOT / cfg.data_dir).resolve()
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    (cfg.data_dir / 'uploads').mkdir(exist_ok=True)
    return cfg
