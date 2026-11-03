import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from astrbot.api import logger


class QuotaManager:
    """配额管理器：自动按日刷新，支持普通群友额度与四碗私房画纸池"""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.data: dict[str, Any] = {}
        self.load()

    def load(self):
        try:
            if self.db_path.exists():
                with open(self.db_path, encoding="utf-8") as f:
                    self.data = json.load(f)
        except Exception as e:
            logger.error(f"[DrawQuota] 读取配额文件失败: {e}")
            self.data = {}

    def save(self):
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.db_path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"[DrawQuota] 保存配额文件失败: {e}")

    @staticmethod
    def get_today_str() -> str:
        return datetime.now().strftime("%Y-%m-%d")

    def get_user_remaining(self, user_id: str, daily_limit: int) -> int:
        today = self.get_today_str()
        user = self.data.get(user_id)
        if not user or user.get("lastDate") != today:
            self.data[user_id] = {
                "lastDate": today,
                "remaining": daily_limit,
                "lastDrawTime": user.get("lastDrawTime", 0) if user else 0,
            }
            self.save()
            return daily_limit
        return user.get("remaining", 0)

    def can_draw_user(
        self, user_id: str, is_whitelist: bool, cooldown_seconds: int, daily_limit: int
    ) -> tuple[bool, str]:
        if is_whitelist:
            return True, ""

        remaining = self.get_user_remaining(user_id, daily_limit)
        if remaining <= 0:
            return (
                False,
                f"❌ 该群友今日的 {daily_limit} 张画纸已耗尽（明天 0 点自动刷新）。如果四碗非常想画，可以考虑动用自己的「私房画纸」~",
            )

        now = time.time()
        last_draw = self.data.get(user_id, {}).get("lastDrawTime", 0)
        diff = int(now - last_draw)
        if diff < cooldown_seconds:
            return (
                False,
                f"⏳ 画笔正在冷却中，请休息 {cooldown_seconds - diff} 秒后再试。",
            )

        return True, ""

    def deduct_user(self, user_id: str, is_whitelist: bool):
        if is_whitelist:
            return
        if user_id in self.data:
            self.data[user_id]["remaining"] = max(
                0, self.data[user_id].get("remaining", 1) - 1
            )
            self.data[user_id]["lastDrawTime"] = time.time()
            self.save()

    def get_siwan_private_remaining(self, default_limit: int = 10) -> int:
        today = self.get_today_str()
        siwan = self.data.get("_siwan_private")
        if not siwan or siwan.get("lastDate") != today:
            self.data["_siwan_private"] = {
                "lastDate": today,
                "remaining": default_limit,
                "lastDrawTime": siwan.get("lastDrawTime", 0) if siwan else 0,
            }
            self.save()
            return default_limit
        return siwan.get("remaining", 0)

    def can_draw_siwan_private(self, cooldown_seconds: int = 20) -> tuple[bool, str]:
        remaining = self.get_siwan_private_remaining()
        if remaining <= 0:
            return (
                False,
                "❌ 四碗今天的私房画纸已经用光啦（0/10）！明天才会重新补充。今天不能再任性挥霍啦，请使用群友的额度~",
            )

        now = time.time()
        last_draw = self.data.get("_siwan_private", {}).get("lastDrawTime", 0)
        diff = int(now - last_draw)
        if diff < cooldown_seconds:
            return (
                False,
                f"⏳ 四碗刚才画得太猛手腕酸了，休息 {cooldown_seconds - diff} 秒再动笔吧~",
            )

        return True, ""

    def deduct_siwan_private(self):
        today = self.get_today_str()
        siwan = self.data.get("_siwan_private", {})
        if siwan.get("lastDate") != today:
            siwan = {"lastDate": today, "remaining": 10, "lastDrawTime": 0}

        siwan["remaining"] = max(0, siwan.get("remaining", 10) - 1)
        siwan["lastDrawTime"] = time.time()
        self.data["_siwan_private"] = siwan
        self.save()
