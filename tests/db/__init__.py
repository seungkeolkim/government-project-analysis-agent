"""DB 레이어 유닛 테스트 패키지 (Phase 1a).

- test_change_detection.py: 변경 감지 4-branch + 2차 감지 + 리셋·이관 동작.
- test_atomic_rollback.py: 리셋 중 예외 시 UPSERT 도 함께 롤백되는 atomic 경계.
"""
