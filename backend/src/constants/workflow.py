from enum import StrEnum


class Role(StrEnum):
    OPERATOR = 'operator'
    REVIEWER = 'reviewer'
    VIEWER = 'viewer'
    DUTY = 'duty'


class Status(StrEnum):
    QUEUED = 'queued'
    RUNNING = 'running'
    COMPLETE = 'complete'
    FAILED = 'failed'


class RecoveryOutcome(StrEnum):
    RESTORED = 'restored'
    ALREADY_COMPLETE = 'already_complete'
    CONFLICT = 'conflict'
    RECOVERABLE = 'recoverable'
    NOT_FOUND = 'not_found'
