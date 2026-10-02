"""兼容垫片：从宿主项目抽出的最小工具函数（保持库自包含）。"""

from collections import Counter


def majority_vote(texts: list[str]) -> tuple[str | None, dict]:
    """多数投票（字符串精确匹配版）。返回 (获胜答案或 None, 票型)。"""
    counts = Counter(t.strip() for t in texts if t and t.strip())
    if not counts:
        return None, {}
    top, cnt = counts.most_common(1)[0]
    ties = list(counts.values()).count(cnt)
    return (top if ties == 1 else None), dict(counts)
