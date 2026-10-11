import re
from datetime import datetime, timezone

from analyst_agent.tools.clock import current_time


def test_current_time_is_now_in_utc():
    now = current_time()
    assert re.fullmatch(r"\w+day \d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC", now)
    assert datetime.now(timezone.utc).strftime("%Y-%m-%d") in now
