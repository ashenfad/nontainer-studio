"""The starter skills a session is given: which ones it may follow, the
files generated for them, and the conditional blocks resolved for its
executor.

They are mounted read-only, not copied in. Each set a session can be
given (by executor, delegation and ws-git) is resolved once on the host
into ``<store>/.skills/<hash>/``, named by a hash of what it holds, and
mounted at ``<root>/skills/<name>`` with nontainer's ``skills.mounts``.
A mount is not versioned, so ws-git never lists a starter skill and no
commit, delegate or fork carries a copy; every session sees the text
this server ships, and a package upgrade reaches the next session opened
because its READMEs hash to a new set. Skills an agent writes are its
own files under ``<root>/skills`` beside them.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any, Callable

from .config import sessions_tool_enabled

log = logging.getLogger(__name__)


def _can_start_from_published(wsgit: bool) -> bool:
    """Whether the ``starting-from-published`` skill can be followed in
    a session, given whether that session carries ``ws-git``.

    Its workflow is two things and needs both: the ``sessions`` tool's
    ``published`` action lists the apps and their origin tags, and the
    ws-git verbs are what read a tag — mount it, take files out of it,
    diff it. With the tool and no verb, every step after the listing is
    a spelling the terminal answers ``command not found`` to, and the
    primer has already told that agent nothing brings files over.
    """
    return sessions_tool_enabled() and wsgit


def _can_delegate(wsgit: bool) -> bool:
    """Whether a session can hand work to delegates and bring it back:
    the ``sessions`` tool to ask, and ``ws-git`` to merge or check out
    what a delegate did. Skill text in a ``delegation`` block is kept
    only where this holds."""
    return sessions_tool_enabled() and wsgit


#: Starter skills whose subject is a studio feature that can be switched
#: off, keyed by directory name: one is seeded only where its gate says
#: the session can follow it. A SKILL.md is a file of text with no
#: conditions of its own, so the gate is on the install rather than
#: inside the skill — text teaching a tool or a verb the agent was not
#: given costs it a turn to find that out. Each gate is asked the
#: session's own ``ws-git`` answer, because an env knob says what was
#: asked for and that flag says what the session got. Seeding happens
#: once, at session creation, so a session keeps what it was told then.
_GATED_SKILLS: dict[str, Callable[[bool], bool]] = {
    "starting-from-published": _can_start_from_published,
}


#: The packages the nontainer-ecosystem skill documents, whose READMEs it
#: carries under references/.
_ECOSYSTEM_PACKAGES = (
    "nontainer-studio",
    "nontainer",
    "termish",
    "monkeyfs",
    "sandtrap",
    "kvgit",
    "reprobate",
    "dud",
)


def _ecosystem_readmes() -> dict[str, tuple[str, bytes]]:
    """``references/<package>.md`` for each installed package: the README
    its distribution metadata carries, labelled with the version.

    Read from what is installed rather than copied into the repo, so the
    text describes the versions this server actually runs and never goes
    stale. A package that is not installed (dud is an extra) is skipped.
    """
    from importlib.metadata import PackageNotFoundError, metadata

    files = {}
    for name in _ECOSYSTEM_PACKAGES:
        try:
            meta = metadata(name)
        except PackageNotFoundError:
            continue
        body = (meta.get_payload() or meta.get("Description") or "").strip()
        if not body:
            continue
        label = f"{name} {meta['Version']}: the README of the installed package"
        files[f"references/{name}.md"] = (label, f"\n{body}\n".encode())
    return files


#: Skill files built when a session is seeded rather than kept in the
#: skill's directory, keyed by that directory's name. Each builder maps a
#: path inside the skill to a label and a markdown body.
_GENERATED_SKILL_FILES: dict[str, Callable[[], dict[str, tuple[str, bytes]]]] = {
    "nontainer-ecosystem": _ecosystem_readmes,
}


def _stamp_generated(label: str, body: bytes) -> bytes:
    """A generated skill file, its first line saying what it is."""
    digest = hashlib.sha256(body).hexdigest()[:16]
    return f"<!-- {label}; generated, sha256 {digest} -->\n".encode() + body


def _module_grants(modules: Any) -> list[Any]:
    """The modules a python config grants, flattened: its entries may be
    preset groups (lists of grants) as well as grants and modules."""
    out: list[Any] = []
    for entry in modules or ():
        if isinstance(entry, (list, tuple)):
            out.extend(_module_grants(entry))
        else:
            out.append(getattr(entry, "module", entry))
    return out


def _module_skills(modules: Any) -> list[tuple[str, dict[str, bytes]]]:
    """``(name, {path: bytes})`` for each skill a granted module's
    package ships, read whole: a package inside a zip has no directory
    to mount, but its bytes go into the starter set like any other."""
    from nontainer import skills

    found: list[tuple[str, dict[str, bytes]]] = []
    seen: set[str] = set()
    for module in _module_grants(modules):
        top = (getattr(module, "__name__", None) or str(module)).split(".")[0]
        if not top or top in seen:
            continue
        seen.add(top)
        try:
            dirs = skills.discover(top)
        except Exception:
            continue
        for skill_dir in dirs:
            try:
                files = _read_tree(skill_dir)
                meta = skills.frontmatter(files.get("SKILL.md", b""))
                raw = meta.get("name") or skill_dir.name
                name = re.sub(r"[^a-z0-9._-]+", "-", raw.lower()).strip("-")
            except Exception:
                continue
            if name and "SKILL.md" in files:
                found.append((name, files))
    return found


def _read_tree(node: Any, prefix: str = "") -> dict[str, bytes]:
    """Every file under a directory-like node (a path or a package
    resource), by relative path."""
    out: dict[str, bytes] = {}
    for child in node.iterdir():
        rel = f"{prefix}/{child.name}" if prefix else child.name
        if child.is_dir():
            out.update(_read_tree(child, rel))
        elif child.is_file():
            out[rel] = child.read_bytes()
    return out


class SkillsMixin:
    """Starter skills, as part of ``Registry``: the read-only mounts a
    session is opened with."""

    @staticmethod
    def _skill_root() -> Path:
        return Path(
            os.getenv("NONTAINER_STUDIO_SKILLS")
            or Path(__file__).resolve().parent.parent / "skills"
        ).expanduser()

    @staticmethod
    def _skill_seeds(*, wsgit: bool) -> list[Path]:
        """The skill directories a session is given: each child of
        NONTAINER_STUDIO_SKILLS holding a SKILL.md, minus the gated ones
        it cannot follow."""
        root = SkillsMixin._skill_root()
        if not root.is_dir():
            return []
        seeds = []
        for child in sorted(root.iterdir()):
            if not (child.is_dir() and (child / "SKILL.md").is_file()):
                continue
            gate = _GATED_SKILLS.get(child.name)
            if gate is not None and not gate(wsgit):
                continue
            seeds.append(child)
        return seeds

    @staticmethod
    def _starter_files(
        *, commands: bool, wsgit: bool, modules: Any = ()
    ) -> dict[str, bytes]:
        """The starter set a session with this executor and ws-git
        answer is given, as ``{"<skill>/<path>": bytes}``: each seed's
        files with its markdown resolved for the session (SKILL.md and
        the references it points into alike), and its
        generated files, then the skills its granted python modules ship
        (``<pkg>/skills/``, nontainer's convention), resolved the same
        way. A seed wins a name both have. Best-effort per file: one
        that will not read or generate is left out, not fatal."""
        delegation = _can_delegate(wsgit)
        out: dict[str, bytes] = {}
        for seed in SkillsMixin._skill_seeds(wsgit=wsgit):
            for path in sorted(p for p in seed.rglob("*") if p.is_file()):
                rel = f"{seed.name}/{path.relative_to(seed).as_posix()}"
                try:
                    data = path.read_bytes()
                except OSError:
                    continue
                if rel.endswith(".md"):
                    text = data.decode("utf-8", "replace")
                    data = SkillsMixin._resolve_skill_text(
                        text, commands=commands, delegation=delegation
                    ).encode()
                out[rel] = data
            make = _GENERATED_SKILL_FILES.get(seed.name)
            if make is None:
                continue
            try:
                generated = make()
            except Exception:
                continue
            for rel, (label, body) in generated.items():
                out[f"{seed.name}/{rel}"] = _stamp_generated(label, body)
        taken = {rel.split("/", 1)[0] for rel in out}
        for name, files in _module_skills(modules):
            if name in taken:
                continue
            taken.add(name)
            for rel, data in files.items():
                if rel.endswith(".md"):
                    data = SkillsMixin._resolve_skill_text(
                        data.decode("utf-8", "replace"),
                        commands=commands,
                        delegation=delegation,
                    ).encode()
                out[f"{name}/{rel}"] = data
        return out

    def _starter_dir(
        self, *, commands: bool, wsgit: bool, modules: Any = ()
    ) -> Path | None:
        """The resolved starter set on disk, written once: its directory
        is named by a hash of what it holds, so an existing one is never
        rewritten (a session may have it mounted) and a changed set gets
        a directory of its own. ``None`` when there are no starter
        skills."""
        files = self._starter_files(commands=commands, wsgit=wsgit, modules=modules)
        if not files:
            return None
        digest = hashlib.sha256()
        for rel in sorted(files):
            digest.update(rel.encode() + b"\0" + hashlib.sha256(files[rel]).digest())
        target = Path(self._store.path) / ".skills" / digest.hexdigest()[:16]
        if target.is_dir():
            return target
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(dir=target.parent, prefix=".building-"))
        try:
            for rel, data in files.items():
                dest = staging / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(data)
            try:
                staging.rename(target)
            except OSError:
                if not target.is_dir():  # not a concurrent build of the same set
                    raise
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
        return target

    def _skill_mounts(
        self,
        *,
        commands: bool,
        wsgit: bool,
        modules: Any = (),
        root: str = "/workspace",
    ) -> dict[str, Any]:
        """The read-only mounts a session is opened with: the starter
        set for its executor and ws-git answer, its granted modules'
        skills included (see ``_starter_files``). Best-effort: a set
        that will not build leaves the session without starter skills,
        never unopened."""
        from nontainer import skills

        try:
            starter = self._starter_dir(commands=commands, wsgit=wsgit, modules=modules)
            return skills.mounts(starter, root=root) if starter is not None else {}
        except Exception:
            log.warning("starter skills could not be built", exc_info=True)
            return {}

    # Conditional blocks in a starter skill's markdown (SKILL.md and
    # its references alike), `<!--if:KEY-->` …
    # `<!--endif-->`, or `<!--if:no-KEY-->` for the other side. Skill text
    # that teaches what a session does not have costs the agent a turn to
    # discover, so such text is written in a block rather than
    # unconditionally. Blocks do not nest. The keys:
    #
    # - `commands`: the executor runs injected terminal builtins (termish);
    #   `no-commands`, the terminal is a real shell that does not. The
    #   portable `ws-*` verbs are NOT this distinction: they ferry into a
    #   guest, so they answer on every rung and need no gate.
    # - `delegation`: the session can delegate and bring the work back
    #   (see `_can_delegate`).
    _IF_BLOCK = re.compile(
        r"[ \t]*<!--if:(no-)?(commands|delegation)-->[ \t]*\n(.*?)[ \t]*<!--endif-->[ \t]*\n?",
        re.DOTALL,
    )

    @staticmethod
    def _resolve_skill_text(
        text: str, *, commands: bool, delegation: bool = False
    ) -> str:
        """Keep the blocks this session has what they teach for, drop
        the others."""
        has = {"commands": commands, "delegation": delegation}

        def _pick(m: "re.Match[str]") -> str:
            negated, key, body = m.group(1), m.group(2), m.group(3)
            return body if has[key] != bool(negated) else ""

        return SkillsMixin._IF_BLOCK.sub(_pick, text)
