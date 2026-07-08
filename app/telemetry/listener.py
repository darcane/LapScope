"""Asyncio UDP endpoint receiving FH6 Data Out packets."""

from __future__ import annotations

import asyncio
import binascii
import logging
import time

from .packet import PACKET_SIZE, parse

log = logging.getLogger("lapscope.udp")


class TelemetryProtocol(asyncio.DatagramProtocol):
    def __init__(self, hub, tracker) -> None:
        self.hub = hub
        self.tracker = tracker
        self._size_warned = False
        self._source_logged = False

    def datagram_received(self, data: bytes, addr) -> None:
        now = time.time()
        hub = self.hub
        hub.last_packet_time = now
        hub.last_packet_size = len(data)

        if len(data) != PACKET_SIZE:
            hub.bad_packets += 1
            if not self._size_warned:
                log.warning(
                    "Unexpected packet size %d from %s (expected %d) - the game's "
                    "format may have changed. First 64 bytes: %s",
                    len(data), addr, PACKET_SIZE, binascii.hexlify(data[:64]).decode(),
                )
                self._size_warned = True
            return

        hub.packets_total += 1
        if not self._source_logged:
            log.info("Receiving telemetry from %s:%d", addr[0], addr[1])
            self._source_logged = True

        frame = parse(data)
        try:
            extras = self.tracker.on_frame(now, data, frame)
        except Exception:
            log.exception("Recorder failed on frame; telemetry stream continues")
            # keep the documented frame shape (ARCHITECTURE.md, WS contract)
            extras = {"session_id": None, "delta": None, "session_best": None,
                      "lap_elapsed": None, "race_mode": False}
        frame.update(extras)
        frame["_t"] = now
        hub.publish(frame)

    def error_received(self, exc: Exception) -> None:
        log.debug("UDP error: %s", exc)
