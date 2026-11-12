
from live_recorder.db.runtime import SettingsValidationError, free_bytes_from_gib, validate_runtime
from live_recorder.domain.enums import (
    CommentJobStatus,
    LiveStatus,
    RecordingStatus,
    TransitionError,
    recording_terminal_path,
    transition_comments,
    transition_recording,
)
from live_recorder.monitor.planner import effective_interval, plan_observation, recovery_action
from live_recorder.platforms.types import LiveObservation


def test_recording_transitions_and_terminal_path():
    assert transition_recording(RecordingStatus.PENDING, RecordingStatus.STARTING) == RecordingStatus.STARTING
    try:
        transition_recording(RecordingStatus.COMPLETED, RecordingStatus.RECORDING)
        raise AssertionError("terminal state accepted a new status")
    except TransitionError:
        pass
    assert recording_terminal_path(RecordingStatus.RECORDING, RecordingStatus.PARTIAL) == [
        RecordingStatus.FINALIZING,
        RecordingStatus.PARTIAL,
    ]
    assert transition_comments(CommentJobStatus.COLLECTING, CommentJobStatus.RECONNECTING) == CommentJobStatus.RECONNECTING


def test_live_starts_jobs_independently():
    observation = LiveObservation(status=LiveStatus.LIVE, platform_stream_id="1")
    both = plan_observation(
        monitoring_enabled=True,
        recording_enabled=True,
        comments_enabled=True,
        comments_global=True,
        observation=observation,
        has_open_session=False,
        open_stream_id=None,
        recording_active=False,
        comments_active=False,
        recording_slots_left=3,
        disk_ok=True,
    )
    assert both.session == "open"
    assert both.start_recording and both.start_comments
    comments_only = plan_observation(
        monitoring_enabled=True,
        recording_enabled=True,
        comments_enabled=True,
        comments_global=True,
        observation=observation,
        has_open_session=True,
        open_stream_id="1",
        recording_active=False,
        comments_active=False,
        recording_slots_left=0,
        disk_ok=False,
    )
    assert not comments_only.start_recording
    assert comments_only.start_comments
    assert comments_only.note == "disk_low"


def test_offline_is_not_confused_with_error():
    offline = plan_observation(
        monitoring_enabled=True,
        recording_enabled=True,
        comments_enabled=True,
        comments_global=True,
        observation=LiveObservation(status=LiveStatus.OFFLINE),
        has_open_session=True,
        open_stream_id="1",
        recording_active=True,
        comments_active=True,
        recording_slots_left=1,
        disk_ok=True,
    )
    assert offline.session == "end"
    assert offline.stop_recording and offline.stop_comments
    unknown = plan_observation(
        monitoring_enabled=True,
        recording_enabled=True,
        comments_enabled=True,
        comments_global=True,
        observation=LiveObservation(status=LiveStatus.ERROR, error="timeout"),
        has_open_session=True,
        open_stream_id="1",
        recording_active=True,
        comments_active=True,
        recording_slots_left=1,
        disk_ok=True,
    )
    assert unknown.session == "keep"
    assert not unknown.stop_recording
    stopped = plan_observation(
        monitoring_enabled=True,
        recording_enabled=False,
        comments_enabled=False,
        comments_global=True,
        observation=LiveObservation(status=LiveStatus.ERROR, error="timeout"),
        has_open_session=True,
        open_stream_id="1",
        recording_active=True,
        comments_active=True,
        recording_slots_left=1,
        disk_ok=True,
        recording_open=True,
        comments_open=True,
    )
    assert stopped.session == "keep"
    assert stopped.stop_recording and stopped.stop_comments
    assert not stopped.start_recording and not stopped.start_comments


