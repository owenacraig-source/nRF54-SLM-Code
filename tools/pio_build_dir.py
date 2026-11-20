"""Keep Zephyr's generated command paths free of spaces on all host OSes."""
import hashlib
from pathlib import Path
import tempfile

Import("env")

project = Path(env.subst("$PROJECT_DIR"))
if " " in env.subst("$BUILD_DIR"):
    key = hashlib.sha256(str(project.resolve()).encode()).hexdigest()[:12]
    root = (Path(tempfile.gettempdir()) / ("nrf54-pcm-" + key)).resolve()
    if " " in str(root):
        raise RuntimeError("Set PLATFORMIO_BUILD_DIR to a path without spaces")
    env.Replace(PROJECT_BUILD_DIR=str(root), BUILD_DIR=str(root / env.subst("$PIOENV")))
    Path(env.subst("$BUILD_DIR")).mkdir(parents=True, exist_ok=True)
    print("Audio build directory: " + env.subst("$BUILD_DIR"))

locator = project / ".pio" / "audio-build-dir.txt"
locator.parent.mkdir(parents=True, exist_ok=True)
locator.write_text(env.subst("$BUILD_DIR") + "\n", encoding="utf-8")
