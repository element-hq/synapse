import logging

root_logger = logging.getLogger()


class ExplicitlyConfiguredLogger(logging.Logger):
    """
    A custom logger class that only allows logging if the logger is explicitly
    configured (does not inherit log level from parent).
    """

    def isEnabledFor(self, level: int) -> bool:
        # Check if the logger is explicitly configured
        explicitly_configured_logger = self.manager.loggerDict.get(self.name)

        log_level = logging.NOTSET
        if isinstance(explicitly_configured_logger, logging.Logger):
            log_level = explicitly_configured_logger.level

        # If the logger is not configured, we don't log anything
        if log_level == logging.NOTSET:
            return False

        # Otherwise, follow the normal logging behavior
        return level >= log_level
