"""DataUpdateCoordinator: definitions once, values on a cycle (HTTP-JSON)."""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .client import EbusdClient, EbusdError
from .const import DOMAIN
from .model import (
    FieldDesc,
    parse_ages,
    parse_decode_errors,
    parse_global,
    parse_values,
)
from .schedule_store import HeatingScheduleStore

_LOGGER = logging.getLogger(__name__)

# A message is force-refreshed once it's older than this. Deliberately VERY
# generous: top-up only fetches values that NO master polls -- config/counters
# that barely change. Set too low, hundreds of values would permanently count
# as "stale", the catch-up backlog would then stay constantly full and each
# cycle's time budget would run out ("time budget ... exhausted" in the log)
# -> filling up would stay sluggish. At 30 min the backlog shrinks to almost
# zero; static values being 30 min old is uncritical. Live values run over
# `fast`/native bus traffic and stay fresher anyway, so they're never touched
# by top-up.
_SELF_MAINTAINED_S = 1800
# Forced bus reads per cycle: many while there's a backlog to catch up on,
# only the baseline afterwards. ebusd runs them in a blocking way, hence capped.
_TOPUP_MAX = 30
_TOPUP_MIN = 8
_TOPUP_PER_BACKLOG = 5  # one more read per this many open messages
# Forced reads run with limited PARALLELISM: ebusd serializes the bus itself,
# the parallelism only overlaps HTTP/arbitration wait times -> noticeably
# faster catch-up. Kept small so as not to overrun ebusd/the bus.
_REFRESH_CONCURRENCY = 3
# Fraction of the cycle that catch-up may use in a blocking way. Generous
# during the one-off fill-up (large backlog); once the backlog is stable,
# the budget is barely touched anyway.
_REFRESH_BUDGET_FRAC = 0.8
# How often definitions are re-synced. Messages newly loaded into ebusd
# (after a config change) are then picked up on their own -- without an
# integration reload.
_DEF_REFRESH_S = 600
# Messages that return a decode error this many times in a row (a response
# arrived, but doesn't match the CSV definition) are taken out of the top-up
# rotation: otherwise they'd cost a failed read + log error every cycle.
# Deliberately based on the decode-error flag (deterministic), NOT on "no
# value" -- a coupler timeout (e.g. wp1) returns no value but is NOT a decode
# error and must not get a genuine message filtered out.
_MAX_DECODE_FAILS = 3
# How often filtered-out ("dead") messages are read again. If one decodes
# again (e.g. because the CSV definition was fixed), it's automatically
# revived -- without an integration reload.
_REVIVE_S = 3600
# Readable messages that ebusd has NEVER read (no lastup, no value) are
# actively kicked off -- with a small, GUARANTEED quota per cycle (before the
# stale top-up). Small, because forced bus reads are blocking and timeouts
# (coupler/not implemented) would otherwise slow down the bus; guaranteed,
# because filling up would otherwise starve as soon as `_stale` (outdated
# values) fills the cycle. Gives up after a few unsuccessful attempts.
_UNREAD_MAX_TRIES = 3
_UNREAD_PER_CYCLE = 3


