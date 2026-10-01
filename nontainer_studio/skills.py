"""The starter skills a session is seeded with: which ones it may follow,
the files generated for them, the top-up older sessions get, and the
conditional blocks resolved for the executor.
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Any, Callable

from nontainer import (
    Workspace,
)

from .config import (
    sessions_tool_enabled,
)


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


#: The first line of a generated skill file: its label and a hash of the
#: rest. A file whose rest still matches is pristine, so it may be
#: refreshed or removed; one that doesn't was edited, and is the
#: session's own.
_GENERATED_STAMP = re.compile(rb"<!-- (.*); generated, sha256 ([0-9a-f]{16}) -->\n")


def _stamp_generated(label: str, body: bytes) -> bytes:
    digest = hashlib.sha256(body).hexdigest()[:16]
    return f"<!-- {label}; generated, sha256 {digest} -->\n".encode() + body


def _is_pristine_generated(data: bytes) -> bool:
    m = _GENERATED_STAMP.match(data)
    if m is None:
        return False
    return hashlib.sha256(data[m.end() :]).hexdigest()[:16] == m.group(2).decode()


class SkillsMixin:
    """Skill seeding, as part of ``Registry``. Static throughout: each
    method works on the workspace it is handed.
    """

    @staticmethod
    def _seed_skills(ws: Workspace, *, wsgit: bool) -> None:
        """Install starter skills into a fresh session: each child
        directory of NONTAINER_STUDIO_SKILLS (default: the repo's
        skills/) plus any skills EMBEDDED in granted python libraries
        (<pkg>/skills/ — the nontainer convention). Best-effort: a bad
        skill must never block a session.

        ``wsgit`` is whether this session carries the verb, which a
        gated skill's own predicate may need: what decides is what the
        session GOT, not what a knob asked for.

        Skill text is resolved for the executor first (see
        ``_resolve_skill_text``) — the seeded copy must not teach
        affordances this session doesn't have.
        """
        from nontainer import skills

        root = Path(
            os.getenv("NONTAINER_STUDIO_SKILLS")
            or Path(__file__).resolve().parent.parent / "skills"
        ).expanduser()
        if root.is_dir():
            for child in sorted(root.iterdir()):
                if child.is_dir() and (child / "SKILL.md").is_file():
                    gate = _GATED_SKILLS.get(child.name)
                    if gate is not None and not gate(wsgit):
                        continue
                    try:
                        installed = skills.install(ws, child)
                    except Exception:
                        continue
                    if SkillsMixin._write_generated(ws, child.name, installed):
                        if ws.caps.versioned and ws.uncommitted:
                            ws.commit(info={"tool": "skill", "skill": installed})
        try:
            skills.install_from_modules(ws)
        except Exception:
            pass
        try:
            SkillsMixin._resolve_skill_conditionals(ws)
        except Exception:
            pass  # a skill that won't resolve is still better than none

    @staticmethod
    def _skill_seeds(*, wsgit: bool) -> list[Path]:
        """The skill directories this session would be seeded from: each
        child of NONTAINER_STUDIO_SKILLS holding a SKILL.md, minus the
        gated ones this session cannot follow."""
        root = Path(
            os.getenv("NONTAINER_STUDIO_SKILLS")
            or Path(__file__).resolve().parent.parent / "skills"
        ).expanduser()
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
    def _write_generated(ws: Workspace, seed: str, installed: str) -> bool:
        """Bring the generated files of the skill seeded from directory
        ``seed`` up to date in its installed tree. True when anything
        changed. Best-effort, like seeding.

        Generated files describe the server, not the session, so unlike
        the directory's own files a pristine one follows the server: it
        is rewritten when its source changed (a package was upgraded)
        and removed when its source is gone (one was uninstalled). A
        file the agent edited no longer matches its stamp, and is left
        as it is either way.
        """
        make = _GENERATED_SKILL_FILES.get(seed)
        if make is None:
            return False
        try:
            files = {
                rel: _stamp_generated(label, body)
                for rel, (label, body) in make().items()
            }
        except Exception:
            return False
        fs = ws.files.fs
        root = f"{ws.root}/skills/{installed}"
        changed = False
        for rel, data in files.items():
            dest = f"{root}/{rel}"
            try:
                if fs.exists(dest):
                    old = fs.read(dest)
                    if old == data or not _is_pristine_generated(old):
                        continue
                else:
                    fs.makedirs(dest.rsplit("/", 1)[0], exist_ok=True)
                fs.write(dest, data)
                changed = True
            except Exception:
                continue
        for path in SkillsMixin._walk_files(fs, root):
            if path[len(root) + 1 :] in files:
                continue
            try:
                if _is_pristine_generated(fs.read(path)):
                    fs.remove(path)
                    changed = True
            except Exception:
                continue
        return changed

    @staticmethod
    def _walk_files(fs: Any, root: str) -> list[str]:
        """Every file under ``root``, or none when it cannot be listed."""
        out = []
        try:
            names = sorted(fs.list(root))
        except Exception:
            return out
        for name in names:
            path = f"{root}/{name}"
            if fs.isdir(path):
                out.extend(SkillsMixin._walk_files(fs, path))
            else:
                out.append(path)
        return out

    @staticmethod
    def _top_up_skills(ws: Workspace, *, wsgit: bool) -> None:
        """Add to an existing session's skill tree the seed files it
        lacks, and touch nothing it has.

        A skill tree is seeded once and then is the session's own
        versioned state, so a reseed would clobber what an agent edited.
        But the notes and the skill text a session receives are the
        current ones, and they name files the current seed carries: a
        session created before a reference existed would be told to
        cat a file that is not there. The additive pass closes that
        gap: a seed file with no counterpart in the tree is written, an
        existing file is left as it is, whoever wrote it. A seed file
        the agent deleted comes back on the next open, which the studio
        cannot tell apart from staleness. Generated files are the one
        exception to "touch nothing": a pristine one is also refreshed
        or removed (see ``_write_generated``). Best-effort, like seeding.
        """
        added = False
        for seed in SkillsMixin._skill_seeds(wsgit=wsgit):
            for path in sorted(p for p in seed.rglob("*") if p.is_file()):
                dest = (
                    f"{ws.root}/skills/{seed.name}/{path.relative_to(seed).as_posix()}"
                )
                try:
                    if ws.files.fs.exists(dest):
                        continue
                    ws.files.fs.makedirs(dest.rsplit("/", 1)[0], exist_ok=True)
                    ws.files.fs.write(dest, path.read_bytes())
                    added = True
                except Exception:
                    continue
            if SkillsMixin._write_generated(ws, seed.name, seed.name):
                added = True
        if not added:
            return
        try:
            SkillsMixin._resolve_skill_conditionals(ws)
        except Exception:
            pass
        if ws.caps.versioned and ws.uncommitted:
            ws.commit(info={"tool": "skill", "skill": "top-up"})

    # Conditional blocks in seeded SKILL.md files: a `commands` block is
    # kept where the executor runs injected terminal builtins, a
    # `no-commands` block where the terminal is a real shell that does
    # not. Skill text that teaches a builtin on a rung without one costs
    # the agent a turn to discover, so text about one is written in a
    # block rather than unconditionally. The portable `ws-*` verbs are
    # NOT this distinction — they ferry into a guest, so they answer on
    # every rung and need no gate.
    _IF_BLOCK = re.compile(
        r"[ \t]*<!--if:(commands|no-commands)-->[ \t]*\n(.*?)[ \t]*<!--endif-->[ \t]*\n?",
        re.DOTALL,
    )

    @staticmethod
    def _resolve_skill_text(text: str, *, commands: bool) -> str:
        """Keep the blocks matching this executor, drop the others."""
        want = "commands" if commands else "no-commands"

        def _pick(m: "re.Match[str]") -> str:
            return m.group(2) if m.group(1) == want else ""

        return SkillsMixin._IF_BLOCK.sub(_pick, text)

    @staticmethod
    def _resolve_skill_conditionals(ws: Workspace) -> None:
        """Rewrite seeded SKILL.md files in place for this executor.

        Post-install rather than pre-install because ``skills.install``
        takes a directory of bytes; rewriting the installed copy keeps
        the source skill single-sourced (one file, both rungs) instead
        of forking it into per-executor variants that drift.
        """
        root = f"{ws.root}/skills"
        if not ws.files.fs.isdir(root):
            return
        commands = ws.runtime.supports_commands
        changed = False
        for name in sorted(ws.files.fs.list(root)):
            path = f"{root}/{name}/SKILL.md"
            if not ws.files.fs.exists(path):
                continue
            text = ws.files.fs.read(path).decode("utf-8", "replace")
            resolved = SkillsMixin._resolve_skill_text(text, commands=commands)
            if resolved != text:
                ws.files.fs.write(path, resolved.encode())
                changed = True
        if changed and ws.caps.versioned and ws.uncommitted:
            ws.commit(info={"tool": "skill", "skill": "resolve-conditionals"})
