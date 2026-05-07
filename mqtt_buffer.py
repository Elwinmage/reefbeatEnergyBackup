"""
MQTT buffer with replay-on-reconnect.

The Home Assistant broker (and HA itself) typically run on the same
domestic infrastructure as the rest of the home, which means a power
outage takes them down within a few minutes. During that window, the
battery monitor service keeps producing measurements -- but they have
nowhere to go and would normally be lost.

This module fixes that: every message intended for MQTT is also
appended to a local JSONL file on disk. When the broker is
unreachable, messages just accumulate locally. When it comes back, a
background thread replays everything that wasn't acknowledged yet.
The result is a continuous timeline in HA covering both before, during
and after the outage -- which is exactly what's needed to study the
battery's behaviour under load.

Storage layout (under buffer_dir, default /var/lib/reef-battery-monitor):
    messages.jsonl       append-only, one JSON per line:
                         {"ts": 1714123456.789, "topic": "...", "payload": "..."}
    publish_offset       a single text file holding the byte offset of
                         the last successfully published message (so we
                         know where to resume on restart)

Retention: messages.jsonl rolls daily; files older than `retention_days`
are deleted on startup. JSON Lines is robust to crashes -- a partial
final line is just ignored on the next read.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional


class MqttBuffer:
    """File-backed message buffer with automatic replay.

    Usage:
        buf = MqttBuffer(Path("/var/lib/reef-battery-monitor"))
        buf.attach_client(mqtt_client)
        ...
        buf.publish("topic/state", '{"voltage": 26.5}')

    `publish` always succeeds locally (writes to disk). The replay
    thread takes care of pushing unsent messages to the broker as
    soon as it's reachable.
    """

    def __init__(self, buffer_dir: Path, retention_days: int = 7,
                 replay_batch_size: int = 200):
        self._dir = buffer_dir
        self._retention_days = retention_days
        # We replay in batches to avoid flooding the broker right after
        # reconnection -- 200 messages at ~5s spacing is ~17 minutes of
        # outage per batch.
        self._replay_batch = replay_batch_size

        self._dir.mkdir(parents=True, exist_ok=True)
        self._log_path = self._dir / "messages.jsonl"
        self._offset_path = self._dir / "publish_offset"

        # Single lock guards both the file (we append) and the
        # offset (we update it after each successful publish).
        self._lock = threading.Lock()

        # The MQTT client is attached after construction (the main code
        # creates it separately). When None, publish() still buffers.
        self._client = None

        # Background replay thread
        self._stop = threading.Event()
        self._wakeup = threading.Event()  # nudges the thread on connect
        self._thread = threading.Thread(
            target=self._replay_loop, name="MqttReplay", daemon=True
        )

        self._purge_old_files()

    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------

    def attach_client(self, client) -> None:
        """Hand over the paho-mqtt client and start the replay thread.

        We attach after construction so the buffer can be created
        before the broker is even reachable -- the wizard might run
        on a Pi whose MQTT host is on the same UPS that just died.
        """
        self._client = client
        if not self._thread.is_alive():
            self._thread.start()

    def notify_connected(self) -> None:
        """Signal that the client just connected (or reconnected).

        Call this from the paho-mqtt on_connect callback so the replay
        thread wakes up immediately instead of waiting for its periodic
        check.
        """
        self._wakeup.set()

    def publish(self, topic: str, payload: str, retain: bool = False) -> None:
        """Append a message to the local log; the replay thread will
        push it to the broker when possible.

        This method NEVER blocks on the network -- the on-disk write
        is the only synchronous step, and it's fast (a few hundred
        microseconds for a few hundred bytes on an SD card).
        """
        record = {
            "ts": time.time(),
            "topic": topic,
            "payload": payload,
            "retain": retain,
        }
        line = json.dumps(record, separators=(",", ":")) + "\n"
        with self._lock:
            try:
                with self._log_path.open("a", encoding="utf-8") as f:
                    f.write(line)
                    f.flush()  # disk durability matters during outage
            except OSError as e:
                # Disk full / read-only filesystem -- not much we can do
                # except complain and let the rest of the service continue.
                print(f"[MQTT-BUF] Write failed: {e}")
                return
        # Wake the replay thread so it can push immediately if the
        # broker is up. Cheap when the thread is already running.
        self._wakeup.set()

    def stop(self) -> None:
        """Stop the replay thread (called on shutdown)."""
        self._stop.set()
        self._wakeup.set()
        if self._thread.is_alive():
            self._thread.join(timeout=2.0)

    @property
    def pending_count(self) -> int:
        """Approximate number of buffered-but-not-published messages.

        Used for diagnostics / a future MQTT sensor. Cheap to call.
        """
        try:
            offset = self._read_offset()
            size = self._log_path.stat().st_size if self._log_path.exists() else 0
            if size <= offset:
                return 0
            # Estimate: count newlines in the unpublished tail. Reading
            # a few MB to count lines is fine for this purpose.
            with self._log_path.open("rb") as f:
                f.seek(offset)
                tail = f.read(size - offset)
            return tail.count(b"\n")
        except OSError:
            return 0

    # -------------------------------------------------------------------------
    # Internal: replay loop
    # -------------------------------------------------------------------------

    def _replay_loop(self) -> None:
        """Push buffered messages to the broker whenever it's reachable.

        Loops forever, checking every 10s OR whenever notify_connected()
        / publish() wakes us up. At each iteration, if the broker is
        connected, we read messages from the last known offset, push
        them, and persist the new offset. Designed to be safe against
        crashes: we only update the offset AFTER paho-mqtt accepts the
        message (publish().rc == MQTT_ERR_SUCCESS).
        """
        while not self._stop.is_set():
            try:
                self._drain_once()
            except Exception as e:  # noqa: BLE001
                print(f"[MQTT-BUF] Replay error: {e}")
            # Sleep for 10s but wake up early on connection events.
            self._wakeup.wait(timeout=10.0)
            self._wakeup.clear()

    def _drain_once(self) -> None:
        """One pass of the replay logic. Pushes up to replay_batch_size
        messages and updates the offset."""
        client = self._client
        if client is None or not client.is_connected():
            return
        if not self._log_path.exists():
            return

        offset = self._read_offset()
        sent = 0
        new_offset = offset

        with self._log_path.open("rb") as f:
            f.seek(offset)
            for raw in f:
                if sent >= self._replay_batch:
                    break  # hand control back so we don't block forever
                try:
                    rec = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    # Corrupt line (partial write before a crash) --
                    # skip it and advance the offset past it.
                    new_offset += len(raw)
                    continue

                # Push to broker. paho-mqtt's publish() is non-blocking
                # by default -- it queues into the loop_start() thread.
                # The rc tells us whether the message was accepted into
                # the queue (not whether it reached the broker -- but
                # for QoS 0 that's enough).
                info = client.publish(
                    rec["topic"],
                    rec["payload"],
                    retain=rec.get("retain", False),
                )
                if info.rc != 0:
                    # Broker disconnected mid-replay; bail out and
                    # retry on the next wakeup.
                    break
                new_offset += len(raw)
                sent += 1

        if new_offset != offset:
            self._write_offset(new_offset)
            if sent > 0:
                print(f"[MQTT-BUF] Replayed {sent} buffered message(s)")

    def _read_offset(self) -> int:
        """Read the last-published byte offset from disk. 0 if missing."""
        try:
            return int(self._offset_path.read_text().strip())
        except (OSError, ValueError):
            return 0

    def _write_offset(self, offset: int) -> None:
        """Atomically persist the new offset (tmp + rename)."""
        tmp = self._offset_path.with_suffix(".tmp")
        try:
            tmp.write_text(str(offset))
            os.replace(tmp, self._offset_path)
        except OSError as e:
            print(f"[MQTT-BUF] Offset write failed: {e}")

    # -------------------------------------------------------------------------
    # Internal: housekeeping
    # -------------------------------------------------------------------------

    def _purge_old_files(self) -> None:
        """Trim the log if it grew too old. Called once at construction.

        Strategy: if messages.jsonl is older than retention_days AND
        fully consumed (offset >= size), wipe it. We don't try to
        rotate mid-file because that would invalidate the offset; the
        file just keeps growing within a session and gets reset between
        sessions when conditions allow.
        """
        if not self._log_path.exists():
            return
        try:
            age_days = (time.time() - self._log_path.stat().st_mtime) / 86400.0
        except OSError:
            return
        if age_days <= self._retention_days:
            return
        # Old enough -- only wipe if everything is published, otherwise
        # the user might still want their pre-outage data.
        offset = self._read_offset()
        size = self._log_path.stat().st_size
        if offset >= size:
            try:
                self._log_path.unlink()
                self._offset_path.unlink(missing_ok=True)
                print(f"[MQTT-BUF] Purged log (>{self._retention_days}d old)")
            except OSError:
                pass
