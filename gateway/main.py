"""The gateway: read the plant, decide what matters, write it, survive an outage.

    softplc ──Modbus──┐
                      ├──► deadband ──► spool ──► InfluxDB
    softplc ──OPC UA──┘        │
                               └──────────► Couchbase (events, occasionally)

The gateway is the only component that speaks every protocol, and it is
deliberately the *thinnest* one. It holds no state worth losing: the plant holds
the physics, the spool holds the data, the contract holds the meaning. If this
process is killed, the next one starts and carries on from the spool. That is the
whole design goal, and it is why the interesting logic lives in modules that can
be tested without a socket.

## One event loop, two protocols, one thread

OPC UA is asyncio. pymodbus's TCP server is a blocking thread with no
asynchronous form. Rather than fight it, the gateway runs the asyncio loop for
OPC UA and hands the Modbus client to a worker thread, with a queue between them.
The scan loop is single-threaded and the queue is the only shared state, which
keeps the deadband and the spool free of locks — they are not contended, and a
lock there would be a claim otherwise.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import signal
import time
from dataclasses import dataclass, field
from typing import Any

from softplc.contract import contract as get_contract
from storage.influx.writer import InfluxWriter, make_transport

from gateway.clients.modbus_client import ModbusLinkDownError, ModbusReader
from gateway.clients.opcua_client import OpcUaReader
from gateway.deadband import Deadband
from gateway.spool.store import Spool, SpoolRecord

log = logging.getLogger("gateway")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


@dataclass
class GatewayConfig:
    contract: Any = None
    modbus_host: str = "softplc"
    modbus_port: int = 5020
    opcua_endpoint: str = "opc.tcp://softplc:4840/wwtp/server/"
    spool_dir: str = "/srv/spool"
    spool_max_mb: int = 512
    poll_interval_s: float = 1.0
    deadband_default: float = 0.0
    influx_url: str = "http://influxdb:8086"
    influx_token: str = ""
    influx_database: str = "wwtp"
    influx_org: str = "wwtp"
    #: Read Modbus, OPC UA, or both. Both is the default because agreement
    #: between the two is the cheapest cross-check available, and disagreement is
    #: either a word-order bug or a wiring bug.
    use_modbus: bool = True
    use_opcua: bool = True


@dataclass
class GatewayStats:
    polls: int = 0
    offered: int = 0
    published: int = 0
    spooled: int = 0
    dropped_overflow: int = 0
    modbus_failures: int = 0
    opcua_failures: int = 0
    started: float = field(default_factory=time.time)

    def as_dict(self) -> dict[str, Any]:
        return {
            "polls": self.polls,
            "offered": self.offered,
            "published": self.published,
            "spooled": self.spooled,
            "dropped_overflow": self.dropped_overflow,
            "modbus_failures": self.modbus_failures,
            "opcua_failures": self.opcua_failures,
            "uptime_s": round(time.time() - self.started, 1),
        }


class Gateway:
    """The read path, the filter, the spool and the writer."""

    def __init__(self, config: GatewayConfig | None = None) -> None:
        self.config = config or GatewayConfig()
        self.c = self.config.contract or get_contract()
        self.deadband = Deadband.from_contract(self.c, self.config.deadband_default)
        self.spool = Spool(self.config.spool_dir, max_mb=self.config.spool_max_mb)
        self.stats = GatewayStats()

        self.modbus: ModbusReader | None = None
        self.opcua: OpcUaReader | None = None
        self.writer: InfluxWriter | None = None
        self._running = False

    # ─── lifecycle ───────────────────────────────────────────────────────────

    async def start(self) -> None:
        if self.config.influx_token:
            self.writer = InfluxWriter(
                self.c,
                make_transport(self.config.influx_url, self.config.influx_token,
                               org=self.config.influx_org),
                database=self.config.influx_database,
            )
        else:
            # No token is not an error at startup: the spool still absorbs
            # everything, and a gateway that refuses to run without a database
            # would throw away the one guarantee it exists to provide.
            log.warning("no INFLUX_TOKEN; spooling only, nothing will be written")

        if self.config.use_modbus:
            self.modbus = ModbusReader(
                self.c, self.config.modbus_host, self.config.modbus_port
            )
            if self.modbus.connect():
                log.info("modbus plan: %d requests for %d registers",
                         self.modbus.request_count, self.modbus.planned_registers)
                for line in self.modbus.describe_plan():
                    log.debug("  %s", line)
            else:
                log.warning("modbus not reachable at %s:%d — will retry",
                            self.config.modbus_host, self.config.modbus_port)

        if self.config.use_opcua:
            self.opcua = OpcUaReader(self.c, endpoint=self.config.opcua_endpoint)
            if await self.opcua.connect():
                log.info("opcua resolved %d/%d signals", self.opcua.resolved_count,
                         len(self.c.signals))
            else:
                log.warning("opcua not reachable at %s", self.config.opcua_endpoint)

        self._running = True

    async def stop(self) -> None:
        self._running = False
        self._drain()
        if self.writer is not None:
            self.writer.flush()
        if self.modbus is not None:
            self.modbus.close()
        if self.opcua is not None:
            await self.opcua.close()
        self.spool.close()
        log.info("gateway stopped: %s", self.stats.as_dict())

    # ─── the cycle ───────────────────────────────────────────────────────────

    async def poll_once(self) -> int:
        """One pass over both protocols. Returns the number of points published."""
        self.stats.polls += 1
        readings: dict[str, float] = {}
        quality: dict[str, int] = {}

        if self.modbus is not None and self.modbus.connected:
            try:
                result = self.modbus.poll()
                for name, value in result.values.items():
                    sid = _signal_for(name, self.c)
                    if sid is None:
                        # A register that is not a measurement: a heartbeat, a
                        # fault code, half of a 32-bit runtime. Publishing it
                        # would put a number in a series nothing is watching.
                        continue
                    readings[sid] = value
                    quality[sid] = result.quality.get(name, 0)
            except ModbusLinkDownError as exc:
                self.stats.modbus_failures += 1
                log.error("modbus link down: %s", exc)
                self.modbus.close()

        if self.opcua is not None and self.opcua.connected:
            result = await self.opcua.poll()
            if result.failed == len(result.quality) and result.quality:
                self.stats.opcua_failures += 1
            for sid, value in result.values.items():
                # OPC UA wins on conflict. It carries a StatusCode and Modbus
                # does not, so when the two disagree the better-informed answer
                # is the one that knows whether it is valid.
                readings[sid] = value
                quality[sid] = result.quality.get(sid, 0)

        return self._publish(readings, quality)

    def _publish(self, readings: dict[str, float],
                 quality: dict[str, int]) -> int:
        """Filter, spool, and hand to the writer."""
        now_ms = int(time.time() * 1000)
        accepted: list[SpoolRecord] = []
        for signal_id, value in readings.items():
            q = quality.get(signal_id, 0)
            self.stats.offered += 1
            if not self.deadband.accept(signal_id, value, q):
                continue
            self.stats.published += 1
            accepted.append(SpoolRecord(
                ts=now_ms // 1000, signal=signal_id, value=value, quality=q
            ))

        if accepted:
            kept = self.spool.extend(accepted)
            self.stats.spooled += kept
            self.stats.dropped_overflow += len(accepted) - kept
        self.spool.flush()
        self._drain()
        return len(accepted)

    def _drain(self) -> int:
        """Send complete spool files to the writer.

        At-least-once: the file is removed by ``drain()`` only after its records
        have been handed over, so a crash between the write and the delete
        re-delivers rather than loses. Duplicates in a time series are a nuisance;
        a silent gap is a lie.
        """
        if self.writer is None:
            return 0
        sent = 0
        for path in self.spool.pending():
            records = list(self.spool.read(path))
            if not records:
                self.spool.acknowledge(path)
                continue
            for r in records:
                if self.writer.add(r.signal, r.value, r.ts * 1000, r.quality):
                    sent += 1
            if self.writer.flush():
                self.spool.acknowledge(path)
            else:
                # Leave the file. The writer's own failure counter is the record
                # of why, and the records are still on disk.
                break
        return sent

    # ─── running ─────────────────────────────────────────────────────────────

    async def run(self, iterations: int | None = None,
                  report_every: float = 30.0) -> None:
        n = 0
        next_report = time.monotonic() + report_every
        while self._running and (iterations is None or n < iterations):
            try:
                await self.poll_once()
            except Exception:  # the loop outlives any one poll
                log.exception("poll failed")
            n += 1
            if self.writer is not None and self.writer.due():
                self.writer.flush()
            if time.monotonic() >= next_report:
                self._report()
                next_report = time.monotonic() + report_every
            await asyncio.sleep(self.config.poll_interval_s)

    def _report(self) -> None:
        offered = self.stats.offered
        published = self.stats.published
        ratio = (1.0 - published / offered) if offered else 0.0
        log.info(
            "polls=%d offered=%d published=%d (%.0f%% filtered) spool=%d files "
            "influx=%s modbus_err=%d opcua_err=%d",
            self.stats.polls, offered, published, ratio * 100,
            self.spool.stats.files, self.writer.stats_dict() if self.writer else "-",
            self.stats.modbus_failures, self.stats.opcua_failures,
        )

    def health(self) -> dict[str, Any]:
        return {
            "running": self._running,
            "modbus": bool(self.modbus and self.modbus.connected),
            "opcua": bool(self.opcua and self.opcua.connected),
            "stats": self.stats.as_dict(),
            "spool": self.spool.stats.as_dict(),
            "influx": self.writer.stats_dict() if self.writer else None,
            "deadband": {
                "offered": sum(self.deadband.offered_since_start().values()),
                "published": sum(self.deadband.accepted_since_start().values()),
            },
        }


def _signal_for(register_name: str, contract: Any) -> str | None:
    """Map a Modbus register name to the signal it carries.

    Answered by the contract's own ``signal:`` field, which is what the register
    map was missing until Phase 3 closed. There is deliberately no fallback: an
    earlier version substring-matched equipment names, and that is right by luck
    and wrong by design — a register is a *view* of a subset of signals, so the
    relation is many-to-one or one-to-none and cannot be derived from names at
    all.

    ``None`` means "this register is not a measurement" — a heartbeat, a fault
    code, a state bitfield, half of a 32-bit value. Returning ``None`` for those
    is the correct answer, not a gap to be papered over.
    """
    signal_id: str | None = contract.register(register_name).signal
    return signal_id


# ─── entrypoint ───────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="gateway", description="Read the plant and store what matters."
    )
    p.add_argument("--modbus-host", default=os.environ.get("MODBUS_HOST", "softplc"))
    p.add_argument("--modbus-port", type=int,
                   default=_env_int("MODBUS_PORT", 5020))
    p.add_argument("--opcua-endpoint",
                   default=os.environ.get(
                       "OPCUA_ENDPOINT", "opc.tcp://softplc:4840/wwtp/server/"))
    p.add_argument("--spool-dir", default=os.environ.get("GATEWAY_SPOOL_DIR",
                                                         "/srv/spool"))
    p.add_argument("--spool-max-mb", type=int,
                   default=_env_int("GATEWAY_SPOOL_MAX_MB", 512))
    p.add_argument("--poll-interval", type=float, default=1.0)
    p.add_argument("--deadband-default", type=float,
                   default=_env_float("GATEWAY_DEADBAND_DEFAULT", 0.0))
    p.add_argument("--no-modbus", action="store_true")
    p.add_argument("--no-opcua", action="store_true")
    p.add_argument("--iterations", type=int, default=None,
                   help="stop after N polls; for smoke tests")
    p.add_argument("--report-every", type=float, default=30.0)
    p.add_argument("--log-level", default=os.environ.get("GATEWAY_LOG_LEVEL",
                                                         "INFO"))
    return p


async def _run(args: argparse.Namespace) -> int:
    config = GatewayConfig(
        modbus_host=args.modbus_host,
        modbus_port=args.modbus_port,
        opcua_endpoint=args.opcua_endpoint,
        spool_dir=args.spool_dir,
        spool_max_mb=args.spool_max_mb,
        poll_interval_s=args.poll_interval,
        deadband_default=args.deadband_default,
        influx_url=os.environ.get("INFLUX_URL", "http://influxdb:8086"),
        influx_token=os.environ.get("INFLUX_TOKEN", ""),
        influx_database=os.environ.get("INFLUX_DATABASE", "wwtp"),
        influx_org=os.environ.get("INFLUX_ORG", "wwtp"),
        use_modbus=not args.no_modbus,
        use_opcua=not args.no_opcua,
    )
    gw = Gateway(config)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    await gw.start()
    runner = asyncio.create_task(
        gw.run(iterations=args.iterations, report_every=args.report_every)
    )
    try:
        await stop.wait()
    finally:
        gw._running = False
        runner.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await runner
        await gw.stop()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-5s %(name)s %(message)s",
    )
    logging.getLogger("asyncua").setLevel(logging.ERROR)
    logging.getLogger("pymodbus").setLevel(logging.WARNING)
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
