"""AT-SPI2 Native Accessibility Perception."""

import asyncio
try:
    import pyatspi
except ImportError:
    pyatspi = None

from kakarot_daemon.scrubber import is_app_sensitive, sanitize_text

TERMINAL_APPS = {"gnome-terminal", "kitty", "alacritty", "konsole"}
ELECTRON_CLASSES = {"code", "obsidian", "discord", "google-chrome", "chromium", "brave-browser", "slack"}

_MAX_TREE_DEPTH = 64  # AT-SPI trees can be pathologically deep; bound traversal


def _safe_focused_node(root):
    """Iterative depth-bounded DFS for the focused node.

    A recursive walk can blow the interpreter stack (RecursionError) on deeply
    nested accessibility trees, so use an explicit stack instead."""
    if not root:
        return None
    stack = [(root, 0)]
    while stack:
        node, depth = stack.pop()
        if not node:
            continue
        try:
            if node.getState().contains(pyatspi.STATE_FOCUSED):
                return node
        except Exception:
            continue
        if depth < _MAX_TREE_DEPTH:
            try:
                stack.extend((child, depth + 1) for child in node)
            except Exception:
                continue
    return None


async def read_screen_context() -> dict[str, str]:
    if pyatspi is None:
        return {"error": "pyatspi is not installed. Cannot read AT-SPI accessibility tree."}
        
    app_info = {"name": ""}
    
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(_read_screen_context_sync, app_info),
            timeout=2.0
        )
    except (asyncio.TimeoutError, TimeoutError):
        result = {"error": "AT-SPI2 extraction timed out."}
    except Exception as e:
        result = {"error": f"Exception during AT-SPI2 extraction: {e}"}
        
    content = result.get("content", "")
    
    if content and not content.startswith("[REDACTED:"):
        return result
        
    if content.startswith("[REDACTED:"):
        return result

    # Extraction failed (empty, timed out, or exception)
    app_name = app_info["name"].lower()
    if any(cls in app_name for cls in ELECTRON_CLASSES):
        return {"error": "Application accessibility tree inactive (Electron/Chromium detected). Please manually highlight the text you want Kakarot to read, or ensure ACCESSIBILITY_ENABLED=1 is set in your environment."}
    
    return {"error": "Active window did not expose readable text via AT-SPI2."}

def _read_screen_context_sync(app_info: dict[str, str]) -> dict[str, str]:
    try:
        desktop = pyatspi.Registry.getDesktop(0)
        if not desktop:
            return {"error": "Could not access AT-SPI desktop."}
            
        focused_app = None
        # Find the active application
        for app in desktop:
            if not app:
                continue
            state = app.getState()
            if state.contains(pyatspi.STATE_ACTIVE) or state.contains(pyatspi.STATE_FOCUSED):
                focused_app = app
                break
            for window in app:
                if not window: continue
                win_state = window.getState()
                if win_state.contains(pyatspi.STATE_ACTIVE) or win_state.contains(pyatspi.STATE_FOCUSED):
                    focused_app = app
                    break
            if focused_app:
                break
                
        if not focused_app:
            return {"error": "No focused application found."}
            
        app_name = focused_app.name or ""
        app_info["name"] = app_name
        
        if is_app_sensitive(app_name):
            return {"content": "[REDACTED: Sensitive Application]"}

        is_terminal = any(term in app_name.lower() for term in TERMINAL_APPS)
        
        # Traverse to find the focused component (depth-bounded, no recursion)
        focused_node = _safe_focused_node(focused_app)
        if not focused_node:
            focused_node = focused_app # Fallback

        # Check for password role
        role = focused_node.getRole()
        node_state = focused_node.getState()
        
        if role == pyatspi.ROLE_PASSWORD_TEXT or node_state.contains(pyatspi.STATE_PROTECTED):
            return {"content": "[REDACTED: Password Field]"}

        text = ""
        try:
            text_iface = focused_node.queryText()
            if is_terminal:
                # Terminal safety: extract only selected text
                n_selections = text_iface.getNSelections()
                if n_selections > 0:
                    start, end = text_iface.getSelection(0)
                    text = text_iface.getText(start, end)
                else:
                    return {"content": "Terminal active: no text selected."}
            else:
                text = text_iface.getText(0, -1)
        except NotImplementedError:
            # Fallback if the node doesn't implement the Text interface
            text = focused_node.name

        if not text:
            return {"content": "No readable text in focused element."}
            
        truncated = text[:2000]
        sanitized = sanitize_text(truncated)
        
        if len(text) > 2000:
            sanitized += "\n[... truncated]"
            
        return {"content": sanitized}

    except Exception as e:
        return {"error": f"AT-SPI perception failed: {e}"}