class EbusdCoordinator(DataUpdateCoordinator[dict[tuple[str, str, str], Any]]):
    """`fields` = descriptors (fixed), `data` = current values per field."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: EbusdClient,
        fields: list[FieldDesc],
        device_meta: dict[str, dict[str, str]],
        scan_interval: int,
        exclude: list[str],
        entry_id: str,
        host: str,
        fast: list[str] | None = None,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=scan_interval),
        )
        self.client = client
        self.fields = fields
        self.device_meta = device_meta
        self._exclude = exclude
        self.entry_id = entry_id
        self.host = host
        self.global_data: dict[str, Any] = {}
        self._max_age = scan_interval
        self._ages: dict[tuple[str, str], int] = {}
        self._cursor = 0
        self._warned_ages = False
        self._last_def_refresh: float | None = None
        self._decode_fails: dict[tuple[str, str], int] = {}
        self._dead: set[tuple[str, str]] = set()
        self._last_revive: float | None = None
        self._unread_tries: dict[tuple[str, str], int] = {}
        # Boiler-regulation weekly schedules, one instance per circuit --
        # created by __init__.py (before platform setup) and then shared
        # between climate.py (reads) and calendar.py (writes).
        self.heating_schedule_stores: dict[str, HeatingScheduleStore] = {}
        self._fast = self._collect_fast(fields, fast or [])
        if self._fast:
            _LOGGER.info(
                "Reading directly from the bus every %d s: %s",
                scan_interval,
                ", ".join(f"{c}/{m}" for c, m in self._fast),
            )

    def _collect_fast(
        self, fields: list[FieldDesc], patterns: list[str]
    ) -> list[tuple[str, str]]:
        """Messages force-read every cycle (name substrings)."""
        if not patterns:
            return []
        out: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for desc in fields:
            key = (desc.circuit, desc.message)
            if key in seen or not self.included(desc):
                continue
            if any(p in desc.message.lower() for p in patterns):
                seen.add(key)
                out.append(key)
        return out

    @property
    def bridge_id(self) -> tuple[str, str]:
        """Identifier of the bridge parent device (via_device target of the circuits)."""
        return (DOMAIN, self.entry_id)

    def included(self, desc: FieldDesc) -> bool:
        """False if the message name contains an exclude pattern."""
        name = desc.message.lower()
        return not any(pattern in name for pattern in self._exclude)

    async def _refresh(self, targets: list[tuple[str, str]]) -> None:
        """Catch up on messages directly from the bus, limited PARALLEL.

        ebusd reads in a blocking way and serializes the bus itself; the
        parallelism (semaphore) only overlaps the HTTP/wait times and thus
        noticeably speeds up catch-up. Time cap: once the budget is used up,
        no new reads are started (already-running ones still finish), so
        that a non-responding device doesn't overrun the cycle.
        """
        if not targets:
            return
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(5.0, self._max_age * _REFRESH_BUDGET_FRAC)
        sem = asyncio.Semaphore(_REFRESH_CONCURRENCY)
        exhausted = False

        async def _one(circuit: str, message: str) -> None:
            nonlocal exhausted
            async with sem:
                if loop.time() > deadline:  # budget used up -> don't start a new read
                    exhausted = True
                    return
                try:
                    await self.client.refresh(circuit, message, self._max_age)
                except EbusdError as err:  # single message not readable -> continue
                    _LOGGER.debug("Direct read of %s/%s: %s", circuit, message, err)

        await asyncio.gather(*(_one(c, m) for c, m in targets))
        if exhausted:
            _LOGGER.debug("Catch-up time budget exhausted, the rest will follow")

    def _stale(self) -> list[tuple[str, str]]:
        """Messages the bus doesn't keep fresh on its own, oldest first.

        Uses ebusd's own clock as the reference point (the response's most
        recent timestamp), so a time drift between HA and ebusd can't skew it.
        """
        if not self._ages:
            return []
        now = max(self._ages.values())
        limit = max(3 * self._max_age, _SELF_MAINTAINED_S)
        stale = [
            (key, lastup)
            for key, lastup in self._ages.items()
            if now - lastup > limit
            and self.included_key(key)
            and key not in self._dead
        ]
        stale.sort(key=lambda item: item[1])  # oldest first
        return [key for key, _ in stale]

    def included_key(self, key: tuple[str, str]) -> bool:
        """Like `included`, but on (circuit, message) instead of a field."""
        name = key[1].lower()
        return not any(pattern in name for pattern in self._exclude)

    async def _maybe_refresh_definitions(self) -> None:
        """Pick up new ebusd messages on their own (no reload needed).

        ebusd may know new messages after a config change that didn't exist
        yet at setup time. Only add, never remove; changed definitions
        (e.g. a new value table) still need a reload.
        """
        loop = asyncio.get_running_loop()
        if self._last_def_refresh is None:  # setup just fetched them
            self._last_def_refresh = loop.time()
            return
        if loop.time() - self._last_def_refresh < _DEF_REFRESH_S:
            return
        self._last_def_refresh = loop.time()
        try:
            fields, device_meta = await self.client.get_definitions()
        except EbusdError as err:
            _LOGGER.debug("Definitions sync failed: %s", err)
            return
        known = {d.key for d in self.fields}
        new = [d for d in fields if d.key not in known]
        if new:
            self.fields = self.fields + new
            self.device_meta = device_meta
            _LOGGER.info("Picked up %d new ebusd message(s)", len(new))

    def _track_decode_errors(self, errs: set[tuple[str, str]]) -> None:
        """Take permanently non-decodable messages out of the rotation.

        Counts consecutive decode errors per message; after `_MAX_DECODE_FAILS`
        it's marked dead and read neither by top-up nor by `fast`. If the error
        disappears (e.g. a corrected CSV after reload), the counter is reset
        and the message is released again.
        """
        for key in list(self._decode_fails):
            if key not in errs:  # decodes again -> forget and revive
                del self._decode_fails[key]
                self._dead.discard(key)
        for key in errs:
            n = self._decode_fails.get(key, 0) + 1
            self._decode_fails[key] = n
            if n >= _MAX_DECODE_FAILS and key not in self._dead:
                self._dead.add(key)
                _LOGGER.info(
                    "%s/%s keeps returning non-decodable data -> removed from "
                    "the read rotation (check the CSV definition)", *key
                )

    def _revive_probe(self) -> list[tuple[str, str]]:
        """Read dead messages again once, at long intervals.

        The forced read triggers a fresh decode attempt; if it succeeds
        (corrected definition), `_track_decode_errors` picks the message back
        up on its own. Otherwise it stays dead until the next attempt.
        """
        if not self._dead:
            return []
        loop = asyncio.get_running_loop()
        if self._last_revive is None:
            self._last_revive = loop.time()
            return []
        if loop.time() - self._last_revive < _REVIVE_S:
            return []
        self._last_revive = loop.time()
        _LOGGER.debug("Self-healing: retrying %d dead message(s)", len(self._dead))
        return list(self._dead)

    def _unread(self) -> list[tuple[str, str]]:
        """Kick off readable messages that have never returned a value, once.

        `_stale` only covers messages that already have a timestamp. An
        `r`-register that neither ebusd polls nor another master queries,
        however, never gets a `lastup` -> it would fall through and stay
        without a value forever ("unavailable"). Actively read such messages
        a few times; once a value arrives, `_stale` takes over from there.

        `lastup <= 0` counts here just like a completely missing entry as
        "never read" -- ebusd returns an entry with `lastup: 0` for freshly
        scanned messages, which would otherwise be wrongly treated as already
        known and would permanently prevent the initial read.
        """
        if not self._ages:  # without lastup, the freshness logic is off anyway
            return []
        valued = {(c, m) for (c, m, _f), v in (self.data or {}).items() if v is not None}
        out: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for desc in self.fields:
            key = (desc.circuit, desc.message)
            # Never force-read pure write messages; ebusd can't answer them
            # with an active read (for passively overheard commands like
            # SetMode, the attempt even causes "ERR: end of input reached").
            if key in seen or desc.writable or desc.passive:
                continue
            if (self._ages.get(key, 0) <= 0 and key not in valued
                    and key not in self._dead and key not in self._fast
                    and self.included_key(key)
                    and self._unread_tries.get(key, 0) < _UNREAD_MAX_TRIES):
                seen.add(key)
                out.append(key)
        return out

    async def _async_update_data(self) -> dict[tuple[str, str, str], Any]:
        await self._maybe_refresh_definitions()
        # Force-read: first the user-named ones, then the stale ones in
        # rotation -- capped, so the bus doesn't get flooded.
        targets = [key for key in self._fast if key not in self._dead]
        targets += self._revive_probe()
        # Initial values BEFORE the stale top-up: a small guaranteed quota,
        # so filling up never starves when `_stale` fills the cycle. Kept
        # small so the blocking initial reads (mainly timeouts) don't slow
        # down the bus.
        unread = self._unread()
        if unread:
            take = min(_UNREAD_PER_CYCLE, len(unread))
            for key in unread[:take]:
                self._unread_tries[key] = self._unread_tries.get(key, 0) + 1
            targets += unread[:take]
            _LOGGER.debug("%d unread message(s), kicking off %d", len(unread), take)
        stale = self._stale()
        if stale:
            take = min(_TOPUP_MAX, max(_TOPUP_MIN, len(stale) // _TOPUP_PER_BACKLOG))
            self._cursor %= len(stale)
            targets += stale[self._cursor : self._cursor + take]
            self._cursor += take
            _LOGGER.debug("%d messages stale, catching up %d", len(stale), take)
        if targets:
            await self._refresh(targets)

        try:
            data = await self.client.get_data()
        except EbusdError as err:
            raise UpdateFailed(f"ebusd: {err}") from err
        self.global_data = parse_global(data)
        self._ages = parse_ages(data)
        self._track_decode_errors(parse_decode_errors(data))
        values = parse_values(data)
        if values and not self._ages and not self._warned_ages:
            # Without `lastup`, there'd be no way to tell what the bus
            # maintains on its own -- nothing would ever get caught up silently.
            self._warned_ages = True
            _LOGGER.warning(
                "ebusd doesn't provide 'lastup' per message; values won't be "
                "caught up. Does this ebusd version support the 'full' parameter?"
            )
        _LOGGER.debug("ebusd: %d fields with a value", len(values))
        return values
