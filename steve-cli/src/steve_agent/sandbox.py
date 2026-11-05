"""Commands for the agent, run with bubblewrap. There is deliberately no way to run one without it."""

import asyncio
import logging
import os
import shutil
import signal
import subprocess
from functools import lru_cache
from pathlib import Path

from deepagents.backends import FilesystemBackend
from deepagents.backends.protocol import ExecuteResponse, SandboxBackendProtocol

from steve_agent.models.configs import SteveSandboxConfig

logger = logging.getLogger(__name__)

MAX_OUTPUT_BYTES = 64_000
_READ_SIZE = 8192
_PATH = "/usr/local/sbin:/usr/local/bin:/usr/bin:/usr/sbin:/bin:/sbin"
_SANDBOX_HOME = "/home/sandbox"
_SYSTEM = ["/usr", "/bin", "/sbin", "/lib", "/lib64"]
# What programs need from /etc, rather than all of it.
_ETC = [
    "alternatives",
    "ca-certificates",
    "crypto-policies",
    "group",
    "hosts",
    "ld.so.cache",
    "ld.so.conf",
    "ld.so.conf.d",
    "localtime",
    "nsswitch.conf",
    "passwd",
    "pki",
    "resolv.conf",
    "ssl",
]


def bwrap_args(folder: Path, *, network: bool) -> list[str]:
    args = ["bwrap", "--die-with-parent", "--new-session", "--unshare-all"]
    if network:
        args.append("--share-net")
    for path in _SYSTEM:
        args += ["--ro-bind-try", path, path]
    for name in _ETC:
        args += ["--ro-bind-try", f"/etc/{name}", f"/etc/{name}"]
    args += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--tmpfs", "/var/tmp", "--dir", _SANDBOX_HOME]
    # Last, as the folder may be under /tmp.
    args += ["--bind", str(folder), str(folder), "--chdir", str(folder)]
    # Nothing is inherited, which is what keeps API keys out.
    args += ["--clearenv", "--setenv", "PATH", _PATH, "--setenv", "HOME", _SANDBOX_HOME]
    args += ["--setenv", "LANG", "C.UTF-8", "--setenv", "TMPDIR", "/tmp", "--setenv", "TERM", "dumb"]
    return args


@lru_cache
def bwrap_works() -> bool:
    """Having the program is not enough: some systems and containers forbid what it does."""
    if shutil.which("bwrap") is None:
        logger.warning("Commands are off: bwrap is not installed.")
        return False
    try:
        folder = Path.cwd()
        result = subprocess.run(
            [*bwrap_args(folder, network=False), "--", "/bin/sh", "-c", "exit 0"],
            capture_output=True,
            timeout=15,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        logger.warning("Commands are off: bwrap could not run: %s", error)
        return False
    if result.returncode != 0:
        logger.warning("Commands are off: bwrap failed: %s", result.stderr.decode(errors="replace").strip())
    return result.returncode == 0


def can_run_commands(folder: Path, home: Path) -> bool:
    """A folder that holds the person's home, or Steve's, would hand commands their keys, read-write."""
    folder = folder.resolve()
    protected = [Path.home().resolve(), home.resolve()]
    if folder == Path("/") or not folder.is_dir():
        return False
    if any(folder == path or folder in path.parents for path in protected):
        logger.warning("Commands are off: %s contains your home or Steve's folder.", folder)
        return False
    return bwrap_works()


class _Output:
    """The start and end of what a command printed, as errors tend to be at the end."""

    def __init__(self) -> None:
        self.start = bytearray()
        self.end = bytearray()
        self.dropped = 0

    def add(self, chunk: bytes) -> None:
        room = MAX_OUTPUT_BYTES // 2 - len(self.start)
        self.start += chunk[:room]
        self.end += chunk[room:]
        extra = len(self.end) - MAX_OUTPUT_BYTES // 2
        if extra > 0:
            del self.end[:extra]
            self.dropped += extra

    def text(self) -> str:
        start, end = self.start.decode(errors="replace"), self.end.decode(errors="replace")
        return f"{start}\n[... {self.dropped} bytes left out ...]\n{end}" if self.dropped else start + end


class SandboxedFolderBackend(FilesystemBackend, SandboxBackendProtocol):
    def __init__(self, folder: Path, config: SteveSandboxConfig) -> None:
        super().__init__(root_dir=str(folder), virtual_mode=True)
        self._folder = folder.resolve()
        self._config = config

    @property
    def id(self) -> str:
        return f"bwrap-{self._folder}"

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        return asyncio.run(self.aexecute(command, timeout=timeout))

    async def aexecute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        limit = self._config.timeout_seconds
        seconds = min(timeout, limit) if timeout and timeout > 0 else limit
        process = await asyncio.create_subprocess_exec(
            *bwrap_args(self._folder, network=self._config.network),
            "--",
            "/bin/sh",
            "-c",
            command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        output = _Output()

        async def read() -> None:
            while chunk := await process.stdout.read(_READ_SIZE):
                output.add(chunk)
            await process.wait()

        try:
            await asyncio.wait_for(read(), seconds)
        except TimeoutError:
            await _kill(process)
            return ExecuteResponse(
                output=f"{output.text()}\nThe command was stopped after {seconds} seconds.",
                exit_code=124,
                truncated=output.dropped > 0,
            )
        except BaseException:
            # Stop pressed, or the client went away. Without this the command would carry on.
            await asyncio.shield(_kill(process))
            raise
        return ExecuteResponse(output=output.text(), exit_code=process.returncode, truncated=output.dropped > 0)


async def _kill(process: asyncio.subprocess.Process) -> None:
    """Ending bwrap ends everything in its process namespace, including what the command left running."""
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    await process.wait()
