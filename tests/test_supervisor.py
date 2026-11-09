from live_recorder.monitor.supervisor import worker_start_notice


def test_clean_worker_start_is_info():
    message, severity = worker_start_notice(0, 0)
    assert message == "ワーカーを起動しました"
    assert severity == "info"


def test_worker_start_with_unfinished_jobs_is_warning():
    message, severity = worker_start_notice(2, 1)
    assert message == "未完了の録画 2 件、コメント 1 件がある状態でワーカーを起動しました"
    assert severity == "warning"


def test_worker_start_names_only_the_unfinished_kind():
    message, severity = worker_start_notice(0, 3)
    assert message == "未完了のコメント 3 件がある状態でワーカーを起動しました"
    assert severity == "warning"
