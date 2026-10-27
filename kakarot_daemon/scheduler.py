"""Nonblocking reminder scheduler with a state-change callback."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import asdict

from kakarot_daemon.reminders import ReminderStore


class ReminderScheduler:
    def __init__(
        self, store: ReminderStore, on_change: Callable[[list[dict]], None] | None = None
    ) -> None:
        self.store = store
        self.pending: list[dict] = []
        self._on_change = on_change

    def _set_pending(self, reminders: list[dict]) -> None:
        if reminders == self.pending:
            return
        
        old_ids = {r["id"] for r in self.pending}
        for r in reminders:
            if r["id"] not in old_ids:
                asyncio.create_task(self._notify_user(r["title"]))
                
        self.pending = reminders
        if self._on_change:
            self._on_change(list(self.pending))

    async def _notify_user(self, title: str) -> None:
        # Native GNOME notification: lands in the notification centre and
        # respects Do Not Disturb, which a hand-drawn popup cannot do.
        from kakarot_daemon.notifications import notify_reminder

        await notify_reminder(title)

    async def refresh(self) -> None:
        self._set_pending([asdict(reminder) for reminder in await self.store.due()])

    async def acknowledge(self, reminder_id: int) -> None:
        if not any(item["id"] == reminder_id for item in self.pending):
            return
        await self.store.mark_completed(reminder_id)
        self._set_pending(
            [item for item in self.pending if item["id"] != reminder_id]
        )

    async def run(self) -> None:
        while True:
            await self.refresh()
            # The previous 1 s poll opened SQLite 86,400 times a day for data
            # that changes on human timescales. Five seconds is still prompt
            # for a reminder and cuts the disk churn by 80%.
            await asyncio.sleep(5.0)
