"""结算窗口领域错误。"""

from __future__ import annotations


class SettlementError(Exception):
    """结算窗口业务约束错误（映射为 4xx）。"""


class WindowNotFoundError(SettlementError):
    pass


class WindowAlreadyExistsError(SettlementError):
    pass


class InvalidStageError(SettlementError):
    pass


class WindowStateError(SettlementError):
    pass


class DeadlinePassedError(SettlementError):
    pass


class ResponsibilityError(SettlementError):
    pass


class RevisionConflictError(SettlementError):
    pass
