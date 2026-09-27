"""Application logging setup.

The application previously declared ``logging.getLogger('app.ai')`` but never attached a
handler, so successful work was invisible: Python's last-resort handler only emits WARNING
and above. That is why a warm model load produced no log line at all.

Only the ``app`` logger tree is configured. The root logger is left alone so that chatty
third-party packages (boto3, urllib3, matplotlib, yolov5) do not flood the container log.
"""
import logging
import sys

LOGGER_NAME = 'app'
_FORMAT = '%(asctime)s %(levelname)s %(name)s %(message)s'
_configured = False


def configure(level: int = logging.INFO) -> logging.Logger:
    """Attach a stdout handler to the ``app`` logger tree, once per process."""
    global _configured
    logger = logging.getLogger(LOGGER_NAME)
    if _configured:
        return logger
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(_FORMAT))
    handler.set_name('ecoguard-stdout')
    logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False
    _configured = True
    return logger
