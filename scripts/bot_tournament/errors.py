# Shared exception types used by the bot tournament coordinator modules.


class ApiError(Exception):
    pass


class ConfigError(Exception):
    pass


class UhpError(Exception):
    pass
