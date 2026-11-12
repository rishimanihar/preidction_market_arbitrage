"""Read a Windows file ACL with ``icacls`` and flag broad read access.

POSIX mode bits mean nothing on Windows, so key files and the settings file
are checked through their access control list instead. ``icacls <path>``
prints one access control entry (ACE) per line::

    C:\\Users\\me\\.config\\pmarb\\.env NT AUTHORITY\\SYSTEM:(F)
                                    BUILTIN\\Users:(I)(RX)
                                    DESKTOP-1\\me:(F)

    Successfully processed 1 files; Failed processing 0 files

A file is refused when Everyone, Users, BUILTIN\\Users or Authenticated Users
holds an allow ACE that includes read access. Names are matched in English
and as well-known SIDs (icacls prints a SID when it cannot resolve a name).
On a non-English Windows the group names are localized. Matching those names
is a known gap; the installer removes inherited ACEs, so they do not appear.

The command runner is injectable, so tests feed sample outputs and never need
Windows.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Literal

CommandRunner = Callable[..., object]

# Lower-case principal (name or SID) -> display name.
BROAD_PRINCIPALS = {
    "everyone": "Everyone",
    "s-1-1-0": "Everyone",
    "users": "Users",
    "builtin\\users": "BUILTIN\\Users",
    "s-1-5-32-545": "BUILTIN\\Users",
    "authenticated users": "Authenticated Users",
    "nt authority\\authenticated users": "NT AUTHORITY\\Authenticated Users",
    "s-1-5-11": "Authenticated Users",
}
# Simple and specific rights that let the holder read file contents.
# F full, M modify, RX read and execute, R read, RD read data,
# GR generic read, GA generic all, MA maximum allowed.
READ_RIGHTS = frozenset({"F", "M", "RX", "R", "RD", "GR", "GA", "MA"})
_INHERITANCE_FLAGS = frozenset({"I", "OI", "CI", "IO", "NP"})
_ACE_RE = re.compile(r"^(?P<principal>[^:]+):(?P<perms>(?:\([^()]*\))+)\s*$")
_GROUP_RE = re.compile(r"\(([^()]*)\)")

RESTRICT_HINT = 'icacls "{path}" /inheritance:r /grant:r "${{env:USERNAME}}:(F)"'


class IcaclsParseError(ValueError):
    """icacls ran but its output could not be read as an ACL."""


@dataclass(frozen=True, slots=True)
class AclEntry:
    """One access control entry from ``icacls``."""

    principal: str
    rights: frozenset[str]
    deny: bool = False
    inherited: bool = False
    inherit_only: bool = False

    @property
    def grants_read(self) -> bool:
        """True for an allow ACE that applies to the file and allows reading."""
        return not self.deny and not self.inherit_only and bool(self.rights & READ_RIGHTS)

    @property
    def broad_name(self) -> str | None:
        """Display name when the principal is one of the broad groups."""
        return BROAD_PRINCIPALS.get(self.principal.strip().lstrip("*").lower())


@dataclass(frozen=True, slots=True)
class AclCheck:
    """Outcome of :func:`check_acl`.

    ``status`` is ``ok`` (only narrow principals can read), ``open`` (a broad
    group can read; see ``broad_readers``), or ``unavailable`` (icacls could
    not run, so nothing was verified; see ``detail``).
    """

    status: Literal["ok", "open", "unavailable"]
    broad_readers: tuple[str, ...] = ()
    detail: str = ""


def _parse_ace(text: str) -> AclEntry | None:
    match = _ACE_RE.match(text.strip())
    if match is None:
        return None
    rights: set[str] = set()
    flags: set[str] = set()
    deny = False
    for group in _GROUP_RE.findall(match["perms"]):
        for token in (part.strip().upper() for part in group.split(",")):
            if not token:
                continue
            if token == "DENY":
                deny = True
            elif token in _INHERITANCE_FLAGS:
                flags.add(token)
            else:
                rights.add(token)
    return AclEntry(
        principal=match["principal"].strip(),
        rights=frozenset(rights),
        deny=deny,
        inherited="I" in flags,
        inherit_only="IO" in flags,
    )


def _strip_path(line: str, path: str) -> str | None:
    """Remove the echoed path from the first output line.

    icacls echoes the argument as given. If the console code page mangled a
    non-ASCII character, fall back to the file name, which is ASCII here.
    """
    if line.lower().startswith(path.lower()):
        return line[len(path) :]
    name = PureWindowsPath(path).name or path
    index = line.lower().rfind(name.lower() + " ")
    if index < 0:
        return None
    return line[index + len(name) :]


def parse_icacls(output: str, path: Path | str) -> list[AclEntry]:
    """Parse ``icacls <path>`` output into entries.

    Raise :class:`IcaclsParseError` when the path or any ACE cannot be found,
    so an unexpected format never passes as "no broad access".
    """
    target = str(path)
    entries: list[AclEntry] = []
    first = True
    for raw in output.splitlines():
        line = raw.rstrip()
        if not line.strip():
            if not first:
                break  # the blank line before the summary ends the ACL block
            continue
        if first:
            first = False
            rest = _strip_path(line, target)
            if rest is None:
                raise IcaclsParseError("icacls output does not start with the checked path")
            line = rest
        entry = _parse_ace(line)
        if entry is None:
            # Every line of the block is an ACE. Never skip one we cannot read.
            raise IcaclsParseError("icacls output has an access control entry that cannot be read")
        entries.append(entry)
    if not entries:
        raise IcaclsParseError("icacls output contained no access control entries")
    return entries


def broad_readers(entries: list[AclEntry]) -> tuple[str, ...]:
    """Broad groups (Everyone, Users, Authenticated Users) that can read."""
    names: list[str] = []
    for entry in entries:
        name = entry.broad_name
        if name is not None and entry.grants_read and name not in names:
            names.append(name)
    return tuple(names)


def check_acl(path: Path | str, runner: CommandRunner | None = None) -> AclCheck:
    """Run ``icacls`` on ``path`` and classify its ACL.

    Raise :class:`IcaclsParseError` when icacls runs but fails or prints
    something unreadable. Those cases fail closed. Only an icacls that cannot
    run at all returns ``unavailable``, and the caller must warn about it.
    """
    execute = runner or subprocess.run
    target = str(path)
    try:
        result = execute(
            ["icacls", target],
            check=False,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=10,
        )
    except FileNotFoundError:
        return AclCheck("unavailable", detail="icacls was not found on PATH")
    except subprocess.TimeoutExpired:
        return AclCheck("unavailable", detail="icacls timed out")
    except OSError as exc:
        return AclCheck("unavailable", detail=f"icacls could not run ({type(exc).__name__})")
    returncode = int(getattr(result, "returncode", 1))
    stdout = str(getattr(result, "stdout", "") or "")
    if returncode != 0:
        raise IcaclsParseError(f"icacls exited with status {returncode}")
    readers = broad_readers(parse_icacls(stdout, target))
    if readers:
        return AclCheck("open", broad_readers=readers)
    return AclCheck("ok")
