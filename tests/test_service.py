from scripts import service


def test_plist_runs_the_loop_from_the_repo_with_a_log_file():
    p = service.plist()
    args = p["ProgramArguments"]
    assert args[:2] == ["/usr/bin/caffeinate", "-s"]                        # awake only on AC power
    assert args[-3:] == ["-m", "pipelines.ingest_odds", "--loop"]
    assert args[2].endswith(".venv/bin/python")
    assert p["WorkingDirectory"] == str(service.ROOT)
    assert p["KeepAlive"] is True and p["RunAtLoad"] is True and p["ThrottleInterval"] >= 60
    assert p["EnvironmentVariables"]["LOG_FILE"].endswith("data/logs/ingest.log")


def test_service_path_includes_sbin_for_system_tools():
    assert "/usr/sbin" in service.plist()["EnvironmentVariables"]["PATH"].split(":")


def test_ui_warns_when_not_reading_live_data(monkeypatch, tmp_path):
    from app import ui
    monkeypatch.setenv("UI_SNAPSHOT_PATH", str(tmp_path / "demo.duckdb"))
    assert "test data" in ui.live_data_warning()
    live = tmp_path / "ui_snapshot.duckdb"
    live.write_text("x")
    monkeypatch.setenv("UI_SNAPSHOT_PATH", str(live))
    assert ui.live_data_warning() is None
