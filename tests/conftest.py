"""Point config at a throwaway DB before any project module imports."""
import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="report_bot_tests_")
os.environ.setdefault("DB_PATH", os.path.join(_tmp, "test_reports.db"))
os.environ.setdefault("PDF_DOWNLOAD_DIR", os.path.join(_tmp, "pdfs"))
# analyst_log.strike_check prices strikes from Yahoo; tests stub it
os.environ.setdefault("STRIKE_CHECK_OFFLINE", "1")