def test_restricted_stream_is_not_recorded():
    plan = plan_observation(
        monitoring_enabled=True,
        recording_enabled=True,
        comments_enabled=True,
        comments_global=True,
        observation=LiveObservation(status=LiveStatus.LIVE, restricted=True, platform_stream_id="9"),
        has_open_session=False,
        open_stream_id=None,
        recording_active=False,
        comments_active=False,
        recording_slots_left=3,
        disk_ok=True,
    )
    assert plan.session == "open"
    assert not plan.start_recording
    assert not plan.start_comments
    assert plan.note == "restricted"


def test_interval_respects_twitcasting_budget():
    interval, warning = effective_interval(60, 0)
    assert interval == 60 and warning is None
    widened, warning = effective_interval(30, 40)
    assert widened > 30
    assert warning


def test_recovery_does_not_invent_success():
    assert recovery_action(still_live=None, has_media=True) == "wait"
    assert recovery_action(still_live=False, has_media=True) == "partial"
    assert recovery_action(still_live=True, has_media=False) == "resume"


def test_settings_reject_unbounded_retention_and_disk_space():
    try:
        validate_runtime({"retention_days": 10**9})
        raise AssertionError("huge retention was accepted")
    except SettingsValidationError:
        pass
    try:
        free_bytes_from_gib("1e309")
        raise AssertionError("non-finite free space was accepted")
    except SettingsValidationError:
        pass
    assert free_bytes_from_gib("1") == 1024**3


def test_settings_reject_too_fast_and_bad_engine_order():
    try:
        validate_runtime({"monitor_interval_seconds": 5})
        raise AssertionError("short interval was accepted")
    except SettingsValidationError:
        pass
    try:
        validate_runtime({"tiktok_engine_order": ["streamlink", "streamlink"]})
        raise AssertionError("duplicate engine was accepted")
    except SettingsValidationError:
        pass
    runtime = validate_runtime({"display_timezone": "Asia/Tokyo", "monitor_interval_seconds": 60})
    assert runtime.display_timezone == "Asia/Tokyo"


def test_replace_session_when_stream_id_changes():
    plan = plan_observation(
        monitoring_enabled=True,
        recording_enabled=True,
        comments_enabled=False,
        comments_global=True,
        observation=LiveObservation(status=LiveStatus.LIVE, platform_stream_id="new"),
        has_open_session=True,
        open_stream_id="old",
        recording_active=True,
        comments_active=False,
        recording_slots_left=0,
        disk_ok=True,
    )
    assert plan.session == "replace"
    assert plan.stop_recording
    assert plan.start_recording


def test_monitoring_off_ends_the_open_session():
    plan = plan_observation(
        monitoring_enabled=False,
        recording_enabled=True,
        comments_enabled=True,
        comments_global=True,
        observation=LiveObservation(status=LiveStatus.LIVE, platform_stream_id="1"),
        has_open_session=True,
        open_stream_id="1",
        recording_active=True,
        comments_active=True,
        recording_slots_left=1,
        disk_ok=True,
    )
    assert plan.session == "end"
    assert plan.stop_recording and plan.stop_comments
    assert not plan.start_recording and not plan.start_comments


def test_recording_off_stops_only_the_recording():
    plan = plan_observation(
        monitoring_enabled=True,
        recording_enabled=False,
        comments_enabled=True,
        comments_global=True,
        observation=LiveObservation(status=LiveStatus.LIVE, platform_stream_id="1"),
        has_open_session=True,
        open_stream_id="1",
        recording_active=True,
        comments_active=True,
        recording_slots_left=1,
        disk_ok=True,
        recording_open=True,
        comments_open=True,
    )
    assert plan.session == "keep"
    assert plan.stop_recording
    assert not plan.stop_comments
    assert not plan.start_recording


def test_restart_gap_starts_at_the_captured_end():
    from datetime import datetime, timedelta, timezone

    from live_recorder.monitor.planner import restart_gap_start

    started = datetime(2026, 11, 11, 1, 0, tzinfo=timezone.utc)
    now = started + timedelta(hours=2, seconds=10)
    assert restart_gap_start(started, 7_200_000, now) == started + timedelta(hours=2)
    assert restart_gap_start(None, None, now) == now
