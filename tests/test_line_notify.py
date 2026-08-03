import os
import unittest
import urllib.error
from unittest.mock import patch

import line_notify


class _Response:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class LineNotifyRetryTest(unittest.TestCase):
    def test_transient_network_error_is_retried(self):
        env = {
            "LINE_CHANNEL_ACCESS_TOKEN": "token",
            "LINE_USER_ID": "U123",
        }
        with patch.dict(os.environ, env, clear=False), \
                patch.object(
                    line_notify.urllib.request,
                    "urlopen",
                    side_effect=[urllib.error.URLError("temporary"), _Response()],
                ) as urlopen, \
                patch.object(line_notify.time, "sleep") as sleep:
            sent = line_notify.push_text("test", label="test")

        self.assertTrue(sent)
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once_with(5)


if __name__ == "__main__":
    unittest.main()
