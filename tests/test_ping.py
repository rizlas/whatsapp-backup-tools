"""Tests for wab_archiver.ping (--ping-url). No real network access."""

from unittest.mock import MagicMock, patch

import pytest
import wab_archiver.main as wa
from wab_archiver import ping

URL = "http://kuma.local:3001/api/push/TOKEN"


class TestBuildUrl:
    def test_success(self):
        assert ping.build_url(URL, 0) == f"{URL}?status=up&msg=OK"

    def test_failure_carries_exit_code(self):
        assert ping.build_url(URL, 2) == f"{URL}?status=down&msg=wab-archiver+exit+2"

    def test_existing_query_kept(self):
        assert ping.build_url(f"{URL}?ping=", 0) == f"{URL}?ping=&status=up&msg=OK"


class TestExitCode:
    @pytest.mark.parametrize(
        "exc,code",
        [
            (None, 0),
            (SystemExit(None), 0),
            (SystemExit(0), 0),
            (SystemExit(2), 2),
            (SystemExit("message"), 1),
            (RuntimeError("boom"), 1),
            (KeyboardInterrupt(), 1),
        ],
    )
    def test_mapping(self, exc, code):
        assert ping.exit_code(exc) == code


class TestSend:
    def test_ok(self, logger):
        resp = MagicMock(status=200)
        resp.__enter__.return_value = resp
        with patch("urllib.request.urlopen", return_value=resp) as op:
            assert ping.send(URL, 0, logger) is True
        assert op.call_args.args[0] == f"{URL}?status=up&msg=OK"

    def test_network_error_is_swallowed(self, logger):
        with patch("urllib.request.urlopen", side_effect=OSError("unreachable")):
            assert ping.send(URL, 0, logger) is False


def _main(tmp_path, *extra, run_effect=None):
    argv = ["wa", "-o", str(tmp_path), "--wa-root", str(tmp_path / "wa"), *extra]
    with (
        patch("sys.argv", argv),
        patch.object(wa, "run_forward_mode", side_effect=run_effect) as run,
        patch.object(ping, "send") as send,
    ):
        try:
            wa.main()
        except SystemExit:
            pass
        except RuntimeError:
            pass
    return run, send


class TestMainIntegration:
    def test_ping_up_after_success(self, tmp_path):
        _, send = _main(tmp_path, "--ping-url", URL)
        assert send.call_args.args[:2] == (URL, 0)

    def test_ping_down_on_prune_abort(self, tmp_path):
        _, send = _main(tmp_path, "--ping-url", URL, run_effect=SystemExit(2))
        assert send.call_args.args[:2] == (URL, 2)

    def test_ping_down_on_crash_and_error_propagates(self, tmp_path):
        argv = ["wa", "-o", str(tmp_path), "--wa-root", "/wa", "--ping-url", URL]
        with (
            patch("sys.argv", argv),
            patch.object(wa, "run_forward_mode", side_effect=RuntimeError("boom")),
            patch.object(ping, "send") as send,
            pytest.raises(RuntimeError),
        ):
            wa.main()
        assert send.call_args.args[:2] == (URL, 1)

    def test_no_ping_without_url(self, tmp_path):
        _, send = _main(tmp_path)
        send.assert_not_called()

    def test_no_ping_on_dry_run(self, tmp_path):
        _, send = _main(tmp_path, "--ping-url", URL, "--dry-run")
        send.assert_not_called()
