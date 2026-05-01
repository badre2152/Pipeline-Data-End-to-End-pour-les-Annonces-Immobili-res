import logging
import os

# 🪵 Bug 7 Fix 1: use abspath() to anchor __file__ before dirname()
# This makes LOG_DIR stable regardless of working directory or how
# the pipeline is invoked (cron, Docker, IDE, CLI from any folder).
_THIS_FILE = os.path.abspath(__file__)
LOG_DIR    = os.path.normpath(os.path.join(os.path.dirname(_THIS_FILE), "../../logs"))
LOG_FILE   = os.path.join(LOG_DIR, "pipeline.log")


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)

    formatter = logging.Formatter(
        "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # ── Console handler (always works) ────────────────────────────────────────
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(formatter)
    logger.addHandler(ch)

    # ── File handler (guarded) ────────────────────────────────────────────────
    # 🪵 Bug 7 Fix 2: create the directory here, not at module level,
    # so any failure is associated with a specific logger call, not import time.
    # 🪵 Bug 7 Fix 3: wrap FileHandler in try/except so a bad path degrades
    # gracefully — console logging still works and you get a clear warning.
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(formatter)
        logger.addHandler(fh)
    except Exception as exc:
        logger.warning(
            f"File logging disabled — could not create log file at "
            f"{LOG_FILE!r}: {exc}"
        )

    return logger