"""백업 관련 상수 정의."""

# SystemSetting 에 저장되는 설정 키 이름
SETTING_KEY_BACKUP_CRON: str = "backup.cron_expression"
SETTING_KEY_BACKUP_MAX_COUNT: str = "backup.max_count"

# 설정 기본값
DEFAULT_BACKUP_CRON: str = "0 3 * * *"  # KST 03:00 매일
DEFAULT_BACKUP_MAX_COUNT: int = 7

# BackupHistory.trigger 허용 값
BACKUP_TRIGGER_SCHEDULED: str = "scheduled"
BACKUP_TRIGGER_MANUAL: str = "manual"

__all__ = [
    "DEFAULT_BACKUP_CRON",
    "DEFAULT_BACKUP_MAX_COUNT",
    "BACKUP_TRIGGER_MANUAL",
    "BACKUP_TRIGGER_SCHEDULED",
    "SETTING_KEY_BACKUP_CRON",
    "SETTING_KEY_BACKUP_MAX_COUNT",
]
