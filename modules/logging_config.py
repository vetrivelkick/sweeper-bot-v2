"""
Sweeper Bot V2 - Logging Configuration (AUDIT FIX #12)

Provides:
- HumanReadableFormatter: Clean, colored console output for humans
- StructuredJSONFormatter: JSON lines for machine parsing (LOG_JSON=true)
- Correlation ID support: Each trade cycle gets a unique ID propagated through logs
- ContextVar-based correlation: Thread-safe, no need to pass IDs through every function
"""
import json
import logging
import time
import uuid
import os
from contextvars import ContextVar
from typing import Optional

# ContextVar for correlation IDs - thread-safe and async-safe
_correlation_id: ContextVar[Optional[str]] = ContextVar('correlation_id', default=None)
_trade_id: ContextVar[Optional[str]] = ContextVar('trade_id', default=None)
_cycle_id: ContextVar[Optional[str]] = ContextVar('cycle_id', default=None)


def set_correlation_id(cid: Optional[str] = None) -> str:
    cid = cid or uuid.uuid4().hex[:12]
    _correlation_id.set(cid)
    return cid

def get_correlation_id() -> Optional[str]:
    return _correlation_id.get()

def set_trade_id(tid: Optional[str] = None) -> str:
    tid = tid or f"trade_{uuid.uuid4().hex[:8]}"
    _trade_id.set(tid)
    return tid

def get_trade_id() -> Optional[str]:
    return _trade_id.get()

def set_cycle_id(cid: Optional[str] = None) -> str:
    cid = cid or f"cycle_{uuid.uuid4().hex[:8]}"
    _cycle_id.set(cid)
    return cid

def get_cycle_id() -> Optional[str]:
    return _cycle_id.get()

def clear_context():
    _correlation_id.set(None)
    _trade_id.set(None)
    _cycle_id.set(None)


# ANSI color codes for console output
_COLORS = {
    'DEBUG':    '\033[36m',    # cyan
    'INFO':     '\033[32m',    # green
    'WARNING':  '\033[33m',    # yellow
    'ERROR':    '\033[31m',    # red
    'CRITICAL': '\033[1;31m',  # bold red
    'RESET':    '\033[0m',
    'DIM':      '\033[2m',
    'BOLD':     '\033[1m',
}


class HumanReadableFormatter(logging.Formatter):
    """Clean, human-readable log formatter with optional color support."""

    def __init__(self, use_color: bool = True):
        super().__init__()
        self.use_color = use_color

    def format(self, record: logging.LogRecord) -> str:
        # Timestamp: 2026-09-05 15:59:14
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(record.created))

        # Level (padded to 8 chars for alignment)
        level = record.levelname

        # Logger name (shortened: sweeper.main -> main, sweeper.orders -> orders)
        logger_name = record.name.replace("sweeper.", "")

        # Build context string from correlation IDs
        context_parts = []
        cyid = get_cycle_id()
        if cyid:
            context_parts.append(cyid)
        tid = get_trade_id()
        if tid:
            context_parts.append(tid)
        context = f" [{'|'.join(context_parts)}]" if context_parts else ""

        # Message
        msg = record.getMessage()

        # Assemble the log line
        if self.use_color:
            color = _COLORS.get(level, '')
            reset = _COLORS['RESET']
            dim = _COLORS['DIM']
            bold = _COLORS['BOLD']
            line = (
                f"{dim}{timestamp}{reset} "
                f"{color}{level:<8}{reset} "
                f"{dim}[{logger_name}]{reset}"
                f"{dim}{context}{reset} "
                f"{bold if level in ('CRITICAL', 'ERROR') else ''}{msg}{reset}"
            )
        else:
            line = f"{timestamp} {level:<8} [{logger_name}]{context} {msg}"

        # Add exception info if present
        if record.exc_info and record.exc_info[0] is not None:
            line += "\n" + self.formatException(record.exc_info)

        return line


class StructuredJSONFormatter(logging.Formatter):
    """JSON log formatter for machine-parseable structured logging."""

    def format(self, record: logging.LogRecord) -> str:
        log_entry = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.gmtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        cid = get_correlation_id()
        if cid:
            log_entry["correlation_id"] = cid
        tid = get_trade_id()
        if tid:
            log_entry["trade_id"] = tid
        cyid = get_cycle_id()
        if cyid:
            log_entry["cycle_id"] = cyid
        if hasattr(record, 'extra') and isinstance(record.extra, dict):
            log_entry.update(record.extra)
        if record.exc_info and record.exc_info[0] is not None:
            log_entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(log_entry, default=str)


class ContextualLoggerAdapter(logging.LoggerAdapter):
    def process(self, msg, kwargs):
        extra = kwargs.get('extra', {})
        cid = get_correlation_id()
        if cid and 'correlation_id' not in extra:
            extra['correlation_id'] = cid
        tid = get_trade_id()
        if tid and 'trade_id' not in extra:
            extra['trade_id'] = tid
        kwargs['extra'] = extra
        return msg, kwargs


def setup_logging(level: str = "INFO", json_format: bool = False, log_file: str = "logs/sweeper.log"):
    """Configure logging with human-readable or JSON format.

    Args:
        level: Log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        json_format: If True, use JSON formatter; if False (default), use human-readable
        log_file: Path to log file (logs are written to both console and file)
    """
    os.makedirs(os.path.dirname(log_file) or '.', exist_ok=True)

    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Remove existing handlers
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    if json_format:
        console_formatter = StructuredJSONFormatter()
        file_formatter = StructuredJSONFormatter()
    else:
        # Console: colored human-readable; File: plain text human-readable
        console_formatter = HumanReadableFormatter(use_color=True)
        file_formatter = HumanReadableFormatter(use_color=False)

    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(console_formatter)
    root_logger.addHandler(console_handler)

    # File handler
    file_handler = logging.FileHandler(log_file)
    file_handler.setFormatter(file_formatter)
    root_logger.addHandler(file_handler)

    # Reduce noise from libraries
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("web3").setLevel(logging.WARNING)
    logging.getLogger("websocket").setLevel(logging.WARNING)

    return root_logger


def get_logger(name: str) -> logging.Logger:
    if not name.startswith("sweeper"):
        name = f"sweeper.{name}"
    return logging.getLogger(name)
