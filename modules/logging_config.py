"""Sweeper Bot V2 - Structured JSON Logging (AUDIT FIX #12)

Provides:
- StructuredJSONFormatter: Outputs logs as JSON lines for machine parsing
- Correlation ID support: Each trade cycle gets a unique ID propagated through logs
- ContextVar-based correlation: Thread-safe, no need to pass IDs through every function
"""
import json
import logging
import time
import uuid
from contextvars import ContextVar
from typing import Optional

# ContextVar for correlation IDs - thread-safe and async-safe
_correlation_id: ContextVar[Optional[str]] = ContextVar('correlation_id', default=None)
_trade_id: ContextVar[Optional[str]] = ContextVar('trade_id', default=None)
_cycle_id: ContextVar[Optional[str]] = ContextVar('cycle_id', default=None)


def set_correlation_id(cid: Optional[str] = None) -> str:
    """Set or generate a correlation ID for the current context."""
    cid = cid or uuid.uuid4().hex[:12]
    _correlation_id.set(cid)
    return cid


def get_correlation_id() -> Optional[str]:
    return _correlation_id.get()


def set_trade_id(tid: Optional[str] = None) -> str:
    """Set or generate a trade ID for tracking a specific trade lifecycle."""
    tid = tid or f"trade_{uuid.uuid4().hex[:8]}"
    _trade_id.set(tid)
    return tid


def get_trade_id() -> Optional[str]:
    return _trade_id.get()


def set_cycle_id(cid: Optional[str] = None) -> str:
    """Set or generate a cycle ID for tracking a bot cycle."""
    cid = cid or f"cycle_{uuid.uuid4().hex[:8]}"
    _cycle_id.set(cid)
    return cid


def get_cycle_id() -> Optional[str]:
    return _cycle_id.get()


def clear_context():
    """Clear all correlation IDs (call at end of cycle/trade)."""
    _correlation_id.set(None)
    _trade_id.set(None)
    _cycle_id.set(None)


class StructuredJSONFormatter(logging.Formatter):
    """JSON log formatter for machine-parseable structured logging.
    
    Each log line is a JSON object with:
    - timestamp (ISO 8601)
    - level (INFO/WARNING/ERROR/CRITICAL)
    - logger (module name)
    - message (log message)
    - correlation_id (if set)
    - trade_id (if set)
    - cycle_id (if set)
    - extra fields (any kwargs passed to logger)
    """

    def format(self, record: logging.LogRecord) -> str:
        log_entry = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.gmtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        
        # Add correlation IDs from context
        cid = get_correlation_id()
        if cid:
            log_entry["correlation_id"] = cid
        
        tid = get_trade_id()
        if tid:
            log_entry["trade_id"] = tid
        
        cyid = get_cycle_id()
        if cyid:
            log_entry["cycle_id"] = cyid
        
        # Add extra fields from record
        if hasattr(record, 'extra') and isinstance(record.extra, dict):
            log_entry.update(record.extra)
        
        # Add exception info if present
        if record.exc_info and record.exc_info[0] is not None:
            log_entry["exception"] = self.formatException(record.exc_info)
        
        return json.dumps(log_entry, default=str)


class ContextualLoggerAdapter(logging.LoggerAdapter):
    """Logger adapter that injects correlation IDs into log records."""
    
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


def setup_logging(level: str = "INFO", json_format: bool = True, log_file: str = "logs/sweeper.log"):
    """Configure logging with structured JSON or text format.
    
    Args:
        level: Log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        json_format: If True, use JSON formatter; if False, use text formatter
        log_file: Path to log file (logs are written to both console and file)
    """
    import os
    os.makedirs(os.path.dirname(log_file) or '.', exist_ok=True)
    
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    
    # Remove existing handlers
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)
    
    if json_format:
        formatter = StructuredJSONFormatter()
    else:
        formatter = logging.Formatter(
            "%(asctime)s [%(name)s] %(levelname)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"
        )
    
    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)
    
    # File handler
    file_handler = logging.FileHandler(log_file)
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)
    
    # Reduce noise from libraries
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("web3").setLevel(logging.WARNING)
    logging.getLogger("websocket").setLevel(logging.WARNING)
    
    return root_logger


def get_logger(name: str) -> logging.Logger:
    """Get a logger with the sweeper namespace."""
    if not name.startswith("sweeper"):
        name = f"sweeper.{name}"
    return logging.getLogger(name)
