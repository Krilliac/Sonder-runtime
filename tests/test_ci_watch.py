from scripts import ci_watch


def test_load_and_format_runs():
    runs = ci_watch.load_runs(
        """
        [
          {
            "databaseId": 1,
            "name": "ci",
            "workflowName": "ci",
            "status": "completed",
            "conclusion": "success",
            "headSha": "abcdef123",
            "url": "https://example.test/1"
          },
          {
            "databaseId": 2,
            "name": "build-apps",
            "workflowName": "build-apps",
            "status": "completed",
            "conclusion": "failure",
            "headSha": "123456789",
            "url": "https://example.test/2"
          },
          {
            "databaseId": 3,
            "name": "deploy",
            "workflowName": "deploy",
            "status": "in_progress",
            "conclusion": "",
            "headSha": "987654321",
            "url": "https://example.test/3"
          }
        ]
        """
    )

    assert ci_watch.summarize(runs) == {
        "total": 3,
        "failing": 1,
        "running": 1,
        "successful": 1,
    }
    out = ci_watch.format_runs(runs)
    assert "build-apps" in out
    assert "completed/failure" in out
    assert "abcdef1" in out
