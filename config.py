import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()

@dataclass
class Config:
    # Bot
    bot_token: str = os.getenv("BOT_TOKEN", "")
    bot_username: str = os.getenv("BOT_USERNAME", "AuronSearchBot")
    admin_id: int = int(os.getenv("ADMIN_ID", "8276815852"))
    
    # Database
    database_url: str = os.getenv("DATABASE_URL", "")
    
    # Redis
    redis_url: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    
    # Webhook
    webhook_url: str = os.getenv("WEBHOOK_URL", "")
    webhook_path: str = os.getenv("WEBHOOK_PATH", "/webhook")
    webhook_secret: str = os.getenv("WEBHOOK_SECRET", "")
    
    # Server
    port: int = int(os.getenv("PORT", "8080"))
    environment: str = os.getenv("ENVIRONMENT", "development")
    
    # Limits
    free_limit: int = 10
    max_ref_bonus: int = 20
    
    # Wallet
    usdt_wallet: str = "TQ1hHPveZ737G5i1ZxHN2sfpV9PSdx5nfV"
    
    # Tariffs
    stars_prices = {"1d": 10, "7d": 60, "30d": 240, "1y": 800, "forever": 1500}
    stars_days = {"1d": 1, "7d": 7, "30d": 30, "1y": 365, "forever": -1}
    usdt_prices = {"1d": 0.5, "7d": 3, "30d": 6, "1y": 25, "forever": 50}
    usdt_days = {"1d": 1, "7d": 7, "30d": 30, "1y": 365, "forever": -1}
    
    def validate(self):
        """Проверка обязательных переменных окружения"""
        errors = []
        
        if not self.bot_token:
            errors.append("❌ BOT_TOKEN is not set in .env file")
        if not self.database_url:
            errors.append("❌ DATABASE_URL is not set in .env file")
        
        # Для продакшена дополнительная проверка
        if self.environment == "production":
            if not self.webhook_url:
                errors.append("❌ WEBHOOK_URL is required in production mode")
            if not self.webhook_secret:
                errors.append("❌ WEBHOOK_SECRET is required in production mode")
        
        if errors:
            raise ValueError("\n".join(errors))
        
        return True

# Создаём экземпляр конфига и сразу валидируем
config = Config()
config.validate()
