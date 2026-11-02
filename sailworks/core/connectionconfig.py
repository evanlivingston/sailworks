"""Validate saved instrument settings independently of a GUI toolkit."""

from .nmea2000datasource import N2K_FORMATS


def connection_config(data):
    config = dict(data)
    kind = config.get("type")
    if kind not in ("serial", "network"):
        raise ValueError("Choose a serial or network connection")
    protocol = config.get("protocol", "nmea0183")
    if protocol in N2K_FORMATS:
        config.setdefault("format", protocol)
        protocol = "nmea2000"
    config["protocol"] = protocol
    if protocol not in ("nmea0183", "nmea2000"):
        raise ValueError("Choose an NMEA protocol")
    direction = config.get("direction", "in")
    if direction not in ("in", "out", "both"):
        raise ValueError("Choose an input/output direction")
    if protocol == "nmea2000":
        config.setdefault("format", "actisense")
        if config["format"] not in N2K_FORMATS or direction != "in":
            raise ValueError(
                "NMEA 2000 requires a supported gateway format and input direction"
            )
    config["direction"] = direction
    return _transport_config(config, kind)


def _transport_config(config, kind):
    if kind == "serial":
        config["data-port"] = str(config.get("data-port", "")).strip()
        config["baudrate"] = int(config.get("baudrate", 9600))
        if not config["data-port"] or config["baudrate"] <= 0:
            raise ValueError("Enter a serial port and positive baud rate")
    else:
        config["host"] = str(config.get("host", "")).strip()
        config["port"] = int(config.get("port", 10110))
        if not config["host"] or not 1 <= config["port"] <= 65535:
            raise ValueError("Enter a host and port between 1 and 65535")
        if config.get("network") not in ("tcp", "udp"):
            raise ValueError("Choose TCP or UDP")
    return config


def connection_key(config):
    if config["type"] == "serial":
        return config["data-port"]
    return f"{config['host']}:{config['port']}"
