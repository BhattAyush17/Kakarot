"""Command-line entry point for the headless Kakarot Live service."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import signal
import ctypes

try:
    libc = ctypes.CDLL("libc.so.6")
    libc.prctl(15, b"kakarot-daemon\0", 0, 0, 0)
except Exception:
    pass

from kakarot_daemon.audio import AudioBridge
from kakarot_daemon.commands import CommandRouter, ExecuteJobs
from kakarot_daemon.config import KakarotDaemonConfig
from kakarot_daemon.control import ControlServer, resolve_socket_path, send_command
from kakarot_daemon.credentials import (
    GeminiCredentialError,
    get_gemini_api_key,
    set_gemini_api_key,
    verify_gemini_api_key,
)
from kakarot_daemon.dbus_service import DBusService
from kakarot_daemon.live import KakarotLiveService
from kakarot_daemon.reminders import ReminderStore
from kakarot_daemon.scheduler import ReminderScheduler
from kakarot_daemon.state import DaemonState
from kakarot_daemon.system_db import initialize_db, wal_maintenance_loop
from kakarot_daemon.tools import ToolRegistry

STARTUP_FAILED = "Unable to start voice. Check your API key and audio devices."


def _initialize_cold_start(state: DaemonState, audio: AudioBridge) -> None:
    """Publish and enforce the muted gate before a cold boot can listen.

    ``AudioBridge.start()`` opens the device streams, so the gate must be
    applied immediately afterwards.  The daemon then advertises ``IDLE_MUTED``
    and the live service parks until an explicit UDS/D-Bus ``unmute`` call.
    """
    state.apply(muted=True)
    audio.set_muted(True)


async def _serve() -> None:
    loop = asyncio.get_running_loop()
    config = KakarotDaemonConfig.from_environment()
    config.ensure_directories()
    # Tighten existing installs too: the audit log may predate the private-mode
    # change and was previously world-readable.
    from kakarot_daemon.audit_logger import setup_audit_logger

    setup_audit_logger()

    # Bootstrap the knowledge-engine database (WAL, mmap, cache PRAGMAs and
    # all four schema tables) before anything else touches disk.
    initialize_db(config.data_dir)
    
    wal_task = loop.create_task(wal_maintenance_loop(config.data_dir / "kakarot.db"))

    # The coordinator owns every observable field.  Reporters below hand it
    # concrete events and nothing else; it decides what the snapshot means.
    state = DaemonState()

    active_service: list[KakarotLiveService | None] = [None]
    control_ref: list[ControlServer | None] = [None]

    async def process_wake_command(pcm: bytes, reason: str) -> None:
        server = control_ref[0]
        service = active_service[0]
        if server is None or service is None:
            return
        await server.broadcast(
            "ui_say", {"text": "Wake word detected. Processing…", "status": "busy"}
        )
        try:
            answer = await service.submit_audio(pcm)
        except Exception as exc:
            from kakarot_daemon.scrubber import sanitize_text

            await server.broadcast(
                "ui_say",
                {"text": sanitize_text(str(exc))[:500] or f"Audio request failed ({reason}).",
                 "status": "error"},
            )
            return
        if answer:
            from kakarot_daemon.scrubber import sanitize_text

            await server.broadcast(
                "ui_say", {"text": sanitize_text(answer)[:1200], "status": "idle"}
            )

    def wake_command_ready(pcm: bytes, reason: str) -> None:
        def schedule() -> None:
            asyncio.create_task(process_wake_command(pcm, reason))

        try:
            loop.call_soon_threadsafe(schedule)
        except RuntimeError:
            # The listener may finish a callback while the daemon loop shuts down.
            pass

    audio = AudioBridge(
        input_device=config.input_device,
        output_device=config.output_device,
        input_queue_chunks=config.input_queue_chunks,
        output_buffer_ms=config.output_buffer_ms,
        wake_model_dir=config.data_dir / "models",
        on_wake_command=wake_command_ready,
    )

    def microphone_levels_ready(levels: dict) -> None:
        """Marshal audio-thread level frames onto the asyncio socket loop."""
        def publish_levels() -> None:
            server = control_ref[0]
            if server is not None:
                asyncio.create_task(server.broadcast("audio_level", levels))

        try:
            loop.call_soon_threadsafe(publish_levels)
        except RuntimeError:
            pass

    # Playback transitions are reported from the PortAudio callback thread, so
    # the coordinator marshals them onto this loop.
    audio.set_output_listener(lambda active: state.apply(speaking=active))
    audio.set_level_listener(microphone_levels_ready)
    reminders = ReminderStore(config.database_path)
    registry = ToolRegistry(reminders)
    registry.repository.observe(state.apply)
    # Commands typed into a front end go through the same gated, audited
    # execute_command tool the agent uses, scheduled onto this loop.
    jobs = ExecuteJobs(registry.execute, loop)
    scheduler = ReminderScheduler(
        reminders, on_change=lambda pending: state.apply(reminders=pending)
    )
    stopped = asyncio.Event()
    changed = asyncio.Event()

    def apply_mute(muted: bool) -> None:
        import datetime
        print(f"[DAEMON TRACE] [{datetime.datetime.now()}] Mute command received. Target state: muted={muted}")
        # ``mute`` receives the gate itself, not the listening intent.
        state.apply(muted=muted)
        # Gate capture immediately; the supervisor tears the session down on
        # its next turn and may still be awaiting credentials right now.
        if muted:
            audio.set_muted(True)
        else:
            audio.manual_wake()
            # A previous startup may have failed before a live service existed
            # (bad key, transient network, missing audio). An explicit listen
            # request is the user's retry signal; without this the supervisor
            # stayed parked forever after one failure.
            if active_service[0] is None and state.snapshot().get("error"):
                changed.set()
        print(f"[DAEMON TRACE] [{datetime.datetime.now()}] Audio hardware muted={muted} and State mutation complete.")

    def sleep_microphone() -> None:
        audio.sleep_microphone()
        state.apply(muted=True)

    def control_status() -> dict:
        snapshot = state.snapshot()
        snapshot["audio_available"] = audio.audio_available
        # ``speak`` gates capture for the length of an answer without clearing
        # the listening intent, so a closed device is healthy unless the user
        # muted or the subsystem lost its hardware entirely.
        snapshot["mic_active"] = audio.is_capturing or (
            audio.audio_available and not snapshot.get("muted")
        )
        return snapshot

    async def process_manual_input(text: str) -> str:
        service = active_service[0]
        if service is None:
            raise RuntimeError("Voice service is not ready for manual input")
        return await service.submit_text(text)

    def apply_speaker_mute(muted: bool) -> None:
        audio.output_muted = bool(muted)
        state.apply(output_muted=audio.output_muted)
        if audio.output_muted:
            audio.clear_playback()

    def extra(command: str) -> None:
        if command.startswith("ack_reminder:"):
            # acknowledge() is coroutine work; the command handler is not.
            loop.create_task(scheduler.acknowledge(int(command.split(":", 1)[1])))
            return
        if command == "reload":
            changed.set()
            return
        if command in {"speaker_on", "speaker_off"}:
            apply_speaker_mute(command == "speaker_off")

    control = ControlServer(
        resolve_socket_path(),
        mute=apply_mute,
        status=control_status,
        stop=stopped.set,
        jobs=jobs,
        extra=extra,
        manual_input=process_manual_input,
        force_listen=lambda: apply_mute(False),
        sleep_mic=sleep_microphone,
        audio_control=apply_speaker_mute,
    )
    control_ref[0] = control
    registry.set_authorization_handler(control.request_authorization)
    registry.set_human_intervention_handler(control.request_human_intervention)

    # GNOME-native control plane. The same router backs both transports, so a
    # shell extension and the private socket can never disagree about commands.
    router = CommandRouter(
        mute=apply_mute,
        status=state.snapshot,
        stop=stopped.set,
        jobs=jobs,
        extra=extra,
    )
    bus_service = DBusService(router, state.snapshot)
    bus_service.start()

    def publish(snapshot):
        """One publication reaches every transport."""
        control.state_changed(snapshot)
        bus_service.publish_state(snapshot)

    state.attach(loop)
    state.set_publisher(publish)
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stopped.set)
    await control.start()
    repository_task = asyncio.create_task(registry.repository.refresh())
    scheduler_task = asyncio.create_task(scheduler.run())
    stop_task = asyncio.create_task(stopped.wait())
    wake_watch_task = None
    connection = None
    change_task = None
    try:
        changed.set()
        while not stopped.is_set():
            change_task = asyncio.create_task(changed.wait())
            waiters = [stop_task, change_task]
            if connection is not None:
                waiters.append(connection)
            await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
            change_task.cancel()
            await asyncio.gather(change_task, return_exceptions=True)
            changed.clear()
            if wake_watch_task:
                wake_watch_task.cancel()
                await asyncio.gather(wake_watch_task, return_exceptions=True)
                wake_watch_task = None
            if connection:
                if connection.done() and not connection.cancelled():
                    from kakarot_daemon.scrubber import sanitize_text

                    failure = connection.exception()
                    if failure is not None:
                        logging.getLogger(__name__).error(
                            "Voice session stopped: %s", failure
                        )
                        state.apply(
                            connected=False,
                            muted=True,
                            error=sanitize_text(str(failure))[:300]
                            or STARTUP_FAILED,
                        )
                    else:
                        state.apply(connected=False)
                active_service[0] = None
                connection.cancel()
                await asyncio.gather(connection, return_exceptions=True)
                connection = None
            audio.close()
            if stopped.is_set():
                break

            try:
                key = await asyncio.to_thread(get_gemini_api_key)
                if not key:
                    raise GeminiCredentialError(
                        "No Gemini API key is configured. Open Settings and save one."
                    )
                # Validate before opening the microphone.  A rejected key used
                # to leave the daemon looking healthy (connected/listening)
                # while every audio turn failed at the first API request.
                await asyncio.to_thread(verify_gemini_api_key, key)
                audio.start()
                wake_watch_task = asyncio.create_task(audio.watch_wake_model())
                state.apply(error="")
                # Cold boot is always muted. The supervisor may initialize the
                # live service, but it must park in IDLE_MUTED until the user
                # explicitly unmutes through UDS or D-Bus.
                _initialize_cold_start(state, audio)
                service = KakarotLiveService(
                    api_key=key,
                    model=config.model,
                    audio=audio,
                    tools=registry,
                    tts_model=config.tts_model,
                    tts_fallback_models=config.tts_fallback_models,
                    fallback_models=config.fallback_models,
                    vad_threshold=config.vad_threshold,
                    vad_silence_seconds=config.vad_silence_seconds,
                    vad_min_speech_seconds=config.vad_min_speech_seconds,
                    barge_in_enabled=config.barge_in_enabled,
                    follow_up_listening=config.follow_up_listening,
                    on_change=state.apply,
                )
                active_service[0] = service
                connection = asyncio.create_task(service.run_forever())
            except Exception as exc:
                from kakarot_daemon.scrubber import sanitize_text

                audio.close()
                detail = sanitize_text(str(exc)).strip()[:300]
                message = detail or STARTUP_FAILED
                logging.getLogger(__name__).error("Voice startup failed: %s", message)
                # Clearing the intent stops the supervisor from retrying until
                # the user asks again.
                state.apply(muted=True, error=message)
    finally:
        background = (
            repository_task, scheduler_task, stop_task, change_task,
            connection, wal_task, wake_watch_task,
        )
        for task in background:
            if task:
                task.cancel()
        await asyncio.gather(
            *(task for task in background if task), return_exceptions=True
        )
        await control.close()
        bus_service.close()
        audio.close()
        await reminders.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Kakarot AI Desktop Daemon")
    subcommands = parser.add_subparsers(dest="command")
    configure = subcommands.add_parser(
        "configure", help="save a Gemini API key in the OS keyring"
    )
    configure.add_argument("--api-key")
    control = subcommands.add_parser(
        "control", help="send a private command to the running daemon"
    )
    control.add_argument(
        "control_command",
        choices=(
            "status",
            "mute",
            "unmute",
            "stop",
            "history",
            "clear_history",
            "reload",
            "speaker_on",
            "speaker_off",
        ),
    )
    args = parser.parse_args()
    if args.command == "configure":
        api_key = args.api_key or getpass.getpass("Gemini API key: ").strip()
        if not api_key:
            parser.error("an API key is required")
        try:
            verify_gemini_api_key(api_key)
        except GeminiCredentialError as exc:
            parser.error(str(exc))
        set_gemini_api_key(api_key)
        print("Gemini API key verified and saved.")
        return 0
    if args.command == "control":
        path = resolve_socket_path()
        print(asyncio.run(send_command(path, args.control_command)))
        return 0
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    asyncio.run(_serve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
