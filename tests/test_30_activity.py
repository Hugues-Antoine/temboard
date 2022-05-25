import logging
from queue import Queue
from time import sleep

import pytest
from sh import ErrorReturnCode, SignalException
from tenacity import (
    Retrying, retry_if_exception_type, wait_fixed, stop_after_delay,
)


logger = logging.getLogger(__name__)


def retry_assert():
    return Retrying(
        retry=retry_if_exception_type(AssertionError),
        stop=stop_after_delay(10),
        wait=wait_fixed(.1),
    )


def test_running(agent_login, browser, pg_sleep, ui_url):
    browser.get(ui_url)  # Goto home
    browser.select("a.instance-link").click()  # Click first instance
    browser.select("div.sidebar a.activity").click()  # Click Activity

    # Pause auto-refresh
    browser.select("td input[type=checkbox]").click()  # Select first process
    # Ensure auto_refresh button is shown
    auto_refresh_resume = browser.select("span#autoRefreshResume")
    assert 'd-none' not in auto_refresh_resume.get_attribute('class')

    td = browser.select('td.query')

    assert 'pg_sleep' in td.text
    assert 'test-activity' in td.text

    browser.select("#killButton").click()  # Click read Terminate
    browser.select("#submitKill").click()  # Confirmation dialog

    # Ensure psql session is killed.
    with pytest.raises(ErrorReturnCode) as ei:
        pg_sleep.wait(timeout=1)

    assert 2 == ei.value.exit_code

    # Ensure processes vanished from view.
    sleep(.1)
    try:
        query = browser.absent("td.query").text
    except Exception:
        pass
    else:
        # If a query is running, ensure it's not pg_sleep.
        assert 'pg_sleep' not in query


def test_lock(browser, pg_lock, registered_agent, ui_url):
    browser.get(ui_url)  # Goto home
    browser.select("a.instance-link").click()  # Click first instance
    browser.select("div.sidebar a.activity").click()  # Click Activity

    browser.select('td.query')  # Wait for queries to appears.

    for attempt in retry_assert():  # Wait for waiting count to come up
        with attempt:
            assert "1" == browser.select("#waiting-count").text

    browser.select(".nav-tabs a.waiting").click()  # Go to waiting tab

    # Pause auto-refresh
    browser.select("td input[type=checkbox]").click()
    # Ensure auto_refresh button is shown
    auto_refresh_resume = browser.select("span#autoRefreshResume")
    assert 'd-none' not in auto_refresh_resume.get_attribute('class')

    td = browser.select('td.query')
    assert 'UPDATE locked_table' in td.text

    assert "1" == browser.select("#blocking-count").text
    browser.select(".nav-tabs a.blocking").click()  # Go to blocking tab

    # Pause auto-refresh
    browser.select("td input[type=checkbox]").click()
    # Ensure auto_refresh button is shown
    auto_refresh_resume = browser.select("span#autoRefreshResume")
    assert 'd-none' not in auto_refresh_resume.get_attribute('class')

    td = browser.select('td.query')
    assert 'LOCK TABLE locked_table' in td.text


@pytest.fixture
def pg_lock(psql, agent_env):
    """Ensure one backend is waiting for another in monitored postgres."""
    EOF = None  # For amoffat/sh stdin Queue.

    locking = psql(_in=Queue(), _bg=True, _iter=True)
    locking.process.stdin.put(
        "CREATE TABLE locked_table AS SELECT generate_series(1, 5);\n")
    assert 'SELECT 5' in next(locking)  # Wait for CREATE to return.
    locking.process.stdin.put("BEGIN;\n")
    locking.process.stdin.put("LOCK TABLE locked_table IN EXCLUSIVE MODE;\n")
    assert locking.is_alive()

    logger.info("Starting waiting process.")
    waiting = psql(_in=Queue(), _bg=True)
    waiting.process.stdin.put(
        "UPDATE locked_table SET generate_series = generate_series + 1;\n")
    waiting.process.stdin.put(EOF)
    assert waiting.is_alive()

    yield None

    locking.process.stdin.put(EOF)
    terminate(locking)
    terminate(waiting)


@pytest.fixture
def pg_sleep(psql):
    """Ensure a backend is running for 30s in monitored Postgres."""
    logger.info("Starting pg_sleep in background.")
    proc = psql(
        c="SELECT pg_sleep(30), 'test-activity';",
        _bg=True)
    assert proc.is_alive()

    yield proc

    logger.info("Stopping pg_sleep.")
    terminate(proc)


def terminate(proc):
    if not proc.is_alive():
        return

    proc.terminate()

    try:
        proc.wait(timeout=5)
    except SignalException:
        pass

    return proc
