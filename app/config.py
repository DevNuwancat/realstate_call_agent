from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    vapi_api_key: str
    vapi_assistant_id: str
    vapi_phone_number_id: str = ""
    vapi_webhook_secret: str = ""

    supabase_url: str
    supabase_service_role_key: str

    allowed_origins: str = "*"

    @property
    def allowed_origins_list(self) -> list[str]:
        return [o.strip() for o in self.allowed_origins.split(",") if o.strip()]


settings = Settings()
