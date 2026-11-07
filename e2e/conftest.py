import pytest


# Each phase's report on the test item (rep_setup, rep_call, rep_teardown), so a fixture's
# teardown can tell whether the test failed.
@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    rep = outcome.get_result()
    setattr(item, "rep_" + rep.when, rep)
