import asyncio
import os

async def play_media(url: str, video: bool = False) -> str:
    env = os.environ.copy()
    env["PIPEWIRE_NODE"] = "echocancel_sink"
    env["PULSE_SINK"] = "echocancel_sink"
    
    cmd = ["systemd-run", "--user", "--scope", "mpv", "--really-quiet"]
    if not video:
        cmd.append("--no-video")
    cmd.append(url)
    
    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            env=env,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL
        )
        
        try:
            # Wait up to 5 seconds for the process to exit
            await asyncio.wait_for(process.wait(), timeout=5.0)
            if process.returncode != 0:
                pass # Failed, pivot to Path A
            else:
                return "Media playing successfully via MPV (Path B)."
        except (asyncio.TimeoutError, TimeoutError):
            # Still running after 5 seconds, it's successful
            return "Media playing successfully via MPV (Path B)."
            
    except Exception:
        # Execution failed completely
        pass

    # Tier 2 Fallback (Path A)
    fallback_cmd = ["systemd-run", "--user", "--scope", "xdg-open", url]
    try:
        await asyncio.create_subprocess_exec(
            *fallback_cmd,
            env=env,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL
        )
        return "Stream extraction failed. Pivoting to Path A: Media opened in browser."
    except Exception as e:
        return f"Failed to play media: {str(e)}"
