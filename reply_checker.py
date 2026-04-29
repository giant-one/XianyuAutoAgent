import json
import re
from dataclasses import dataclass, field
from typing import Optional, List
from loguru import logger


@dataclass
class Rule:
    id: str
    name: str
    match_type: str       # "exact" | "regex"
    pattern: str
    reply: List[str]      # 支持多条回复
    priority: int
    compiled: Optional[re.Pattern] = field(default=None, repr=False)


class RuleChecker:
    RULES_PATH = "rules.json"

    def __init__(self):
        self._rules: list[Rule] = []
        self._enabled: bool = True
        self.reload()

    def reload(self) -> None:
        """读取 rules.json，预编译正则，按 priority 降序排序"""
        try:
            with open(self.RULES_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)

            self._enabled = data.get("enabled", True)
            new_rules = []
            for r in data.get("rules", []):
                compiled = None
                if r["match_type"] == "regex":
                    try:
                        compiled = re.compile(r["pattern"])
                    except re.error as e:
                        logger.error(f"[RuleChecker] 规则 {r.get('id')} 正则编译失败: {e}，已跳过")
                        continue

                # reply 必须是数组格式
                replies = r.get("reply", [])
                if not isinstance(replies, list):
                    logger.warning(f"[RuleChecker] 规则 {r.get('id')} 的 reply 字段不是数组格式，已跳过")
                    continue

                new_rules.append(Rule(
                    id=r.get("id", ""),
                    name=r.get("name", ""),
                    match_type=r["match_type"],
                    pattern=r["pattern"],
                    reply=replies,
                    priority=r.get("priority", 0),
                    compiled=compiled,
                ))

            # 按 priority 降序排列（优先级高的在前，多条命中时取第一条即为最高优先级）
            new_rules.sort(key=lambda x: x.priority, reverse=True)
            self._rules = new_rules
            logger.info(f"[RuleChecker] 加载完成，共 {len(self._rules)} 条规则，enabled={self._enabled}")

        except FileNotFoundError:
            logger.warning(f"[RuleChecker] {self.RULES_PATH} 不存在，规则层不生效")
            self._rules = []
        except Exception as e:
            logger.error(f"[RuleChecker] 加载规则文件失败: {e}，保留旧规则")

    def match(self, user_msg: str) -> Optional[List[str]]:
        """
        匹配用户消息，返回命中规则的回复列表；未命中返回 None。
        因为规则已按 priority 降序排列，第一条命中即为最高优先级。
        """
        if not self._enabled or not self._rules:
            return None

        stripped = user_msg.strip()
        for rule in self._rules:
            if rule.match_type == "exact":
                if stripped == rule.pattern:
                    logger.info(f"[RuleChecker] 命中规则 [{rule.id}] {rule.name} (exact)")
                    return rule.reply if rule.reply else None
            elif rule.match_type == "regex":
                if rule.compiled and rule.compiled.search(stripped):
                    logger.info(f"[RuleChecker] 命中规则 [{rule.id}] {rule.name} (regex)")
                    return rule.reply if rule.reply else None
        return None

    def match_all(self, user_msg: str) -> List[str]:
        """
        匹配用户消息，返回所有命中规则的回复列表（按优先级排序）。
        与 match() 不同，match_all 会返回所有命中的规则回复。
        """
        if not self._enabled or not self._rules:
            return []

        stripped = user_msg.strip()
        replies = []
        for rule in self._rules:
            if rule.match_type == "exact":
                if stripped == rule.pattern:
                    logger.info(f"[RuleChecker] 命中规则 [{rule.id}] {rule.name} (exact)")
                    replies.extend(rule.reply)
            elif rule.match_type == "regex":
                if rule.compiled and rule.compiled.search(stripped):
                    logger.info(f"[RuleChecker] 命中规则 [{rule.id}] {rule.name} (regex)")
                    replies.extend(rule.reply)
        return replies
