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

    # Vapi has no balance API, so we remember the balance you read from its dashboard
    # (credit_balance) and the tracked spend at that moment (credit_balance_spend_at).
    # Remaining credit = credit_balance - spend since then.
    credit_balance: float | None = None
    credit_balance_spend_at: float = 0.0
    # Preferred: the moment (UTC, e.g. 2026-10-01T09:00:00Z) you read credit_balance in Vapi.
    # Spend since then is summed from Vapi's own call list, so nothing is missed.
    credit_balance_at: str | None = None

    @property
    def allowed_origins_list(self) -> list[str]:
        return [o.strip() for o in self.allowed_origins.split(",") if o.strip()]


settings = Settings()
