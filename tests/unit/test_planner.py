from app.sync.planner import plan_initial_fetches, plan_polling_fetches


def test_plan_initial_fetches_respects_batch_size() -> None:
    episodes = [1, 2, 3]
    assert plan_initial_fetches(episodes, 2) == [1, 2]


def test_plan_polling_fetches_prioritizes_new_then_backlog() -> None:
    planned = plan_polling_fetches(
        new_candidates=[1, 2],
        backlog_candidates=[3, 4, 5],
        backlog_batch_size=2,
    )
    assert planned == [1, 2, 3, 4]
