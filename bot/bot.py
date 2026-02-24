"""Legacy entrypoint.

Раньше весь код жил в этом файле. После рефакторинга логика разнесена по пакетам:
- handlers/
- services/
- ui/
- app.py (создание Application + запуск)

Этот файл оставлен для обратной совместимости (импорт bot.main).
"""

from __future__ import annotations

from .app import main  # noqa: F401
