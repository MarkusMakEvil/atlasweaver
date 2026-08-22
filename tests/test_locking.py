from pathlib import Path

import pytest

from project_knowledge.locking import ExclusiveFileLock, TransactionLockError


def test_cross_process_style_lock_times_out_without_stealing_active_lock(tmp_path: Path) -> None:
    path = tmp_path / "transaction.lock"
    with ExclusiveFileLock(path, timeout=0.1):
        with pytest.raises(TransactionLockError, match="already active"):
            with ExclusiveFileLock(path, timeout=0.02):
                pass
