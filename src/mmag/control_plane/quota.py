"""Atomic SQLite quota reservations and usage settlement."""

from __future__ import annotations

import time
from typing import Any


class QuotaStore:
    """Own quota transactions while sharing the control-plane connection and lock."""

    def __init__(self, connection: Any, lock: Any) -> None:
        self._connection = connection
        self._lock = lock

    def reserve_quota(
        self,
        reservation_id: str,
        *,
        subject_id: str,
        period: str,
        run_id: str,
        cost_usd: float,
        limit_usd: float,
        expires_at: float,
    ) -> None:
        now = time.time()
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                self._connection.execute(
                    "UPDATE quota_reservations SET status='released', updated_at=? "
                    "WHERE status='reserved' AND expires_at<?",
                    (now, now),
                )
                existing = self._connection.execute(
                    "SELECT * FROM quota_reservations WHERE reservation_id=?",
                    (reservation_id,),
                ).fetchone()
                if existing is not None:
                    if existing["subject_id"] != subject_id or existing["period"] != period:
                        raise RuntimeError("quota reservation identity mismatch")
                    if existing["status"] == "reserved":
                        self._connection.execute(
                            "UPDATE quota_reservations SET expires_at=?, updated_at=? "
                            "WHERE reservation_id=?",
                            (expires_at, now, reservation_id),
                        )
                        self._connection.commit()
                        return
                    if existing["status"] == "settled":
                        self._connection.commit()
                        return
                    raise RuntimeError("quota reservation was released")
                committed = self._connection.execute(
                    "SELECT cost_usd FROM quota_usage WHERE subject_id=? AND period=?",
                    (subject_id, period),
                ).fetchone()
                reserved = self._connection.execute(
                    "SELECT COALESCE(SUM(reserved_cost_usd), 0) AS total "
                    "FROM quota_reservations "
                    "WHERE subject_id=? AND period=? AND status='reserved'",
                    (subject_id, period),
                ).fetchone()
                total = float(committed["cost_usd"] if committed else 0) + float(
                    reserved["total"]
                )
                if total + cost_usd > limit_usd:
                    raise ValueError("quota_exceeded")
                self._connection.execute(
                    """INSERT INTO quota_reservations
                    (reservation_id, subject_id, period, run_id, reserved_cost_usd,
                     expires_at, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (reservation_id, subject_id, period, run_id, cost_usd, expires_at, now, now),
                )
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise

    def settle_quota(
        self,
        reservation_id: str,
        *,
        cost_usd: float,
        input_tokens: int,
        output_tokens: int,
    ) -> None:
        now = time.time()
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                row = self._connection.execute(
                    "SELECT * FROM quota_reservations WHERE reservation_id=?",
                    (reservation_id,),
                ).fetchone()
                if row is None:
                    raise KeyError(reservation_id)
                if row["status"] == "settled":
                    self._connection.commit()
                    return
                if row["status"] != "reserved":
                    raise RuntimeError("quota reservation is not active")
                self._connection.execute(
                    """INSERT INTO quota_usage
                    (subject_id, period, cost_usd, input_tokens, output_tokens, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(subject_id, period) DO UPDATE SET
                    cost_usd=cost_usd+excluded.cost_usd,
                    input_tokens=input_tokens+excluded.input_tokens,
                    output_tokens=output_tokens+excluded.output_tokens,
                    updated_at=excluded.updated_at""",
                    (
                        row["subject_id"],
                        row["period"],
                        cost_usd,
                        input_tokens,
                        output_tokens,
                        now,
                    ),
                )
                self._connection.execute(
                    """UPDATE quota_reservations SET status='settled', actual_cost_usd=?,
                    input_tokens=?, output_tokens=?, updated_at=? WHERE reservation_id=?""",
                    (cost_usd, input_tokens, output_tokens, now, reservation_id),
                )
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise

    def release_quota(self, reservation_id: str) -> None:
        with self._lock:
            self._connection.execute(
                "UPDATE quota_reservations SET status='released', updated_at=? "
                "WHERE reservation_id=? AND status='reserved'",
                (time.time(), reservation_id),
            )
            self._connection.commit()

    def quota_snapshot(self, subject_id: str, period: str) -> tuple[float, int, int, float]:
        usage = self._connection.execute(
            "SELECT * FROM quota_usage WHERE subject_id=? AND period=?",
            (subject_id, period),
        ).fetchone()
        reserved = self._connection.execute(
            "SELECT COALESCE(SUM(reserved_cost_usd), 0) AS total "
            "FROM quota_reservations "
            "WHERE subject_id=? AND period=? AND status='reserved' AND expires_at>=?",
            (subject_id, period, time.time()),
        ).fetchone()
        return (
            float(usage["cost_usd"] if usage else 0),
            int(usage["input_tokens"] if usage else 0),
            int(usage["output_tokens"] if usage else 0),
            float(reserved["total"]),
        )
