"""Compatibility wrapper for serial Actisense BST inputs."""

from .nmea2000datasource import NMEA2000DataSource


class ActisenseDataSource(NMEA2000DataSource):
    """Preserve the original Actisense serial source API."""

    def __init__(self, protocol, direction, port, baudrate=230400):
        super().__init__(
            {
                "type": "serial",
                "protocol": protocol,
                "direction": direction,
                "data-port": port,
                "baudrate": baudrate,
            }
        )
