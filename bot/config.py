"""Настройки бота из файла .env."""
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent

# Русский текст в среднем занимает 3-4 символа на токен. Считаем по нижней границе,
# чтобы сжимать историю чуть раньше, а не упираться в молчаливую обрезку на сервере.
CHARS_PER_TOKEN = 3


@dataclass(frozen=True)
class Config:
    bot_token: str
    admin_username: str
    lm_base_url: str
    lms_path: str
    default_model: str
    context_length: int
    summary_max_tokens: int
    idle_unload_minutes: int
    db_path: Path
    # Минимум места под ответ в токенах. Обычно модели отдаётся всё свободное место
    # контекста, но под него всегда держится хотя бы этот запас. Он же задаёт, сколько
    # контекста остаётся под историю, поэтому слишком большое значение тут ни к чему.
    reply_max_tokens: int = 4096
    # Рассуждения Qwen3.6 способны съесть весь лимит: рассказ занял 316 секунд и не дал
    # ни слова ответа, а без них вышел за 43 секунды. Пустое значение включает их обратно.
    reasoning_effort: str = "none"
    # Проект Bionic, где виден чат админа. Пустое значение выключает синхронизацию.
    bionic_project: str = ""

    @property
    def prompt_budget(self) -> int:
        """Сколько токенов можно отдать под запрос, оставив место на ответ."""
        return self.context_length - self.reply_max_tokens

    @property
    def max_input_chars(self) -> int:
        """Предел одного сообщения: больше не поместится рядом с историей."""
        return int(self.prompt_budget * 0.6 * CHARS_PER_TOKEN)


def load_config() -> Config:
    load_dotenv(ROOT / ".env")

    token = os.getenv("BOT_TOKEN", "").strip()
    admin = os.getenv("ADMIN_USERNAME", "").strip().lstrip("@").lower()
    if not token or not admin:
        raise SystemExit(
            "Заполни BOT_TOKEN и ADMIN_USERNAME в файле .env (образец: .env.example)."
        )

    default_lms = Path.home() / ".lmstudio" / "bin" / "lms.exe"
    config = Config(
        bot_token=token,
        admin_username=admin,
        lm_base_url=os.getenv("LM_BASE_URL", "http://127.0.0.1:1234").rstrip("/"),
        lms_path=os.getenv("LMS_PATH", str(default_lms)),
        default_model=os.getenv("DEFAULT_MODEL", "").strip(),
        context_length=int(os.getenv("CONTEXT_LENGTH", "16384")),
        reply_max_tokens=int(os.getenv("REPLY_MAX_TOKENS", "4096")),
        summary_max_tokens=int(os.getenv("SUMMARY_MAX_TOKENS", "3000")),
        idle_unload_minutes=int(os.getenv("IDLE_UNLOAD_MINUTES", "5")),
        db_path=ROOT / "data" / "bot.db",
        reasoning_effort=os.getenv("REASONING_EFFORT", "none").strip(),
        bionic_project=os.getenv("BIONIC_PROJECT", "").strip(),
    )
    if config.reply_max_tokens >= config.context_length:
        raise SystemExit("REPLY_MAX_TOKENS должен быть меньше CONTEXT_LENGTH.")
    return config
