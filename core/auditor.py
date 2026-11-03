import re

POSITIVE_BLOCKED_WORDS_REGEX = re.compile(
    r"\b(nsfw|sensitive|explicit|pussy|nude|naked|nipples?|penis|vagina|sex|hentai|anal|lewd|uncensored|areola)\b|裸|色情|露点|生殖器|做爱",
    re.IGNORECASE,
)
NEGATIVE_ANTI_SFW_REGEX = re.compile(
    r"\b(safe|sfw|censored|clothes|clothed)\b|衣服|合规", re.IGNORECASE
)


class ContentAuditor:
    """提示词合规性审查器"""

    @staticmethod
    def audit(prompt: str, negative: str) -> tuple[bool, str | None]:
        pos_match = POSITIVE_BLOCKED_WORDS_REGEX.search(prompt or "")
        if pos_match:
            return (
                False,
                f"⚠️ 触发安全红线：描述词包含违规关键词 [{pos_match.group(0)}]。严禁生成 NSFW/露骨内容！",
            )
        neg_match = NEGATIVE_ANTI_SFW_REGEX.search(negative or "")
        if neg_match:
            return (
                False,
                f"⚠️ 触发安全红线：负向提示词中严禁试图过滤着装特征 [{neg_match.group(0)}]！",
            )
        return True, None
