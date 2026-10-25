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
