#!/usr/bin/env python3
"""Baton: Claude (conductor / monitor) drives Codex CLI (executor).

Every Codex run is a "leg": `codex exec` (first leg) or `codex exec resume <thread>` (later legs).
A leg ends when the executor finishes, asks for a decision, hands in a major-change report,
gets blocked, or is stopped. Legs run detached, so they survive the Claude session; Claude
blocks on `baton wait` (in the background) and is woken by leg ends and 30-minute ticks.

State lives in <project>/.baton/ (see README). Python 3.9+, stdlib only.
"""
import argparse
import datetime
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time

try:
    import fcntl
except ImportError:
    fcntl = None

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(HERE)
TEMPLATES = os.path.join(SKILL_DIR, "templates")
REFERENCES = os.path.join(SKILL_DIR, "references")
sys.path.insert(0, HERE)
import codex_quota  # noqa: E402
import statusline as baton_statusline  # noqa: E402

# Baton's own records never count as code: not at the top, and not in a nested project either.
EXCLUDE = [":(exclude,glob)**/.baton/**", ":(exclude,glob)**/.council/**"]
# Default info/exclude of the shadow repo, written once when the repo is created (decision D001):
# data/ and models/ are often code packages (src/models/), so only the top-level ones are
# excluded; outputs/, logs/ and checkpoints/ are excluded at any depth.
SHADOW_EXCLUDE_MARK = "# Baton shadow-repo excludes"
SHADOW_EXCLUDE_DEFAULTS = [
    "**/.baton/runs/", "**/.baton/rollback-backup/", "**/.claude/settings.local.json",
    ".venv*/", "venv/", "node_modules/", "__pycache__/", "*.pyc", ".DS_Store",
    "/data/", "/models/", "outputs/", "logs/", "checkpoints/",
]
# Directory names worth showing at init wherever they occur, so the user can confirm them.
BULKY_DIR_NAMES = {"data", "models", "outputs", "logs", "checkpoints"}
# Every variable `git rev-parse --local-env-vars` lists: none may leak into shadow-repo calls.
GIT_LOCAL_ENV_VARS = (
    "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_CONFIG", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT",
    "GIT_OBJECT_DIRECTORY", "GIT_DIR", "GIT_WORK_TREE", "GIT_IMPLICIT_WORK_TREE", "GIT_GRAFT_FILE",
    "GIT_INDEX_FILE", "GIT_NO_REPLACE_OBJECTS", "GIT_REPLACE_REF_BASE", "GIT_PREFIX",
    "GIT_INTERNAL_SUPER_PREFIX", "GIT_SHALLOW_FILE", "GIT_COMMON_DIR",
    "GIT_LITERAL_PATHSPECS", "GIT_GLOB_PATHSPECS", "GIT_NOGLOB_PATHSPECS", "GIT_ICASE_PATHSPECS",
)
TASK_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
LABEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}")
BATON_GITIGNORE = ["runs/", "*.lock", "statusline-saved.json", "tasks/*/state.json", "rollback-backup/", "project-id"]
LEG_END_EVENTS = {
    "done": "DONE", "decision": "DECISION", "report": "REPORT", "blocked": "BLOCKED",
    "failed": "FAILED", "stopped": "STOPPED", "paused_unsupervised": "PAUSED_UNSUPERVISED",
}
NEXT_STEP = {
    "DONE": "按 references/review.md 做最终验收（diff + 测试），再向用户汇报；不要自行提交。",
    "DECISION": "读决策请求文件，按 references/decision.md 决策（按 conductor.council_for_major_decisions 决定是否用 council），然后 baton resume。",
    "REPORT": "按 references/review.md 审阅汇报 HTML，写 .baton/reviews/ 审阅意见，然后 baton resume。",
    "BLOCKED": "看 summary 里缺什么；能解决就解决后 baton resume，涉及权限/网络/费用先问用户。",
    "FAILED": "看错误和 stderr 末尾判断原因（额度、网络、参数）；修正后 baton resume 重试本轮。",
    "STOPPED": "已按指令停止。写纠偏/修复指令后 baton resume。",
    "PAUSED_UNSUPERVISED": "超过监管时限没有 Opus 检查，执行者已被暂停。先做一次监管检查，再 baton resume。",
    "TICK": "按 references/supervision.md 做一次监管检查，结束时运行 baton supervised 记录结论，然后重新后台 baton wait。",
    "QUOTA_LOW": "立刻在回复里提醒用户 Codex 额度不足，然后重新后台 baton wait。",
}


# ---------------------------------------------------------------- helpers

def now():
    return time.time()


def stamp(ts=None, fmt="%Y-%m-%d %H:%M"):
    return datetime.datetime.fromtimestamp(ts or now()).strftime(fmt)


def fmt_dur(sec):
    sec = int(max(0, sec))
    h, rem = divmod(sec, 3600)
    m = rem // 60
    return f"{h}h{m:02d}m" if h else f"{m}m"


def deep_merge(base, over):
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def read_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def write_json(path, data):
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def write_json_keep(path, data):
    """Atomic write that keeps the file's mode and writes through a symlink to its target."""
    real = os.path.realpath(path)
    try:
        mode = stat.S_IMODE(os.stat(real).st_mode)
    except OSError:
        mode = None
    tmp = f"{real}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # never readable by others mid-write
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.chmod(tmp, mode if mode is not None else 0o644)
    os.replace(tmp, real)


def read_text(path, default=""):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return default


def file_size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def read_bytes_since(path, offset):
    """Text appended after byte `offset`; None if the file shrank below it or disappeared."""
    try:
        with open(path, "rb") as f:
            if os.fstat(f.fileno()).st_size < offset:
                return None
            f.seek(offset)
            return f.read().decode("utf-8", "replace")
    except OSError:
        return None if offset > 0 else ""


def proc_cmdline(pid):
    try:
        return subprocess.run(["ps", "-o", "command=", "-p", str(pid)],
                              capture_output=True, text=True).stdout.strip()
    except OSError:
        return ""


def pid_alive(pid, marker=None):
    """Process exists (and, with `marker`, its command line contains it: guards PID reuse)."""
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return marker is None
    return marker is None or marker in proc_cmdline(pid)


def die(msg, code=1):
    sys.stdout.flush()
    print(f"baton: {msg}", file=sys.stderr)
    sys.exit(code)


class ConfigError(Exception):
    """A project configuration error that should be shown to the caller."""


CONDUCTOR_MODELS = {"opus", "sonnet", "haiku", "fable"}
MODES = {"standard", "lean"}


def lean_overrides():
    return {
        "conductor": {"reasoning_effort": "medium"},
        "executor": {
            "model": "gpt-5.6-luna",
            "reasoning_effort": "medium",
            "escalation": {"enabled": False},
        },
        "monitor": {"enabled": False},
        "supervision": {"max_unsupervised_minutes": 0, "interval_minutes": 0},
    }


def run(cmd, cwd=None, env=None, check=True, input_text=None):
    p = subprocess.run(cmd, cwd=cwd, env=env, input=input_text, capture_output=True, text=True)
    if check and p.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} failed: {p.stderr.strip()[:400]}")
    return p.stdout


def safe_quota(block=True, notify=False):
    try:
        return codex_quota.get(block=block, notify=notify)
    except Exception:  # noqa: BLE001 — quota is advisory, never fatal
        return None


# ---------------------------------------------------------------- git checkpoints

def is_git(root):
    p = subprocess.run(["git", "-C", root, "rev-parse", "--is-inside-work-tree"],
                       capture_output=True, text=True, env=clean_git_env())
    return p.returncode == 0 and p.stdout.strip() == "true"


def git(root, *args, env=None, check=True, input_text=None):
    """git in the user's own repository (statusline helpers and v1 tasks)."""
    return run(["git", "-C", root, *args], env=env if env is not None else clean_git_env(),
               check=check, input_text=input_text)


def git_top(root):
    return git(root, "rev-parse", "--show-toplevel").strip()


def snapshot(root, message):
    """Commit object of the whole worktree (tracked + untracked, minus ignored).

    Uses a throwaway index, so the user's index, HEAD and worktree are untouched.
    """
    top = git_top(root)
    head = git(top, "rev-parse", "--verify", "-q", "HEAD", check=False).strip()
    fd, tmp_index = tempfile.mkstemp(prefix="baton-index-")
    os.close(fd)
    try:
        real_index = git(top, "rev-parse", "--git-path", "index", check=False).strip()
        if real_index and not os.path.isabs(real_index):
            real_index = os.path.join(top, real_index)
        if real_index and os.path.exists(real_index):
            # copy2 keeps the index mtime, so git's racy-clean check still catches same-second edits
            shutil.copy2(real_index, tmp_index)  # keeps the stat cache: fast add
        else:
            os.remove(tmp_index)
        env = clean_git_env({"GIT_INDEX_FILE": tmp_index})
        env.setdefault("GIT_AUTHOR_NAME", "Baton")
        env.setdefault("GIT_AUTHOR_EMAIL", "baton@localhost")
        env.setdefault("GIT_COMMITTER_NAME", "Baton")
        env.setdefault("GIT_COMMITTER_EMAIL", "baton@localhost")
        # assume-unchanged / skip-worktree entries would hide local edits from `add -A`;
        # clear them in the copy (skip-worktree only where the file is actually present)
        assumed, skipped = [], []
        for e in git(top, "ls-files", "-v", "-z", env=env, check=False).split("\0"):
            if len(e) < 3:
                continue
            tag, path = e[0], e[2:]
            if tag in "Ss" and os.path.lexists(os.path.join(top, path)):
                skipped.append(path)
            if tag.islower():
                assumed.append(path)
        if assumed:
            git(top, "update-index", "--no-assume-unchanged", "-z", "--stdin", env=env,
                check=False, input_text="".join(p + "\0" for p in assumed))
        if skipped:
            git(top, "update-index", "--no-skip-worktree", "-z", "--stdin", env=env,
                check=False, input_text="".join(p + "\0" for p in skipped))
        for attempt in range(5):
            p = subprocess.run(["git", "-C", top, "add", "-A", "--ignore-errors"], env=env,
                               capture_output=True, text=True)
            if p.returncode in (0, 1):  # 1: some files unreadable, the rest was added
                if p.returncode == 1:
                    print(f"baton: 快照时有文件无法读取，已跳过：{p.stderr.strip()[:300]}", file=sys.stderr)
                break
            time.sleep(0.3 * (attempt + 1))  # usually a file vanished mid-scan; try again
        else:
            raise RuntimeError(f"git add -A 失败：{p.stderr.strip()[:300]}")
        tree = git(top, "write-tree", env=env).strip()
        args = ["commit-tree", tree, "-m", message] + (["-p", head] if head else [])
        return git(top, *args, env=env).strip()
    finally:
        if os.path.exists(tmp_index):
            os.remove(tmp_index)


def _git_author_env(env):
    env.setdefault("GIT_AUTHOR_NAME", "Baton")
    env.setdefault("GIT_AUTHOR_EMAIL", "baton@localhost")
    env.setdefault("GIT_COMMITTER_NAME", "Baton")
    env.setdefault("GIT_COMMITTER_EMAIL", "baton@localhost")
    return env


def clean_git_env(extra=None):
    """os.environ without any variable that redirects git to another repository."""
    env = {k: v for k, v in os.environ.items() if k not in GIT_LOCAL_ENV_VARS}
    if extra:
        env.update(extra)
    return env


def _locked(path):
    """Open path for an exclusive flock; use as a context manager."""
    f = open(path, "w")
    if fcntl:
        fcntl.flock(f, fcntl.LOCK_EX)
    return f


def _probe_ignorecase(directory):
    """True if the file system holding directory ignores letter case; None if it cannot tell."""
    try:
        fd, probe = tempfile.mkstemp(prefix=".baton-case-probe-", dir=directory)
        os.close(fd)
    except OSError:
        return None
    try:
        return os.path.exists(os.path.join(directory, os.path.basename(probe).upper()))
    finally:
        try:
            os.remove(probe)
        except OSError:
            pass


def validate_external_state(project):
    real_state, real_root = os.path.realpath(project.state_dir), os.path.realpath(project.root)
    if real_state == real_root or real_state.startswith(real_root + os.sep):
        raise RuntimeError(f"项目外状态目录 {project.state_dir} 落在项目目录里，快照会把自己也存进去；"
                           "请把 BATON_HOME 设到项目之外")


def ensure_shadow(project):
    """Create the project's private snapshot repo once.

    The repo is built in a temporary directory under snap.lock and renamed into place, so
    concurrent first snapshots never see a half-made repo.  Its default excludes are written
    only here: later edits by the user are never undone.
    """
    validate_external_state(project)
    gd = project.snap_git_dir
    if os.path.isfile(os.path.join(gd, "HEAD")):
        return gd
    os.makedirs(project.state_dir, exist_ok=True)
    with _locked(os.path.join(project.state_dir, "snap.lock")):
        if os.path.isfile(os.path.join(gd, "HEAD")):
            return gd
        if os.path.isdir(gd):
            os.rmdir(gd)  # a leftover empty dir; a non-empty one without HEAD raises here, on purpose
        tmp = f"{gd}.tmp-{os.getpid()}"
        shutil.rmtree(tmp, ignore_errors=True)
        env = clean_git_env()
        run(["git", "init", "-q", "--bare", tmp], env=env)
        # store working-tree bytes exactly: no end-of-line conversion or filters on the way in or out
        run(["git", "--git-dir", tmp, "config", "core.autocrlf", "false"], env=env)
        # git probed case sensitivity where snap.git lives; the project's own volume is what counts
        probe_dir = project.dir if os.path.isdir(project.dir) else project.root
        ignorecase = _probe_ignorecase(probe_dir) if os.path.isdir(probe_dir) else None
        if ignorecase is not None:
            run(["git", "--git-dir", tmp, "config", "core.ignorecase", "true" if ignorecase else "false"], env=env)
        os.makedirs(os.path.join(tmp, "info"), exist_ok=True)
        with open(os.path.join(tmp, "info", "attributes"), "w") as f:
            f.write("* -text -filter -ident -working-tree-encoding\n")
        exclude = os.path.join(tmp, "info", "exclude")
        text = read_text(exclude)
        with open(exclude, "a") as f:
            f.write(("\n" if text and not text.endswith("\n") else "")
                    + SHADOW_EXCLUDE_MARK + "\n" + "\n".join(SHADOW_EXCLUDE_DEFAULTS) + "\n")
        os.rename(tmp, gd)
    return gd


class SnapStore:
    """Where a task's checkpoints live.

    "shadow" (v2): Baton's private repo in the project state dir.  Every git call runs with
    GIT_DIR=<snap.git> and GIT_WORK_TREE=<project dir>, so the user's .git, index, HEAD and refs
    are never touched, and ignore files above the project dir (such as a whitelist .gitignore
    of an enclosing repo) do not apply.
    "repo" (v1 tasks): the user's own repository, as Baton v1 used it.
    """

    def __init__(self, project, kind):
        self.project, self.kind = project, kind
        if kind == "shadow":
            self.cwd = self.top = project.root
            self.git_dir = project.snap_git_dir
        else:
            self.cwd = project.root
            self.top = git_top(project.root)
            self.git_dir = None

    def env(self, extra=None):
        env = clean_git_env()
        if self.kind == "shadow":
            env["GIT_DIR"] = self.git_dir
            env["GIT_WORK_TREE"] = self.top
        if extra:
            env.update(extra)
        return env

    def git(self, *args, env=None, check=True, input_text=None, cwd=None):
        return run(["git", "-C", cwd or self.cwd, *args], env=self.env(env), check=check, input_text=input_text)

    def has_commit(self, rev):
        if self.kind == "shadow" and not os.path.isdir(self.git_dir):
            return False
        return bool(self.git("rev-parse", "--verify", "-q", f"{rev}^{{commit}}", check=False).strip())

    def user_tracked(self):
        """Paths under the project that the user's own repository tracks (decision D002).

        Read-only: `git ls-files` reads the user's index and writes nothing.  Empty when the
        project is not inside a git repository.
        """
        p = subprocess.run(["git", "-C", self.project.root, "ls-files", "-z", "--", "."],
                           env=clean_git_env(), capture_output=True, text=True)
        return {x for x in p.stdout.split("\0") if x} if p.returncode == 0 else set()

    def user_gitlinks(self):
        p = subprocess.run(["git", "-C", self.project.root, "ls-files", "-s", "-z", "--", "."],
                           env=clean_git_env(), capture_output=True, text=True)
        links = set()
        for item in p.stdout.split("\0"):
            if item.startswith("160000 ") and "\t" in item:
                links.add(item.split("\t", 1)[1])
        return links

    def snapshot(self, message):
        """Commit object of the project dir; refs are not moved.

        Covers every file that is not excluded, plus the files the user's repository tracks
        even when an ignore rule matches them, as Baton v1 did.  Entries that an exclude added
        after an earlier snapshot now matches leave the shadow index first, so editing the
        exclude file takes effect for files that were already snapshotted.
        """
        if self.kind == "repo":
            return snapshot(self.project.root, message)
        ensure_shadow(self.project)
        top = self.top
        with _locked(os.path.join(self.project.state_dir, "snap.lock")):  # one shadow-index user at a time
            env = _git_author_env(self.env())
            stale_lock = os.path.join(self.git_dir, "index.lock")
            if os.path.exists(stale_lock):
                os.remove(stale_lock)  # every Baton writer holds snap.lock, so this one is left over
            tracked = self.user_tracked()
            now_ignored = [x for x in run(["git", "-C", top, "ls-files", "-z", "-ci", "--exclude-standard"],
                                          env=env).split("\0") if x and x not in tracked]
            if now_ignored:
                run(["git", "-C", top, "update-index", "--force-remove", "-z", "--stdin"], env=env,
                    input_text="".join(x + "\0" for x in now_ignored))
            for attempt in range(5):
                p = subprocess.run(["git", "-C", top, "add", "-A", "--ignore-errors"], env=env,
                                   capture_output=True, text=True)
                if p.returncode in (0, 1):  # 1: some files unreadable, the rest was added
                    if p.returncode == 1:
                        print(f"baton: 快照时有文件无法读取，已跳过：{p.stderr.strip()[:300]}", file=sys.stderr)
                    break
                time.sleep(0.3 * (attempt + 1))  # usually a file vanished mid-scan; try again
            else:
                raise RuntimeError(f"git add -A 失败：{p.stderr.strip()[:300]}")
            if tracked:
                have = set(run(["git", "-C", top, "ls-files", "-z"], env=env).split("\0"))
                gitlinks = self.user_gitlinks()
                forced = []
                skipped = []
                for path in sorted(tracked - have):
                    if not os.path.lexists(os.path.join(top, path)):
                        continue
                    if _leading_symlink(top, path):
                        skipped.append(f"{path}（路径经过符号链接）")
                    elif _under(path, gitlinks):
                        skipped.append(f"{path}（gitlink）")
                    else:
                        forced.append(path)
                if skipped:
                    print("baton: 强制加入时跳过：" + "，".join(skipped[:20]), file=sys.stderr)
                if forced:
                    add_cmd = ["git", "-C", top, "add", "-f", "--ignore-errors", "--pathspec-from-file=-",
                               "--pathspec-file-nul"]
                    forced_input = "".join(x + "\0" for x in forced)
                    add = subprocess.run(add_cmd, env=dict(env, GIT_LITERAL_PATHSPECS="1"),
                                         input=forced_input, capture_output=True, text=True)
                    if add.returncode != 0:
                        print("baton: 批量强制加入失败，逐个重试：" + add.stderr.strip()[:500], file=sys.stderr)
                        for path in forced:
                            one = subprocess.run(add_cmd, env=dict(env, GIT_LITERAL_PATHSPECS="1"),
                                                 input=path + "\0", capture_output=True, text=True)
                            if one.returncode != 0:
                                print(f"baton: 强制加入失败 {path}：{one.stderr.strip()[:300]}", file=sys.stderr)
            tree = run(["git", "-C", top, "write-tree"], env=env).strip()
            return run(["git", "-C", top, "commit-tree", tree, "-m", message], env=env).strip()


def store_ref(store, task, label, commit):
    ref, k = f"refs/baton/{task}/{label}", 1
    while store.git("rev-parse", "--verify", "-q", ref, check=False).strip():
        k += 1  # never overwrite an existing checkpoint
        ref = f"refs/baton/{task}/{label}-{k}"
    store.git("update-ref", ref, commit)
    return ref


def checkpoint(store, task, label):
    return store_ref(store, task, label, store.snapshot(f"baton checkpoint {task}/{label} {stamp()}"))


def try_checkpoint(project, task, label, st=None):
    """checkpoint() that reports instead of raising: (ref or None, error or None)."""
    try:
        if not project.task_exists(task):
            return None, f"任务 {task} 不存在（没有简报或状态文件）"
        if project.legacy_state_in_flight(task):
            return None, "任务还有 Baton v1 的一轮在运行，请等这一轮结束"
        store = project.store(task, st)
        if store is None:
            return None, "这个任务建立于 Baton v1，项目又不是 git 仓库，没有回滚点"
        ref = checkpoint(store, task, label)
        if store.kind == "shadow" and (project.read_state(task, {}) or {}).get("snapshot_store") != "shadow":
            # from now on this task's refs live in the shadow repo, whatever the task looked like
            project.update_state(task, lambda s: s.update(snapshot_store="shadow"))
        return ref, None
    except Exception as e:  # noqa: BLE001 — a failed snapshot must not take the caller down
        return None, str(e)


def shortstat(store, a, b):
    """(files, insertions, deletions) between two commits, excluding Baton's own dirs."""
    out = store.git("diff", "--shortstat", a, b, "--", ".", *code_excludes(store), check=False)

    def num(pattern):
        m = re.search(pattern, out)
        return int(m.group(1)) if m else 0
    return num(r"(\d+) files? changed"), num(r"(\d+) insertions?"), num(r"(\d+) deletions?")


def diff_tree_entries(store, a, b):
    """[(src_mode, dst_mode, status, top-relative path)] between two commits, under the project.

    Plumbing (diff-tree) so user diff.* settings such as diff.relative or diff.renames
    cannot change the paths we act on.
    """
    raw = store.git("diff-tree", "-r", "--raw", "-z", "--no-renames", a, b, "--", ".", *code_excludes(store))
    parts = raw.split("\0")
    out = []
    i = 0
    while i + 1 < len(parts):
        meta, path = parts[i], parts[i + 1]
        if not meta.startswith(":"):
            i += 1
            continue
        i += 2
        fields = meta[1:].split()
        if len(fields) >= 5:
            out.append((fields[0], fields[1], fields[4][0], path))
    return out


def shadow_excluded_many(store, paths, tracked=None):
    """Return current shadow-excluded paths in one check-ignore subprocess."""
    if store is None or store.kind != "shadow":
        return set()
    tracked = store.user_tracked() if tracked is None else tracked
    candidates = [path for path in paths if path not in tracked]
    if not candidates:
        return set()
    p = subprocess.run(["git", "-C", store.top, "check-ignore", "--no-index", "-z", "--stdin"],
                       env=store.env(), input="".join(path + "\0" for path in candidates),
                       capture_output=True, text=True)
    return {path for path in p.stdout.split("\0") if path} if p.returncode == 0 else set()


def shadow_excluded(store, path, tracked=None):
    """Compatibility wrapper for one path; callers handling many paths use the batch helper."""
    return path in shadow_excluded_many(store, [path], tracked)


# ---------------------------------------------------------------- project / state

def git_repo_top(start):
    """Top of the git repository holding start (or holding its nearest existing ancestor)."""
    probe = os.path.abspath(start)
    while not os.path.isdir(probe) and os.path.dirname(probe) != probe:
        probe = os.path.dirname(probe)
    p = subprocess.run(["git", "-C", probe, "rev-parse", "--show-toplevel"],
                       capture_output=True, text=True, env=clean_git_env())
    return canonical_dir(p.stdout.strip()) if p.returncode == 0 else None


def canonical_dir(path):
    """Absolute real path with the on-disk letter case (APFS ignores case but keeps it).

    Two spellings of one directory must give one project id; os.path.realpath keeps the
    spelling it was given, so on macOS ask the kernel for the path of an open descriptor.
    """
    path = os.path.realpath(os.path.abspath(os.path.expanduser(path)))
    if not os.path.exists(path):
        suffix, ancestor = [], path
        while not os.path.exists(ancestor) and os.path.dirname(ancestor) != ancestor:
            suffix.append(os.path.basename(ancestor))
            ancestor = os.path.dirname(ancestor)
        return os.path.join(canonical_dir(ancestor), *reversed(suffix))
    if sys.platform == "darwin" and fcntl is not None and os.path.isdir(path):
        try:
            fd = os.open(path, os.O_RDONLY)
            try:
                buf = fcntl.fcntl(fd, 50, b"\0" * 1024)  # F_GETPATH
            finally:
                os.close(fd)
            got = buf.split(b"\0", 1)[0].decode("utf-8", "surrogateescape")
            if got and os.path.samefile(got, path):
                return got
        except OSError:
            pass
    return path


def is_initialized(path):
    """`baton init` has run here: .baton/config.json exists (early v1 projects had no baton.json).

    The state root ~/.baton holds only projects/, so it never makes the home directory look
    like a project.
    """
    return os.path.isfile(os.path.join(path, ".baton", "config.json"))


def is_project_dir(path, legacy=True):
    """baton.json marks a project; with legacy=True an initialized .baton/ does too."""
    return os.path.isfile(os.path.join(path, "baton.json")) or (legacy and is_initialized(path))


def nested_project_dirs(root):
    """Nearest nested projects, using the same markers as rollback ownership."""
    found = []
    for directory, names, _ in os.walk(root):
        keep = []
        for name in sorted(names):
            full = os.path.join(directory, name)
            if name in (".git", ".baton", ".council") or os.path.islink(full):
                continue
            if is_project_dir(full):
                found.append(os.path.relpath(full, root).replace(os.sep, "/"))
            else:
                keep.append(name)
        names[:] = keep
    return found


_CODE_EXCLUDES_CACHE = {}
_CODE_NESTED_CACHE = {}


def _shadow_prune_dir(name, rel_dir):
    """Fast directory pruning for the shadow's built-in exclude rules."""
    if name.startswith(".venv") or name in {"venv", "node_modules", "__pycache__",
                                              "outputs", "logs", "checkpoints"}:
        return True
    # D001 intentionally excludes only the project's top-level data/ and models/.
    return rel_dir == "." and name in {"data", "models"}


def nested_project_dirs_for_code(root):
    """Nested projects that can affect code statistics, skipping shadow-excluded trees."""
    if root in _CODE_NESTED_CACHE:
        return list(_CODE_NESTED_CACHE[root])
    found = []
    for directory, names, _ in os.walk(root):
        rel_dir = os.path.relpath(directory, root)
        keep = []
        for name in sorted(names):
            full = os.path.join(directory, name)
            if name in (".git", ".baton", ".council") or os.path.islink(full):
                continue
            if _shadow_prune_dir(name, rel_dir):
                continue
            if is_project_dir(full):
                found.append(os.path.relpath(full, root).replace(os.sep, "/"))
            else:
                keep.append(name)
        names[:] = keep
    _CODE_NESTED_CACHE[root] = found
    return list(found)


def code_excludes(store):
    # Quote glob metacharacters so directory names such as "project[1]" stay literal.
    root = store.project.root
    paths = _CODE_EXCLUDES_CACHE.get(root)
    if paths is None:
        nested = nested_project_dirs_for_code(root)
        paths = ["".join("[" + c + "]" if c in "[*?" else c for c in path)
                 for path in nested]
        _CODE_EXCLUDES_CACHE[root] = paths
    return EXCLUDE + [":(exclude,glob)" + path + "/**" for path in paths]


def find_project_dir(start, legacy=True):
    """Find the nearest project dir without crossing the containing git root.

    `init` passes legacy=False: a new project is placed by baton.json only, so an early-v1
    .baton/ higher up cannot capture it without the boundary confirmation.
    """
    start = canonical_dir(start)
    top = git_repo_top(start)
    if not top:
        return start
    cur = start
    while True:
        if is_project_dir(cur, legacy):
            return cur
        if cur == top:
            break
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return start


def task_has_v1_refs(st):
    """True when Baton v1 stored checkpoints of this task in the user's repository."""
    if st.get("snapshot_store") == "shadow":
        return False
    return bool(st.get("base_ref") or any(lg.get("start_ref") or lg.get("end_ref") for lg in st.get("legs", [])))


class Project:
    def __init__(self, root):
        self.root = canonical_dir(root)
        self.dir = os.path.join(self.root, ".baton")

    @property
    def project_id(self):
        real = os.path.realpath(self.root)
        digest = hashlib.sha1(os.fsencode(real)).hexdigest()[:8]
        return f"{os.path.basename(real)}-{digest}"

    @property
    def id_file(self):
        return self.p("project-id")

    def recorded_id(self):
        data = read_json(self.id_file)
        return data.get("id") if isinstance(data, dict) else None

    def record_id(self):
        write_json(self.id_file, {"id": self.project_id, "root": self.root})

    def check_identity(self, warn_only=False):
        """Refuse to run on a fresh state dir when the project folder was moved or renamed."""
        old = self.recorded_id()
        if old and old != self.project_id:
            old_dir = os.path.join(os.path.dirname(self.state_dir), old)
            if os.path.isdir(old_dir):
                hint = "移动项目用 baton init --relink；复制项目用 baton init --fresh。"
            else:
                hint = "原状态目录已不存在，移动用 baton init --relink；复制或在新机器上用 baton init --fresh。"
            message = (f"项目目录换过位置或名字：.baton/project-id 记的是 {old}，现在算出来是 {self.project_id}。"
                       f"原来的任务状态和回滚点在 {old_dir}。{hint}"
                       "否则新开的轮次会覆盖 .baton/runs/ 里的旧记录。")
            if warn_only:
                print("警告：" + message, file=sys.stderr)
                return False
            die(message)
        return True

    @property
    def state_dir(self):
        home = os.environ.get("BATON_HOME") or os.path.expanduser("~/.baton")
        return os.path.join(os.path.abspath(os.path.expanduser(home)), "projects", self.project_id)

    @property
    def snap_git_dir(self):
        return os.path.join(self.state_dir, "snap.git")

    def store(self, task=None, st=None):
        """SnapStore for a task: the shadow repo, unless Baton v1 already put its refs in the user repo.

        A v1 task without any v1 ref (e.g. one in a non-git project) has nothing to stay
        compatible with, so it moves to the shadow repo with its next checkpoint.
        """
        if task is not None:
            if st is None:
                st = self.read_state(task, {}) or {}
            if task_has_v1_refs(st):
                return SnapStore(self, "repo") if is_git(self.root) else None
        return SnapStore(self, "shadow")

    def config_layers(self):
        """Keep the raw project layers available for v1 migration decisions."""
        project_cfg = read_json(os.path.join(self.dir, "config.json"), {})
        if not isinstance(project_cfg, dict):
            project_cfg = {}
        return project_cfg, self.read_baton_config()

    @property
    def needs_backend_migration(self):
        return not any(isinstance(layer.get("executor"), dict) and "backend" in layer["executor"]
                       for layer in self.config_layers())

    @property
    def config(self):
        base = read_json(os.path.join(SKILL_DIR, "config.json"), {})
        project_cfg, baton_cfg = self.config_layers()
        # quota belongs to the account-level skill configuration.  Keep the
        # project layers from changing it even though all other keys deep-merge.
        project_cfg = {k: v for k, v in project_cfg.items() if k != "quota"}
        baton_cfg = {k: v for k, v in baton_cfg.items() if k != "quota"}
        layers = (project_cfg, baton_cfg)
        mode = base.get("mode", "standard")
        for layer in layers:
            if "mode" in layer:
                mode = layer["mode"]
        if mode not in MODES:
            raise ConfigError(f"mode 的值 {mode!r} 无效，只能是 standard 或 lean")
        defaults = deep_merge(base, lean_overrides()) if mode == "lean" else base
        cfg = deep_merge(deep_merge(defaults, project_cfg), baton_cfg)
        cfg["mode"] = mode
        project_conductor = any("conductor" in layer for layer in layers)
        project_judge = any("judge" in layer for layer in layers)
        if project_judge:
            legacy_judge = {}
            for layer in layers:
                if isinstance(layer.get("judge"), dict):
                    legacy_judge = deep_merge(legacy_judge, layer["judge"])
            if not project_conductor:
                # v1: judge was the conductor.  The monitor remains at its v2 default.
                cfg["conductor"] = dict(cfg["conductor"], model=legacy_judge.get("model"))
                if "council_for_major_decisions" in legacy_judge:
                    cfg["conductor"]["council_for_major_decisions"] = legacy_judge["council_for_major_decisions"]
            else:
                # Early v2 files used judge for the read-only monitor.  Merge it as an alias,
                # while an explicit monitor in the same layer takes precedence.
                monitor = dict(defaults.get("monitor") or {})
                for layer in layers:
                    if isinstance(layer.get("judge"), dict):
                        monitor = deep_merge(monitor, layer["judge"])
                    if isinstance(layer.get("monitor"), dict):
                        monitor = deep_merge(monitor, layer["monitor"])
                cfg["monitor"] = monitor
        cfg.pop("judge", None)
        for role in ("conductor", "monitor"):
            section = cfg.get(role)
            if not isinstance(section, dict):
                raise ConfigError(f"配置中的 {role} 必须是 JSON object")
            model = section.get("model")
            if role == "conductor":
                if not isinstance(model, str) or model not in CONDUCTOR_MODELS:
                    raise ConfigError(
                        f"conductor.model 的值 {model!r} 无效，只能是 opus、sonnet、haiku 或 fable"
                    )
            elif not isinstance(model, str) or not model.strip():
                raise ConfigError("monitor.model 必须是非空字符串")
        if not isinstance(cfg.get("executor"), dict):
            raise ConfigError("配置中的 executor 必须是 JSON object")
        if not any(isinstance(layer.get("executor"), dict) and "backend" in layer["executor"]
                   for layer in layers):
            cfg["executor"]["backend"] = "codex"
        cfg["project"] = {"root": self.root, "id": self.project_id, "state_dir": self.state_dir}
        return cfg

    @property
    def judge_renamed_to_monitor(self):
        project_cfg, baton_cfg = self.config_layers()
        return (any("conductor" in layer for layer in (project_cfg, baton_cfg)) and
                any("judge" in layer for layer in (project_cfg, baton_cfg)))

    @property
    def baton_config_path(self):
        return os.path.join(self.root, "baton.json")

    def read_baton_config(self):
        path = self.baton_config_path
        if not os.path.exists(path):
            return {}
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            raise ConfigError(f"{path} 不是合法 JSON：第 {e.lineno} 行第 {e.colno} 列：{e.msg}") from e
        except OSError as e:
            raise ConfigError(f"无法读取 {path}：{e}") from e
        if not isinstance(data, dict):
            raise ConfigError(f"{path} 顶层必须是 JSON object，实际是 {type(data).__name__}")
        return data

    def config_paths(self):
        return [
            (os.path.join(SKILL_DIR, "config.json"), True),
            (os.path.join(self.dir, "config.json"), os.path.exists(os.path.join(self.dir, "config.json"))),
            (self.baton_config_path, os.path.exists(self.baton_config_path)),
        ]

    def p(self, *parts):
        return os.path.join(self.dir, *parts)

    def task_dir(self, task):
        return self.p("tasks", task)

    def state_task_dir(self, task):
        return os.path.join(self.state_dir, "tasks", task)

    def runs_dir(self, task):
        return self.p("runs", task)

    def leg_path(self, task, n, suffix):
        return os.path.join(self.runs_dir(task), f"leg-{n:03d}.{suffix}")

    def state_path(self, task):
        return os.path.join(self.state_task_dir(task), "state.json")

    def state_lock_path(self, task):
        return os.path.join(self.state_task_dir(task), "state.lock")

    def launch_lock_path(self):
        return os.path.join(self.state_dir, "launch.lock")

    def legacy_state_path(self, task):
        return os.path.join(self.task_dir(task), "state.json")

    def import_legacy_state(self, task):
        """Import a v1 task state once, under the state lock; project files stay where they are.

        While a Baton v1 runner is still running the task's leg, nothing is imported: that
        runner keeps writing the old file, so a copy would split the state.  The v1 state is
        returned instead, for read-only use.
        """
        target = self.state_path(task)
        if os.path.exists(target):
            return None
        legacy = self.legacy_state_path(task)
        old = read_json(legacy) if os.path.isfile(legacy) else None
        if not isinstance(old, dict):
            return None
        leg = old["legs"][-1] if old.get("legs") else None
        if self.legacy_state_in_flight(task, old=old, leg=leg):
            return old
        os.makedirs(self.state_task_dir(task), exist_ok=True)
        with _locked(self.state_lock_path(task)):
            if not os.path.exists(target):
                old["imported_from_v1"] = stamp()
                write_json(target, old)
        return None

    def legacy_state_in_flight(self, task, old=None, leg=None):
        """Whether a v1 runner may still be writing the project-local state file."""
        if os.path.exists(self.state_path(task)):
            return False
        if old is None:
            legacy = self.legacy_state_path(task)
            old = read_json(legacy) if os.path.isfile(legacy) else None
        if not isinstance(old, dict):
            return False
        if leg is None:
            leg = old["legs"][-1] if old.get("legs") else None
        if not leg or leg.get("status") != "running":
            return False
        pid = leg.get("runner_pid")
        if pid:
            marker = runner_marker(task, leg.get("n"))
            if pid_alive(pid, marker):
                return True
            # A live PID without the marker may be a reused PID.  Keep the conservative
            # v1 protection for one day, then allow the stale state to be imported.
            started = leg.get("started_at")
            try:
                recent = started is not None and now() - float(started) <= 24 * 60 * 60
            except (TypeError, ValueError):
                recent = False
            return recent and pid_alive(pid)
        started = leg.get("started_at")
        try:
            return started is not None and now() - float(started) < 30
        except (TypeError, ValueError):
            return False

    def task_exists(self, task):
        return (os.path.isfile(os.path.join(self.task_dir(task), "brief.md"))
                or os.path.exists(self.state_path(task))
                or os.path.exists(self.legacy_state_path(task)))

    def read_state(self, task, default=None):
        in_flight = self.import_legacy_state(task)
        if in_flight is not None:
            return in_flight
        return read_json(self.state_path(task), default)

    def require_init(self):
        if not is_initialized(self.root):
            die(f"{self.root} 还没有初始化，先运行 baton init")

    def load_state(self, task):
        st = self.read_state(task)
        if st is None:
            die(f"任务 {task} 不存在（{self.state_path(task)}）")
        return st

    def update_state(self, task, fn):
        """Read-modify-write the task state under a file lock."""
        if self.import_legacy_state(task) is not None:
            die(f"任务 {task} 还有 Baton v1 的一轮在运行，它还在写旧的状态文件；"
                "等这一轮结束再用新版本操作这个任务")
        os.makedirs(self.state_task_dir(task), exist_ok=True)
        with open(self.state_lock_path(task), "w") as lock:
            if fcntl:
                fcntl.flock(lock, fcntl.LOCK_EX)
            st = read_json(self.state_path(task), {})
            fn(st)
            write_json(self.state_path(task), st)
            return st

    def tasks(self):
        found = set()
        for d in (self.p("tasks"), os.path.join(self.state_dir, "tasks")):
            if os.path.isdir(d):
                found.update(name for name in os.listdir(d)
                             if os.path.isdir(os.path.join(d, name)) and TASK_RE.fullmatch(name))
        return sorted(found)


def current_leg(st):
    return st["legs"][-1] if st.get("legs") else None


def check_task(task):
    if not TASK_RE.fullmatch(task or ""):
        die("任务名只能用字母、数字、下划线和连字符，以字母或数字开头，不超过 64 个字符")


def runner_marker(task, n):
    return f"_run {task} {n}"


def codex_alive(leg):
    """The leg's own codex process is running (identified by its unique -o path, not just 'codex')."""
    return bool(leg) and not leg.get("codex_exited") and pid_alive(leg.get("codex_pid"), leg.get("last") or "\0")


def kill_codex(leg):
    """Stop the leg's codex process group (codex is started as a session leader). True if it was alive."""
    if not codex_alive(leg):
        return False
    pid = leg["codex_pid"]
    for sig, grace in ((signal.SIGTERM, 8), (signal.SIGKILL, 3)):
        try:
            os.killpg(pid, sig)
        except (ProcessLookupError, PermissionError):
            return True
        deadline = now() + grace
        while now() < deadline:
            if not pid_alive(pid):
                return True
            time.sleep(0.2)
    return True


def refresh_runner_liveness(project, task, st):
    """A leg marked running whose runner is gone died without cleanup: stop its codex, mark failed."""
    if project.legacy_state_in_flight(task):
        return st
    leg = current_leg(st)
    if not leg or leg.get("status") != "running":
        return st
    if not leg.get("runner_pid") and now() - leg["started_at"] < 30:
        return st  # launch_leg has not recorded the runner pid yet
    if pid_alive(leg.get("runner_pid"), runner_marker(task, leg["n"])):
        return st
    killed = kill_codex(leg)

    def mark(s):
        lg = current_leg(s)
        if lg.get("status") == "running":
            lg["status"] = "failed"
            lg["error"] = "runner 进程意外退出（机器重启或被杀）" + ("；已终止残留的 codex 进程" if killed else "")
            lg["ended_at"] = now()
            lg["codex_exited"] = True
            s["status"] = "idle"
    return project.update_state(task, mark)


def mark_reported(project, task):
    def mark(s):
        if current_leg(s):
            current_leg(s)["reported"] = True
    project.update_state(task, mark)


def surface_unreported(project, task, st, action):
    """If the last leg ended on its own and nobody has seen its event yet, show it and stop."""
    leg = current_leg(st)
    if leg and leg["status"] not in ("running", "stopped") and not leg.get("reported"):
        print(leg_event_text(project, task, leg))
        mark_reported(project, task)
        die(f"第 {leg['n']} 轮在{action}前已经自己结束（{leg['status']}），上面的事件还没处理过。"
            f"先按事件处理；处理完再执行同一条命令即可。", 4)


# ---------------------------------------------------------------- events digest

def read_events(path, offset=0):
    """Complete JSONL events after byte `offset`; returns (events, new_offset)."""
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            data = f.read()
    except OSError:
        return [], offset
    cut = data.rfind(b"\n")
    if cut < 0:
        return [], offset
    events = []
    for line in data[:cut + 1].splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if isinstance(ev, dict):
            events.append(ev)
    return events, offset + cut + 1


def thread_id_from(path):
    events, _ = read_events(path)
    for ev in events:
        if ev.get("type") == "thread.started" and ev.get("thread_id"):
            return ev["thread_id"]
    return None


def digest(events):
    cmds, files, msgs, errors, running = [], {}, [], [], {}
    usage = {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0}
    for ev in events:
        et = ev.get("type", "")
        item = ev.get("item") if isinstance(ev.get("item"), dict) else {}
        it = item.get("type", "")
        if et == "item.started" and it == "command_execution":
            running[item.get("id")] = item.get("command", "")
        elif et == "item.completed":
            if it == "command_execution":
                running.pop(item.get("id"), None)
                cmds.append((item.get("command", ""), item.get("exit_code")))
            elif it == "file_change":
                for ch in item.get("changes") or []:
                    if isinstance(ch, dict):
                        files[ch.get("path", "?")] = ch.get("kind", "?")
            elif it == "agent_message":
                msgs.append(item.get("text", ""))
            elif it == "error":
                errors.append(item.get("message", json.dumps(item, ensure_ascii=False)))
        elif et in ("turn.failed", "error"):
            err = ev.get("error") or ev
            errors.append(err.get("message") if isinstance(err, dict) else str(err))
        elif et == "turn.completed":
            u = ev.get("usage") if isinstance(ev.get("usage"), dict) else {}
            for k in usage:
                try:
                    usage[k] += int(u.get(k) or 0)
                except (TypeError, ValueError):
                    pass
    return {"commands": cmds, "files": files, "messages": msgs, "errors": errors,
            "running": list(running.values()), "usage": usage}


def fmt_usage(u):
    def k(n):
        return f"{n / 1e6:.2f}M" if n >= 1e6 else f"{n / 1e3:.0f}k"
    return f"输入 {k(u['input_tokens'])}（缓存 {k(u['cached_input_tokens'])}），输出 {k(u['output_tokens'])}"


def clip(text, n):
    text = (text or "").strip()
    return text if len(text) <= n else text[:n] + " …"


# ---------------------------------------------------------------- prompts

def fill(template, mapping):
    for k, v in mapping.items():
        template = template.replace("{{" + k + "}}", str(v))
    return template


def protocol_text(project, task):
    cfg = project.config
    protocol_name = "executor-protocol-lean.md" if cfg.get("mode") == "lean" else "executor-protocol.md"
    return fill(read_text(os.path.join(REFERENCES, protocol_name)), {
        "TASK": task,
        "BATON_DIR": project.dir,
        "LOG": project.p("log.md"),
        "REPORTS": project.p("reports"),
        "DECISIONS": project.p("decisions"),
        "REPORT_TEMPLATE": os.path.join(TEMPLATES, "report.html"),
        "DECISION_TEMPLATE": os.path.join(TEMPLATES, "decision.md"),
        "MAJOR_FILES": cfg["change_size"]["major_files"],
        "MAJOR_LINES": cfg["change_size"]["major_lines"],
        "LANGUAGE": cfg.get("language", "中文"),
        "TEST_COMMAND": cfg["supervision"].get("test_command") or "（未配置，按简报中的验证方式）",
    })


def codex_command(project, kind, thread_id, last_path):
    ex = project.config["executor"]
    cmd = ["codex", "exec"] + (["resume"] if kind == "resume" else [])
    cmd += ["--json", "-m", ex["model"],
            "-c", f'model_reasoning_effort="{ex["reasoning_effort"]}"',
            "-c", f'service_tier="{ex["service_tier"]}"',
            "-c", f'sandbox_mode="{ex["sandbox"]}"',
            "-c", 'approval_policy="never"',
            "--output-schema", os.path.join(TEMPLATES, "leg-result.schema.json"),
            "-o", last_path]
    if ex["sandbox"] == "workspace-write":
        net = "true" if ex.get("network_access") else "false"
        cmd += ["-c", f"sandbox_workspace_write.network_access={net}"]
    if ex.get("disable_fast_mode"):
        cmd += ["--disable", "fast_mode"]
    for kv in ex.get("extra_config") or []:
        cmd += ["-c", kv]
    if not is_git(project.root):
        cmd += ["--skip-git-repo-check"]
    if kind == "start":
        cmd += ["-C", project.root, "-"]
    else:
        cmd += [thread_id, "-"]
    return cmd


# ---------------------------------------------------------------- project statusline

def settings_local_path(project):
    return os.path.join(project.root, ".claude", "settings.local.json")


def skills_dir():
    return os.environ.get("CLAUDE_SKILLS_DIR") or os.path.join(
        os.path.dirname(baton_statusline.user_settings_path()), "skills")


def statusline_script_path():
    """Prefer the installed skills/baton link, which survives moving the clone."""
    ours = os.path.join(HERE, "statusline.py")
    link = os.path.join(skills_dir(), "baton", "scripts", "statusline.py")
    try:
        if os.path.exists(link) and os.path.samefile(link, ours):
            return link
    except OSError:
        pass
    return ours


def statusline_entry(project, previous):
    """The statusLine object Baton installs; refreshInterval follows the statusline it wraps."""
    interval = previous.get("refreshInterval") if isinstance(previous, dict) else None
    for path in (os.path.join(project.root, ".claude", "settings.json"), baton_statusline.user_settings_path()):
        if interval:
            break
        sl = (read_json(path, {}) or {}).get("statusLine") if isinstance(read_json(path, {}), dict) else None
        if isinstance(sl, dict) and not baton_statusline.is_baton_command(sl.get("command")):
            interval = sl.get("refreshInterval")
    command = f"python3 {shlex.quote(statusline_script_path())} {baton_statusline.MARK}"
    return {"type": "command", "command": command, "refreshInterval": interval or 60}


def statusline_script_ok(command):
    """The script named in a Baton statusLine command still exists."""
    try:
        parts = shlex.split(command or "")
    except ValueError:
        return False
    return len(parts) >= 2 and os.path.exists(parts[1])


def settings_rel_top(project):
    """Path of the project's settings.local.json relative to the git top level."""
    prefix = git(project.root, "rev-parse", "--show-prefix", check=False).rstrip("\n")
    return prefix + ".claude/settings.local.json"


def gitignore_escape(rel):
    out = re.sub(r"([\\*?\[])", r"\\\1", rel)
    if out.endswith(" "):
        out = out[:-1] + "\\ "
    return out


def settings_tracked(project):
    if not is_git(project.root):
        return False
    top = git_top(project.root)
    return subprocess.run(["git", "-C", top, "ls-files", "--error-unmatch", "--",
                           ":(icase)" + settings_rel_top(project)], capture_output=True).returncode == 0


def settings_ignored(project):
    top = git_top(project.root)
    return subprocess.run(["git", "-C", top, "check-ignore", "-q", "--", settings_rel_top(project)]).returncode == 0


def ensure_settings_ignored(project):
    """Keep the machine-local settings file out of git without touching the project's .gitignore.

    Returns a warning string when the file still is not ignored, else None.
    """
    if not is_git(project.root):
        return None
    if settings_tracked(project):
        return "注意：.claude/settings.local.json 已被 git 跟踪，里面的本机路径会被提交"
    if settings_ignored(project):
        return None
    top = git_top(project.root)
    pattern = "/" + gitignore_escape(settings_rel_top(project))
    path = git(top, "rev-parse", "--git-path", "info/exclude").strip()
    path = path if os.path.isabs(path) else os.path.join(top, path)
    if pattern not in read_text(path).splitlines():
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as f:
            f.write(f"\n# Baton: machine-local Claude Code settings\n{pattern}\n")
    if not settings_ignored(project):
        return "注意：.claude/settings.local.json 没能被 git 忽略（项目规则又把它放行了），提交前请检查"
    return None


def baton_statusline_installed(project):
    data = read_json(settings_local_path(project))
    current = data.get("statusLine") if isinstance(data, dict) else None
    return isinstance(current, dict) and baton_statusline.is_baton_command(current.get("command"))


def statusline_install(project, force=False):
    """Point the project's statusLine at Baton's wrapper; the previous one stays as line 1."""
    path = settings_local_path(project)
    root_real = os.path.realpath(project.root)
    real = os.path.realpath(path)
    if not force and not (real == root_real or real.startswith(root_real + os.sep)):
        return f"未改 statusline：{path} 是指向项目外的符号链接（{real}）。确实要装就运行 baton statusline install --force"
    if settings_tracked(project) and not force:
        return (f"未改 statusline：{path} 已被 git 跟踪，写入本机路径会被提交。"
                f"确实要装就运行 baton statusline install --force")
    created = not os.path.lexists(path)
    data = {}
    if not created:
        data = read_json(path)
        if not isinstance(data, dict):
            return f"未配置 statusline：{path} 读不出合法的 JSON 对象（文件损坏或是失效的符号链接），请手动检查"
    current = data.get("statusLine")
    if isinstance(current, dict) and baton_statusline.is_baton_command(current.get("command")):
        fresh = statusline_entry(project, current)
        if current.get("command") != fresh["command"]:
            current["command"] = fresh["command"]  # skill moved: refresh the script path
            try:
                write_json_keep(path, data)
            except OSError as e:
                return f"未能更新 statusline：{e}"
        warn = ensure_settings_ignored(project)
        return f"statusline 已是 Baton 版本（{path}）" + (f"\n{warn}" if warn else "")
    data["statusLine"] = statusline_entry(project, current)
    claude_dir_existed = os.path.isdir(os.path.dirname(path))
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        write_json_keep(path, data)
    except OSError as e:
        return f"未配置 statusline：写入 {path} 失败（{e}）"
    os.makedirs(project.dir, exist_ok=True)
    write_json(project.p("statusline-saved.json"),
               {"created_file": created, "had_statusline": current is not None, "previous": current})
    warn = ensure_settings_ignored(project)
    # Claude Code hot-reloads settings files, but only picks up a new file mid-session
    # if its folder already existed when the session started.
    when = ("如果会话启动时 .claude/ 已存在，当前会话会自动加载，否则重开会话后生效" if claude_dir_existed
            else "本次新建了 .claude/ 目录，需要重开 Claude Code 会话后生效")
    return (f"statusline 已配置：{path}\n原有 statusline 保留为第一行，第二行显示 Codex 额度；{when}。"
            f"撤销用 baton statusline uninstall" + (f"\n{warn}" if warn else ""))


def statusline_uninstall(project):
    path = settings_local_path(project)
    data = read_json(path)
    current = data.get("statusLine") if isinstance(data, dict) else None
    if not (isinstance(current, dict) and baton_statusline.is_baton_command(current.get("command"))):
        return "当前项目的 statusline 不是 Baton 配置的，未改动"
    saved = read_json(project.p("statusline-saved.json"), {}) or {}
    if saved.get("had_statusline") and saved.get("previous") is not None:
        data["statusLine"] = saved["previous"]
    else:
        data.pop("statusLine", None)
    try:
        if not data and saved.get("created_file") and not os.path.islink(path):
            os.remove(path)
        else:
            write_json_keep(path, data)
    except OSError as e:
        return f"撤销失败：{e}"
    try:
        os.remove(project.p("statusline-saved.json"))
    except OSError:
        pass
    return f"已撤销 Baton statusline（{path}）"


def statusline_report(project):
    data = read_json(settings_local_path(project))
    current = data.get("statusLine") if isinstance(data, dict) else None
    installed = isinstance(current, dict) and baton_statusline.is_baton_command(current.get("command"))
    base = baton_statusline.resolve_base(project.root)
    state = "未安装"
    if installed:
        state = "已安装 Baton 版本" if statusline_script_ok(current.get("command")) \
            else "已安装，但命令里的脚本不存在（Baton 被移走了？重新 baton statusline install）"
    return (f"statusline：{state}（{settings_local_path(project)}）\n"
            f"第一行来源：{base or '无'}")


# ---------------------------------------------------------------- commands

def write_baton_gitignore(project):
    gi = project.p(".gitignore")
    have = read_text(gi).splitlines() if os.path.exists(gi) else None
    if have is None:
        os.makedirs(project.dir, exist_ok=True)
        with open(gi, "w") as f:
            f.write("# Baton runtime and machine-local files\n" + "\n".join(BATON_GITIGNORE) + "\n")
        return
    missing = [line for line in BATON_GITIGNORE if line not in have]
    if missing:
        with open(gi, "a") as f:
            f.write("\n" + "\n".join(missing) + "\n")


def _rebase_paths(obj, old_root, new_root):
    """Rewrite absolute paths under old_root (as stored in task state) to new_root."""
    if isinstance(obj, str):
        if obj == old_root or obj.startswith(old_root + os.sep):
            return new_root + obj[len(old_root):]
        return obj
    if isinstance(obj, list):
        return [_rebase_paths(x, old_root, new_root) for x in obj]
    if isinstance(obj, dict):
        return {k: _rebase_paths(v, old_root, new_root) for k, v in obj.items()}
    return obj


def relink_state(project, old_id):
    """After the project folder moved: move its old state dir to the new id and fix stored paths."""
    old_dir = os.path.join(os.path.dirname(project.state_dir), old_id)
    if not os.path.isdir(old_dir):
        die(f"找不到原来的状态目录 {old_dir}；如果这是复制项目或换了机器，请用 baton init --fresh")
    if os.path.exists(project.state_dir):
        die(f"新的状态目录 {project.state_dir} 已经存在，不覆盖；先确认里面没有要保留的东西，移走后再试")
    id_data = read_json(project.id_file) or {}
    old_root = id_data.get("root")
    if old_root and os.path.isdir(old_root):
        original_id = read_json(os.path.join(old_root, ".baton", "project-id")) or {}
        if original_id.get("id") == old_id:
            die(f"检测到这是复制项目：原目录 {old_root} 仍存在且 project-id 仍为 {old_id}；"
                "为避免搬走原项目状态，请改用 baton init --fresh")
    os.rename(old_dir, project.state_dir)
    if old_root and old_root != project.root:
        tasks_dir = os.path.join(project.state_dir, "tasks")
        for task in (os.listdir(tasks_dir) if os.path.isdir(tasks_dir) else []):
            path = os.path.join(tasks_dir, task, "state.json")
            st = read_json(path)
            if isinstance(st, dict):
                write_json(path, _rebase_paths(st, old_root, project.root))
    project.record_id()
    print(f"已把 {old_dir} 接到 {project.state_dir}")


def cmd_init(project, args):
    try:
        validate_external_state(project)
    except RuntimeError as e:
        die(str(e))
    top = git_repo_top(project.root)
    if top and project.root != top:
        print(f"项目目录：{project.root}")
        print(f"git 顶层：{top}")
        if not getattr(args, "accept_boundary", False):
            die("项目目录与 git 顶层不同；确认边界后加 --accept-boundary 重试（未创建任何文件）")
    old_id = project.recorded_id()
    if old_id and old_id != project.project_id:
        relink = getattr(args, "relink", False)
        fresh = getattr(args, "fresh", False)
        if relink and fresh:
            die("--relink 和 --fresh 不能同时使用")
        if fresh:
            if os.path.exists(project.state_dir) and os.listdir(project.state_dir):
                die(f"新的状态目录 {project.state_dir} 已经存在，不覆盖；请先确认后再用 --fresh")
            print(f"复制项目将使用新的项目外状态目录：{project.state_dir}（原状态保留在旧项目）")
        elif not relink:
            project.check_identity()  # explains the move and exits
        else:
            relink_state(project, old_id)
    # Validate an existing baton.json before changing the project state.  When
    # it is absent, the skill defaults and any existing .baton override form
    # the values used for the generated file below.
    project.config
    for d in ("tasks", "reports", "reviews", "decisions", "runs"):
        os.makedirs(project.p(d), exist_ok=True)
    project = Project(project.root)  # the newly created directory now has its on-disk spelling
    os.makedirs(project.state_dir, exist_ok=True)
    write_baton_gitignore(project)
    if not os.path.exists(project.p("config.json")):
        write_json(project.p("config.json"), {
            "_comment": "Project overrides for Baton. Put model and test settings in <project>/baton.json; quota.* is ignored in project files.",
        })
    if not os.path.exists(project.baton_config_path):
        cfg = project.config
        write_json(project.baton_config_path, {
            "conductor": {
                "model": cfg["conductor"]["model"],
                "council_for_major_decisions": cfg["conductor"]["council_for_major_decisions"],
            },
            "monitor": {
                "model": cfg["monitor"]["model"],
                "reasoning_effort": cfg["monitor"].get("reasoning_effort"),
                "final_reasoning_effort": cfg["monitor"].get("final_reasoning_effort"),
                "timeout_minutes": cfg["monitor"].get("timeout_minutes"),
            },
            "executor": {
                "backend": cfg["executor"]["backend"],
                "model": cfg["executor"]["model"],
                "reasoning_effort": cfg["executor"]["reasoning_effort"],
                "service_tier": cfg["executor"]["service_tier"],
                "disable_fast_mode": cfg["executor"].get("disable_fast_mode", True),
                "network_access": cfg["executor"].get("network_access", False),
            },
            "supervision": {
                "test_command": cfg["supervision"].get("test_command", ""),
            },
        })
    if not os.path.exists(project.p("log.md")):
        with open(project.p("log.md"), "w") as f:
            f.write("# Baton 修改日志\n\n执行者每完成一处修改追加一条；major 条目指向 reports/ 下的汇报。\n")
    if not os.path.exists(project.p("supervision.md")):
        with open(project.p("supervision.md"), "w") as f:
            f.write("# Baton 监管记录\n\nOpus 每 30 分钟一次的检查结论。\n")
    ensure_shadow(project)
    project.record_id()
    print(f"已初始化 {project.dir}")
    print(f"项目外状态目录：{project.state_dir}")
    if not getattr(args, "no_statusline", False):
        print(statusline_install(project))
    print_snapshot_scope(project)


def _human_kb(kb):
    for unit in ("KB", "MB", "GB"):
        if kb < 1024 or unit == "GB":
            return f"{kb:.0f} {unit}" if unit == "KB" else f"{kb:.1f} {unit}"
        kb /= 1024.0


def _du(path):
    try:
        p = subprocess.run(["du", "-sk", path], capture_output=True, text=True, timeout=30)
        if p.returncode == 0 and p.stdout.split():
            return _human_kb(int(p.stdout.split()[0]))
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    return "?"


def print_snapshot_scope(project, max_depth=4):
    """Show what snapshots cover: top-level entries, nested bulky dirs and nested projects."""
    store = SnapStore(project, "shadow")
    tracked = store.user_tracked()
    ignored_cache = {}
    sampled_dirs = set()

    def label(rel):
        bare = rel.rstrip("/")
        tracked_here = {p for p in tracked if p == bare or p.startswith(bare + "/")}
        is_excluded = excluded(rel)
        if tracked_here and is_excluded:
            result = "快照（用户仓库已跟踪）"
        else:
            result = "排除" if is_excluded else "快照"
        if bare in sampled_dirs:
            result += "（按抽样判定）"
        return result

    def excluded(rel):
        bare = rel.rstrip("/")
        full = os.path.join(project.root, bare)
        paths = [bare]
        if os.path.isdir(full) and not os.path.islink(full):
            files = []
            for dirpath, dirnames, filenames in os.walk(full):
                rel_dir = os.path.relpath(dirpath, project.root)
                dirnames[:] = [d for d in dirnames
                               if not os.path.islink(os.path.join(dirpath, d))
                               and not _shadow_prune_dir(d, rel_dir)]
                for filename in filenames:
                    files.append(os.path.relpath(os.path.join(dirpath, filename), project.root)
                                 .replace(os.sep, "/"))
                    if len(files) > 5000:
                        break
                if len(files) > 5000:
                    break
            if len(files) > 5000:
                sampled_dirs.add(bare)
                files = files[:5000]
            paths.extend(files)
        pending = [path for path in paths if path not in ignored_cache]
        if pending:
            ignored = shadow_excluded_many(store, pending, tracked=set())
            ignored_cache.update({path: path in ignored for path in pending})
        if ignored_cache.get(bare, False):
            return True
        if not os.path.isdir(full) or os.path.islink(full):
            return False
        return bool(paths[1:]) and all(ignored_cache.get(path, False) for path in paths[1:])

    print("快照覆盖范围（项目顶层条目 · 大小 · 是否排除）：")
    for name in sorted(os.listdir(project.root)):
        if name == ".git":
            continue
        full = os.path.join(project.root, name)
        is_dir = os.path.isdir(full) and not os.path.islink(full)
        print(f"  {label(name)}  {_du(full):>9}  {name + ('/' if is_dir else '')}")
    bulky, nested = [], []
    skip = {".git", ".baton", ".council", "node_modules", "__pycache__", "venv"}
    for dirpath, dirnames, _ in os.walk(project.root):
        rel_dir = os.path.relpath(dirpath, project.root)
        depth = 0 if rel_dir == "." else rel_dir.count(os.sep) + 1
        keep = []
        for d in sorted(dirnames):
            rel = d if rel_dir == "." else os.path.join(rel_dir, d)
            if d in skip or d.startswith(".venv") or os.path.islink(os.path.join(dirpath, d)):
                continue
            if is_project_dir(os.path.join(dirpath, d)):
                nested.append(rel)
                continue
            if depth >= 1 and d in BULKY_DIR_NAMES:
                bulky.append(rel)
            # Keep walking excluded parents so the confirmation list still names a
            # bulky directory such as exp/run1/checkpoints/ inside them.
            if depth + 1 < max_depth:
                keep.append(d)
        dirnames[:] = keep
    if bulky:
        print("嵌套的 data/models/outputs/logs/checkpoints 目录（大小 · 是否排除）：")
        for rel in bulky:
            print(f"  {label(rel + '/')}  {_du(os.path.join(project.root, rel)):>9}  {rel}/")
    if nested:
        print("嵌套项目（有自己的 Baton，外层任务的回滚不会改动它们）：" + "，".join(nested))
    print(f"排除规则在 {os.path.join(project.snap_git_dir, 'info', 'exclude')}，项目内的 .gitignore 也生效，"
          "项目目录以上的 .gitignore 不生效；用户仓库已跟踪的文件即使被忽略也会进快照。"
          "请和用户确认范围；要调整就编辑这个文件，下次快照起生效。")


def cmd_statusline(project, args):
    if args.action == "install":
        print(statusline_install(project, force=args.force))
    elif args.action == "uninstall":
        print(statusline_uninstall(project))
    else:
        print(statusline_report(project))


def cmd_new(project, args):
    project.require_init()
    check_task(args.task)
    os.makedirs(project.task_dir(args.task), exist_ok=True)
    brief = os.path.join(project.task_dir(args.task), "brief.md")
    if os.path.exists(brief):
        die(f"简报已存在：{brief}")
    if args.brief:
        shutil.copyfile(args.brief, brief)
    else:
        text = read_text(os.path.join(TEMPLATES, "brief.md")).replace("{{TASK}}", args.task)
        with open(brief, "w") as f:
            f.write(text)
    print(brief)


def running_task(project, exclude=None):
    """Another task of this project with a running leg or a live codex, if any."""
    for task in project.tasks():
        if task == exclude:
            continue
        st = project.read_state(task, {}) or {}
        if not st.get("legs"):
            continue
        st = refresh_runner_liveness(project, task, st)
        leg = current_leg(st)
        if leg and (leg.get("status") == "running" or codex_alive(leg)):
            return task, leg["n"]
    return None


def launch_leg(project, task, kind, prompt):
    os.makedirs(project.dir, exist_ok=True)
    os.makedirs(project.state_dir, exist_ok=True)
    with open(project.launch_lock_path(), "w") as launch_lock:
        if fcntl:
            fcntl.flock(launch_lock, fcntl.LOCK_EX)  # one launch at a time per project
        st = project.read_state(task, {}) or {}
        st = refresh_runner_liveness(project, task, st) if st.get("legs") else st
        leg = current_leg(st) if st else None
        if leg and leg.get("status") == "running":
            die(f"任务 {task} 第 {leg['n']} 轮仍在运行；先 baton wait 或 baton stop")
        if codex_alive(leg):
            die(f"上一轮的 codex 进程（pid {leg['codex_pid']}）还在运行；先 baton stop {task}")
        other = running_task(project, exclude=task)
        if other:
            die(f"同一项目里任务 {other[0]} 的第 {other[1]} 轮还在运行，两个执行者不能同时改一个工作区")
        if st:
            surface_unreported(project, task, st, "续跑")
        n = len(st.get("legs", [])) + 1
        os.makedirs(project.runs_dir(task), exist_ok=True)
        prompt_path = project.leg_path(task, n, "prompt.md")
        if os.path.exists(prompt_path):
            die(f"{prompt_path} 已经存在，和任务状态里的轮次对不上（项目目录可能换过位置，或状态目录变了）；"
                "为免覆盖旧的运行记录，先查明原因；移动项目用 baton init --relink")
        with open(prompt_path, "w") as f:
            f.write(prompt)

        print(codex_quota.describe(safe_quota(block=True, notify=True)))

        start_ref, err = try_checkpoint(project, task, f"leg-{n:03d}-start", st=st)
        if err:
            print(f"注意：本轮起点回滚点没建成：{err}")
        last_path = project.leg_path(task, n, "last.json")
        cmd = codex_command(project, kind, st.get("thread_id"), last_path)
        t0 = now()
        log_size = file_size(project.p("log.md"))

        def add_leg(s):
            s.setdefault("task", task)
            s.setdefault("created_at", t0)
            s.setdefault("legs", [])
            s["legs"].append({
                "n": n, "kind": kind, "status": "running", "started_at": t0,
                "start_ref": start_ref, "cmd": cmd, "prompt": prompt_path,
                "events": project.leg_path(task, n, "events.jsonl"), "last": last_path,
                "stderr": project.leg_path(task, n, "stderr.log"), "log_size_at_start": log_size,
            })
            s["status"] = "running"
            # resuming the executor is itself a supervision point: Opus has just looked at everything
            s["last_supervision_at"] = t0
            s["supervision"] = {"leg": n, "events_offset": 0, "log_offset": log_size, "ref": start_ref, "since": t0}
            s.pop("pending_supervision", None)
        project.update_state(task, add_leg)

        log = open(project.leg_path(task, n, "runner.log"), "w")
        runner = subprocess.Popen([sys.executable, os.path.abspath(__file__), "--root", project.root,
                                   "_run", task, str(n)],
                                  stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                  start_new_session=True, cwd=project.root)

        def set_pid(s):
            current_leg(s)["runner_pid"] = runner.pid
        project.update_state(task, set_pid)
    cfg = project.config
    ex = cfg["executor"]
    print(f"BATON::STARTED task={task} leg={n} kind={kind} model={ex['model']} "
          f"effort={ex['reasoning_effort']} service_tier={ex['service_tier']} "
          f"judge={cfg['conductor']['model']} runner_pid={runner.pid} monitor={cfg['monitor']['model']}")
    print(f"回滚点：{start_ref or '无'}")
    interval = project.config["supervision"]["interval_minutes"]
    if interval:
        next_step = f"下一步：用后台方式运行 `baton wait {task}`，leg 结束或 {interval} 分钟监管时间到会唤醒你。"
    else:
        next_step = "下一步：用后台方式运行 `baton wait {task}`；本模式只在 leg 结束或额度不足时返回。".format(task=task)
    print(next_step)


def cmd_start(project, args):
    project.require_init()
    check_task(args.task)
    brief_path = os.path.join(project.task_dir(args.task), "brief.md")
    brief = read_text(brief_path)
    if not brief.strip():
        die(f"缺少任务简报 {brief_path}；先 baton new {args.task} 并填写")
    if "（待填）" in brief or "{{TASK}}" in brief:
        die(f"简报里还有未填写的模板占位（「（待填）」或 {{{{TASK}}}}）：{brief_path}")
    st = project.read_state(args.task, {}) or {}
    if st.get("thread_id"):
        die(f"任务 {args.task} 已有 Codex 会话 {st['thread_id']}；继续请用 baton resume")
    r = subprocess.run(["git", "check-ref-format", f"refs/baton/{args.task}/base"])
    if r.returncode != 0:
        die(f"任务名 {args.task} 不能用作 git ref，请换一个")
    write_baton_gitignore(project)
    if is_git(project.root) and baton_statusline_installed(project):
        warn = ensure_settings_ignored(project)
        if warn:
            print(warn)
    if not st.get("base_ref"):
        if not st.get("legs"):
            # a task started by v2 keeps every checkpoint in the shadow repo
            st = project.update_state(args.task, lambda s: s.update(snapshot_store="shadow", task=args.task))
        ref, err = try_checkpoint(project, args.task, "base", st=st)
        if err:
            die(f"任务起点回滚点没建成：{err}")
        project.update_state(args.task, lambda s: s.update(base_ref=ref, task=args.task))
    prompt = (protocol_text(project, args.task)
              + f"\n\n---\n\n# 任务简报（{args.task}）\n\n" + brief)
    launch_leg(project, args.task, "start", prompt)


def message_from(args):
    """The conductor's message (file or --message), validated before anything else happens."""
    if args.message_file:
        if not os.path.isfile(args.message_file):
            die(f"找不到消息文件 {args.message_file}")
        message = read_text(args.message_file)
        if not message.strip():
            die(f"消息文件 {args.message_file} 是空的")
        return message
    if not (args.message or "").strip():
        die("需要指挥消息：给出消息文件，或用 --message TEXT")
    return args.message


def cmd_resume(project, args, message=None):
    project.require_init()
    message = message if message is not None else message_from(args)
    st = refresh_runner_liveness(project, args.task, project.load_state(args.task))
    if not st.get("thread_id"):
        leg = current_leg(st)
        tid = thread_id_from(leg["events"]) if leg else None
        if not tid:
            die("找不到 Codex 会话 id，无法续跑；可能首轮没有成功启动，改用 baton start")
        project.update_state(args.task, lambda s: s.update(thread_id=tid))
    cfg = project.config
    prompt = (f"[Baton 指挥 · {args.kind}]\n\n{message.strip()}\n\n"
              "（继续遵守 Baton 执行协议：小修改记 log，大修改写汇报后停下，"
              "需要决策时写决策请求后停下；本轮结束按 JSON schema 输出。"
              f"当前 major 阈值：{cfg['change_size']['major_files']} 个文件或 {cfg['change_size']['major_lines']} 行；"
              f"验证命令：{cfg['supervision'].get('test_command') or '按简报'}。）\n")
    launch_leg(project, args.task, "resume", prompt)


def _interrupt(proc):
    for sig, grace in ((signal.SIGINT, 20), (signal.SIGTERM, 10), (signal.SIGKILL, 5)):
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            return
        try:
            proc.wait(timeout=grace)
            return
        except subprocess.TimeoutExpired:
            continue


class _Terminated(Exception):
    pass


def _raise_terminated(signum, frame):
    # one shutdown is enough: ignore further TERM/HUP while we stop codex and record the leg
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    raise _Terminated(f"signal {signum}")


def cmd_run(project, args):
    """Internal: the detached leg runner. Always records a result, even when it is killed."""
    task, n = args.task, int(args.n)
    signal.signal(signal.SIGTERM, _raise_terminated)
    signal.signal(signal.SIGHUP, _raise_terminated)
    st = project.load_state(task)
    leg = st["legs"][n - 1]
    stop_flag = project.leg_path(task, n, "stop")
    proc, reason, error = None, None, None
    try:
        with open(leg["prompt"], "rb") as fin, open(leg["events"], "wb") as fout, \
                open(leg["stderr"], "wb") as ferr:
            proc = subprocess.Popen(leg["cmd"], cwd=project.root, stdin=fin, stdout=fout,
                                    stderr=ferr, start_new_session=True)

        def set_codex(s):
            current_leg(s)["codex_pid"] = proc.pid
        project.update_state(task, set_codex)

        limit = float(project.config["supervision"].get("max_unsupervised_minutes") or 0)
        while proc.poll() is None:
            time.sleep(2)
            if proc.poll() is not None:
                break  # finished on its own during the sleep: its result wins over a late stop
            if not st.get("thread_id"):
                tid = thread_id_from(leg["events"])
                if tid:
                    st = project.update_state(task, lambda s: s.update(thread_id=tid))
            if os.path.exists(stop_flag):
                reason = "stopped"
                _interrupt(proc)
                break
            last_sup = (project.read_state(task, {}) or {}).get("last_supervision_at", leg["started_at"])
            if limit and now() - last_sup > limit * 60:
                reason = "paused_unsupervised"
                _interrupt(proc)
                break
    except BaseException as e:  # noqa: BLE001 — killed or crashed: stop codex, still record the leg
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        error = f"runner 中断（{type(e).__name__}: {e}）"
        if proc is not None and proc.poll() is None:
            _interrupt(proc)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    rc = proc.wait() if proc is not None else -1

    result = read_json(leg["last"])
    if result is not None and not isinstance(result, dict):
        result = {"status": "unknown", "summary": json.dumps(result, ensure_ascii=False)[:4000],
                  "artifact": "", "change_size": "unknown"}
    if result is None and os.path.exists(leg["last"]):
        result = {"status": "unknown", "summary": read_text(leg["last"])[:4000],
                  "artifact": "", "change_size": "unknown"}
    finished_ok = rc == 0 and isinstance(result, dict) and result.get("status") in ("done", "decision", "report", "blocked")
    if finished_ok:
        status = result["status"]  # the executor's own result beats a stop that arrived too late
    elif error:
        status = "failed"
    elif reason:
        status = reason
    else:
        status = "failed"

    end_ref, cp_err = try_checkpoint(project, task, f"leg-{n:03d}-end")
    diff = None
    if end_ref and leg.get("start_ref"):
        try:
            diff = shortstat(project.store(task), leg["start_ref"], end_ref)
        except Exception:  # noqa: BLE001
            diff = None
    events, _ = read_events(leg["events"])
    dg = digest(events)
    log_size = file_size(project.p("log.md"))
    stderr_tail = read_text(leg["stderr"])[-1500:]
    notes = [x for x in (error if not finished_ok else None,
                         "收到停止指令时执行者已自己结束，按它的结果记录" if finished_ok and (reason or error) else None,
                         f"本轮终点回滚点没建成：{cp_err}" if cp_err else None) if x]

    def finish(s):
        lg = s["legs"][n - 1]
        lg.update(status=status, exit_code=rc, ended_at=now(), result=result, end_ref=end_ref,
                  diff=diff, usage=dg["usage"], log_grew=log_size > lg.get("log_size_at_start", 0),
                  stderr_tail=stderr_tail if status == "failed" else "", codex_exited=True)
        if notes:
            lg["error"] = "；".join(notes)
        if not s.get("thread_id"):
            s["thread_id"] = thread_id_from(lg["events"])
        s["status"] = "done" if status == "done" else "idle"
    project.update_state(task, finish)
    try:
        os.remove(stop_flag)
    except OSError:
        pass
    safe_quota(block=True, notify=True)


def report_baseline(st, leg):
    """End ref of the previous leg that handed in a report (else the task base)."""
    for lg in reversed(st.get("legs", [])[: leg["n"] - 1]):
        if lg.get("status") == "report" and lg.get("end_ref"):
            return lg["end_ref"]
    return st.get("base_ref")


LEAN_HINT = "lean 模式：请把主会话的思考深度设为 medium（conductor.reasoning_effort）。"


def print_lean_hint(project):
    if project.config.get("mode") == "lean":
        print(LEAN_HINT)


def leg_event_text(project, task, leg):
    ev = LEG_END_EVENTS.get(leg["status"], str(leg["status"]).upper())
    lines = [f"BATON::EVENT {ev} task={task} leg={leg['n']}"]
    if project.config.get("mode") == "lean":
        lines.append(LEAN_HINT)
    res = leg.get("result") if isinstance(leg.get("result"), dict) else {}
    if res.get("summary"):
        lines.append(f"执行者摘要：{clip(str(res['summary']), 1500)}")
    if res.get("artifact"):
        lines.append(f"产物：{res['artifact']}")
    if leg.get("diff"):
        f, a, d = leg["diff"]
        lines.append(f"本轮改动（不含 .baton/）：{f} 个文件，+{a} −{d}")
        cs = project.config["change_size"]
        if res.get("change_size") == "minor" and (f >= cs["major_files"] or a + d >= cs["major_lines"]):
            lines.append("注意：执行者标为 minor，但改动量超过 major 阈值，可能是未汇报的大修改。")
        if (f or a or d) and not leg.get("log_grew"):
            lines.append("注意：有代码改动但 log.md 本轮没有新增记录。")
    if ev == "REPORT" and leg.get("end_ref"):
        st = project.read_state(task, {}) or {}
        base = report_baseline(st, leg)
        store = project.store(task, st)
        if base and store:
            try:
                f, a, d = shortstat(store, base, leg["end_ref"])
                lines.append(f"自上次汇报以来（{base}）：{f} 个文件，+{a} −{d}；审阅用 baton diff {task} --since report")
            except Exception:  # noqa: BLE001
                pass
    if leg.get("usage"):
        lines.append(f"token：{fmt_usage(leg['usage'])}")
    lines.append(f"用时：{fmt_dur((leg.get('ended_at') or now()) - leg['started_at'])}")
    if leg.get("error"):
        lines.append(f"错误：{leg['error']}")
    if leg.get("stderr_tail"):
        lines.append("stderr 末尾：\n" + leg["stderr_tail"])
    lines.append(f"回滚点：{leg.get('start_ref') or '无'} → {leg.get('end_ref') or '无'}")
    q = safe_quota(block=False)
    if codex_quota.is_low(q):
        lines.append(codex_quota.describe(q).splitlines()[0])
    lines.append("下一步：" + NEXT_STEP.get(ev, "查看 baton status"))
    return "\n".join(lines)


def cmd_wait(project, args):
    project.require_init()
    minutes = args.tick if args.tick is not None else project.config["supervision"]["interval_minutes"]
    last_quota_check = 0.0
    while True:
        st = refresh_runner_liveness(project, args.task, project.load_state(args.task))
        leg = current_leg(st)
        if not leg:
            print(f"BATON::EVENT IDLE task={args.task}：还没有启动任何一轮")
            print_lean_hint(project)
            return
        if leg["status"] != "running":
            if leg.get("reported"):
                print(f"BATON::EVENT IDLE task={args.task} leg={leg['n']}：没有运行中的一轮，"
                      f"上一轮状态 {leg['status']}（已处理过）。需要继续就 baton resume。")
                print_lean_hint(project)
                return
            print(leg_event_text(project, args.task, leg), flush=True)
            mark_reported(project, args.task)
            return
        elapsed_sup = now() - st.get("last_supervision_at", leg["started_at"])
        if supervision_tick_due(elapsed_sup, minutes):
            print(f"BATON::EVENT TICK task={args.task} leg={leg['n']}：距上次监管 {fmt_dur(elapsed_sup)}，"
                  f"本轮已运行 {fmt_dur(now() - leg['started_at'])}")
            print_lean_hint(project)
            print("下一步：" + NEXT_STEP["TICK"])
            return
        if now() - last_quota_check > 300:
            last_quota_check = now()
            data = safe_quota(block=True, notify=True)
            keys = codex_quota.low_window_keys(data)
            alerted = st.get("quota_alerted") or []
            fresh = [k for k in keys if not codex_quota.already_alerted(k, alerted)]
            if fresh:
                project.update_state(args.task, lambda s: s.update(quota_alerted=sorted(set((s.get("quota_alerted") or []) + keys))))
                print(f"BATON::EVENT QUOTA_LOW task={args.task} leg={leg['n']}")
                print_lean_hint(project)
                print(codex_quota.describe(data))
                print("下一步：" + NEXT_STEP["QUOTA_LOW"])
                return
        time.sleep(10)


def supervision_tick_due(elapsed_seconds, interval_minutes):
    """Whether a wait call should return a periodic TICK event."""
    return float(interval_minutes) > 0 and elapsed_seconds >= float(interval_minutes) * 60


def cmd_status(project, args):
    project.require_init()
    task = args.task
    v1_in_flight = project.legacy_state_in_flight(task)
    st = project.load_state(task)
    if not v1_in_flight:
        st = refresh_runner_liveness(project, task, st)
    cfg = project.config
    leg = current_leg(st)
    print(f"# Baton 状态 · {task} · {stamp()}")
    print(f"任务状态：{st.get('status')}；Codex 会话：{st.get('thread_id') or '未知'}；共 {len(st.get('legs', []))} 轮")
    if not leg:
        return
    running = leg["status"] == "running"
    print(f"当前第 {leg['n']} 轮（{leg['kind']}）：{leg['status']}，已运行 {fmt_dur((leg.get('ended_at') or now()) - leg['started_at'])}"
          + (f"，codex pid {leg.get('codex_pid')} {'存活' if codex_alive(leg) else '不在'}" if running else ""))
    if leg["status"] != "running" and not leg.get("reported"):
        print(f"注意：这一轮已结束（{leg['status']}），事件还没被 baton wait 报告过。")
    last_sup = st.get("last_supervision_at", leg["started_at"])
    print(f"上次监管：{stamp(last_sup)}（{fmt_dur(now() - last_sup)} 前）；"
          f"自动暂停时限 {cfg['supervision'].get('max_unsupervised_minutes')} 分钟")
    print(codex_quota.describe(safe_quota(block=True)))

    sup = st.get("supervision") or {}
    same_leg = sup.get("leg") == leg["n"]
    offset = sup.get("events_offset", 0) if same_leg else 0
    events, events_end = read_events(leg["events"], offset)
    dg = digest(events)
    still_running = digest(read_events(leg["events"], 0)[0])["running"] if running else []
    print(f"\n## 上次监管以来的执行者活动（{len(events)} 个事件）")
    if still_running:
        print("正在执行：" + "；".join(clip(c, 200) for c in still_running))
    if dg["commands"]:
        failed = sum(1 for _, code in dg["commands"] if code not in (0, None))
        print(f"命令 {len(dg['commands'])} 条（非零退出 {failed} 条），最近 {min(15, len(dg['commands']))} 条：")
        for c, code in dg["commands"][-15:]:
            print(f"  [{code}] {clip(c, 220)}")
    if dg["files"]:
        print(f"文件变更 {len(dg['files'])} 个：")
        for path, kind in list(dg["files"].items())[-40:]:
            print(f"  {kind:<7} {os.path.relpath(path, project.root) if os.path.isabs(path) else path}")
    for m in dg["messages"][-3:]:
        print("执行者消息：" + clip(m, 600))
    for e in dg["errors"]:
        print("错误：" + clip(e, 400))

    now_commit = None
    store = project.store(task, st)
    if store:
        print("\n## 代码改动（不含 .baton/ .council/）")
        try:
            now_commit = store.snapshot("baton status probe")
        except Exception as e:  # noqa: BLE001
            print(f"（快照失败，无法统计改动：{e}）")
    if now_commit:
        for label, ref in (("任务起点", st.get("base_ref")), ("本轮起点", leg.get("start_ref")),
                           ("上次监管", sup.get("ref"))):
            if ref:
                f, a, d = shortstat(store, ref, now_commit)
                print(f"相对{label} {ref}：{f} 个文件，+{a} −{d}")
        if leg.get("start_ref"):
            stat_out = store.git("diff", "--stat=120", leg["start_ref"], now_commit, "--", ".", *code_excludes(store), check=False)
            print(stat_out.rstrip()[-4000:] or "（本轮暂无改动）")
            pairs = [(s, p) for _, _, s, p in diff_tree_entries(store, leg["start_ref"], now_commit)]
            tracked = store.user_tracked() if store.kind == "shadow" else set()
            deleted_paths = [p for s, p in pairs if s == "D"]
            excluded_set = shadow_excluded_many(store, deleted_paths, tracked)
            deleted = [p for p in deleted_paths if p not in excluded_set]
            excluded_deleted = [p for p in deleted_paths if p in excluded_set]
            if deleted:
                print(f"注意：本轮删除了 {len(deleted)} 个文件（含改名前的旧路径）：" + "，".join(deleted[:20]))
            if excluded_deleted:
                print("注意：以下路径当前被影子仓库排除，相对旧回滚点显示为删除："
                      + "，".join(excluded_deleted[:20]))
            risky = [p for _, p in pairs if re.search(r"(^|/)(\.github|\.gitlab-ci|Dockerfile|pyproject\.toml|package(-lock)?\.json|requirements.*\.txt|setup\.(py|cfg)|go\.mod|Cargo\.toml)", p)]
            if risky:
                print("注意：改到了构建/依赖/CI 文件：" + "，".join(risky[:20]))
            tests = [p for s, p in pairs if s in ("D", "M", "T") and re.search(r"(test|spec)", p, re.I)]
            if tests:
                print("注意：改动或删除了测试文件：" + "，".join(tests[:20]))

    log_path = project.p("log.md")
    log_off = sup.get("log_offset") if same_leg and sup.get("log_offset") is not None else leg.get("log_size_at_start", 0)
    new_log = read_bytes_since(log_path, log_off)
    log_end = file_size(log_path)
    print("\n## log.md 新增")
    if new_log is None:
        print("注意：log.md 被删除或比上次监管时变短了，执行者可能改写了已有记录（协议只允许追加）。")
    elif len(new_log) > 4000:
        heads = [l for l in new_log.splitlines() if l.startswith("## ")]
        print(f"新增 {len(heads)} 条记录（{len(new_log)} 字），标题如下，最后部分全文附后：")
        for h in heads:
            print("  " + h)
        print("……\n" + new_log[-3000:])
    else:
        print(new_log.strip() or "（无新增）")
    since = sup.get("since", last_sup)
    for sub in ("reports", "decisions"):
        fresh = [f for f in sorted(os.listdir(project.p(sub))) if os.path.getmtime(project.p(sub, f)) > since] \
            if os.path.isdir(project.p(sub)) else []
        if fresh:
            print(f"\n{sub}/ 新文件：" + "，".join(fresh))
    tc = cfg["supervision"].get("test_command")
    print(f"\n验证命令：{tc}" if tc else "\n验证命令：未配置（.baton/config.json → supervision.test_command）")

    if v1_in_flight:
        print("提示：Baton v1 这一轮仍在运行，未写入 pending_supervision；等它结束后再用 v2 命令监管。")
        return

    pend = {"leg": leg["n"], "events_offset": events_end, "log_offset": log_end,
            "commit": now_commit, "at": now()}
    project.update_state(task, lambda s: s.update(pending_supervision=pend))


def cmd_supervised(project, args):
    project.require_init()
    task = args.task
    if project.legacy_state_in_flight(task):
        die("任务还有 Baton v1 的一轮在运行，暂不写监管记录；等这一轮结束后再操作")
    st = project.load_state(task)
    leg = current_leg(st)
    pend = st.get("pending_supervision")
    # Use the baseline of the snapshot Opus actually read, so nothing between status and now is skipped.
    if not (pend and leg and pend.get("leg") == leg["n"] and now() - pend.get("at", 0) < 3600):
        pend = None
    label = f"tick-{stamp(fmt='%Y%m%d-%H%M%S')}"
    ref, err = None, None
    store = project.store(task, st)
    if pend and pend.get("commit") and store:
        try:
            ref = store_ref(store, task, label, pend["commit"])
        except Exception as e:  # noqa: BLE001
            err = str(e)
    else:
        ref, err = try_checkpoint(project, task, label, st=st)
    events_off = pend["events_offset"] if pend else (read_events(leg["events"])[1] if leg else 0)
    log_off = pend["log_offset"] if pend else file_size(project.p("log.md"))
    since = pend["at"] if pend else now()
    with open(project.p("supervision.md"), "a") as f:
        f.write(f"\n## {stamp()} · {task} · 第 {leg['n'] if leg else '-'} 轮 · {args.verdict}\n\n"
                f"{args.note.strip()}\n\n- 回滚点：`{ref or '无'}`\n")

    def mark(s):
        s["last_supervision_at"] = now()
        s["supervision"] = {"leg": leg["n"] if leg else 0, "events_offset": events_off, "log_offset": log_off,
                            "ref": ref or (s.get("supervision") or {}).get("ref"), "since": since}
        s.pop("pending_supervision", None)
    project.update_state(task, mark)
    if err:
        print(f"注意：监管回滚点没建成：{err}")
    print(f"已记录监管结论 {args.verdict}；回滚点 {ref or '无'}。若本轮仍在运行，重新后台 baton wait {task}。")


def cmd_stop(project, args):
    project.require_init()
    before = current_leg(project.load_state(args.task))
    st = refresh_runner_liveness(project, args.task, project.load_state(args.task))
    leg = current_leg(st)
    if not leg or leg["status"] != "running":
        if before and before.get("status") == "running" and leg and leg.get("status") == "failed":
            print(f"第 {leg['n']} 轮的 runner 已不在：{leg.get('error')}")
        elif leg and kill_codex(leg):
            print(f"第 {leg['n']} 轮的 runner 已不在，残留的 codex 进程已终止")
        elif leg and not leg.get("reported") and leg["status"] != "stopped":
            print(leg_event_text(project, args.task, leg))
            mark_reported(project, args.task)
            print(f"第 {leg['n']} 轮在停止前已经自己结束，上面是它的事件。")
        else:
            print("没有运行中的一轮")
        return
    with open(project.leg_path(args.task, leg["n"], "stop"), "w") as f:
        f.write(args.reason or "stopped by conductor")
    deadline = now() + 60
    while now() < deadline:
        time.sleep(2)
        st = refresh_runner_liveness(project, args.task, project.load_state(args.task))
        if current_leg(st)["status"] != "running":
            print(f"第 {leg['n']} 轮已结束：{current_leg(st)['status']}；回滚点 {current_leg(st).get('end_ref')}")
            return
    print("停止信号已发出但 60 秒内未结束；稍后用 baton status 确认")


def cmd_steer(project, args):
    project.require_init()
    message = message_from(args)  # validate before touching the running leg
    st = refresh_runner_liveness(project, args.task, project.load_state(args.task))
    leg = current_leg(st)
    if leg and leg["status"] == "running":
        cmd_stop(project, argparse.Namespace(task=args.task, reason="steer"))
        st = project.load_state(args.task)
        leg = current_leg(st)
    if leg and leg["status"] == "running":
        die("当前轮还没停下，稍后用 baton status 确认后再纠偏")
    if leg and not leg.get("reported"):
        if leg["status"] != "stopped":
            surface_unreported(project, args.task, st, "纠偏")
        mark_reported(project, args.task)
    cmd_resume(project, argparse.Namespace(task=args.task, kind="纠偏"), message=message)


def cmd_checkpoint(project, args):
    check_task(args.task)
    if not project.task_exists(args.task):
        die(f"任务 {args.task} 不存在（没有简报或状态文件）")
    if project.legacy_state_in_flight(args.task):
        die("任务还有 Baton v1 的一轮在运行，暂不建立回滚点；等这一轮结束后再操作")
    label = args.label or f"manual-{stamp(fmt='%Y%m%d-%H%M%S')}"
    if not LABEL_RE.fullmatch(label):
        die("标签只能用字母、数字、下划线和连字符")
    ref, err = try_checkpoint(project, args.task, label)
    if err:
        die(f"回滚点没建成：{err}")
    print(ref)


def cmd_refs(project, args):
    store = project.store(args.task)
    out = store.git("for-each-ref", "--sort=creatordate",
                    "--format=%(creatordate:format:%m-%d %H:%M)  %(refname)", f"refs/baton/{args.task}",
                    check=False) if store and (store.kind == "repo" or os.path.isdir(store.git_dir)) else ""
    print(out.rstrip() or "（无回滚点）")


def cmd_diff(project, args):
    st = project.load_state(args.task)
    leg = current_leg(st)
    base = {"base": st.get("base_ref"), "leg": leg and leg.get("start_ref"),
            "tick": (st.get("supervision") or {}).get("ref"),
            "report": leg and report_baseline(st, leg) if leg else None}.get(args.since, args.since)
    if not base:
        die("没有对应的回滚点")
    store = project.store(args.task, st)
    if store is None:
        die("这个任务建立于 Baton v1，项目又不是 git 仓库，没有回滚点")
    if not store.has_commit(base):
        die(f"找不到回滚点 {base}；可用的回滚点见 baton refs {args.task}")
    try:
        target = store.snapshot("baton diff probe")
    except Exception as e:  # noqa: BLE001
        die(f"快照失败：{e}")
    extra = ["--stat=120"] if args.stat else []
    output = store.git("diff", *extra, base, target, "--", ".", *code_excludes(store), check=False)
    sys.stdout.write(output)
    tracked = store.user_tracked() if store.kind == "shadow" else set()
    deleted_paths = [path for _, _, status_, path in diff_tree_entries(store, base, target) if status_ == "D"]
    excluded_set = shadow_excluded_many(store, deleted_paths, tracked)
    excluded = [path for path in deleted_paths if path in excluded_set]
    if excluded:
        if output and not output.endswith("\n"):
            print()
        print("注意：以下路径当前被影子仓库排除，相对旧回滚点显示为删除，diff 不代表会被 rollback 写回："
              + "，".join(excluded[:20]))


# ---------------------------------------------------------------- rollback

def _leading_symlink(top, rel):
    """First leading directory component of rel that is a symlink in the worktree, or None."""
    cur = top
    for part in rel.split("/")[:-1]:
        cur = os.path.join(cur, part)
        if os.path.islink(cur):
            return cur
        if not os.path.isdir(cur):
            return None
    return None


def _under(path, prefixes):
    return any(path == p or path.startswith(p + "/") for p in prefixes)


def _backup_destination(backup_dir, rel, manifest=None):
    """Choose a backup path, preserving case-only collisions with a manifest entry."""
    dest = os.path.join(backup_dir, rel)
    if os.path.lexists(dest):
        parent, name = os.path.dirname(dest), os.path.basename(dest)
        try:
            names = os.listdir(parent)
        except OSError:
            names = []
        exact = name in names
        case_conflict = (not exact and any(n.casefold() == name.casefold() for n in names))
        if not case_conflict:
            raise RuntimeError(f"备份目标已存在：{dest}")
        stem = dest
        n = 2
        while os.path.lexists(dest):
            dest = f"{stem}.{n}"
            n += 1
        if manifest is not None:
            manifest.append({"original": rel.replace(os.sep, "/"),
                             "backup": os.path.relpath(dest, backup_dir).replace(os.sep, "/")})
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    return dest


def _backup(top, full, backup_dir, manifest=None):
    rel = os.path.relpath(full, top)
    dest = _backup_destination(backup_dir, rel, manifest)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    if os.path.islink(full) or not os.path.isdir(full):
        shutil.copy2(full, dest, follow_symlinks=False)
    else:
        shutil.copytree(full, dest, symlinks=True)
    return dest


def _prune_empty_dirs(path, stop):
    stop = os.path.realpath(stop)
    path = os.path.realpath(path)
    while path.startswith(stop + os.sep) and os.path.isdir(path) and not os.path.islink(path):
        try:
            os.rmdir(path)
        except OSError:
            return
        path = os.path.dirname(path)


def cmd_rollback(project, args):
    """Restore worktree files under the project root to a checkpoint.

    Only worktree files change: HEAD, branches and the real index stay as they are. The current
    state is checkpointed first, and every file that will be overwritten or deleted is copied
    byte for byte to <state dir>/rollback-backup/<name>/ before anything changes. Nothing is
    written or deleted through a symlinked directory or inside a submodule.
    """
    root = project.root
    st = project.load_state(args.task)
    store = project.store(args.task, st)
    if store is None:
        die("这个任务建立于 Baton v1，项目又不是 git 仓库，无法回滚")
    target = store.git("rev-parse", "--verify", "-q", f"{args.ref}^{{commit}}", check=False).strip() \
        if store.kind == "repo" or os.path.isdir(store.git_dir) else ""
    if not target:
        die(f"找不到回滚点 {args.ref}")
    leg = current_leg(st)
    if leg and leg["status"] == "running":
        die("执行者仍在运行，先 baton stop")
    if not args.yes:
        print(f"将把 {root} 下的工作区文件（不含 .baton/ .council/）恢复到 {args.ref}。HEAD、分支和暂存区不变。")
        print(f"当前状态会先存成回滚点，要改写或删除的文件会先原样备份到 {os.path.join(project.state_dir, 'rollback-backup')}。子模块内部不在回滚范围内。")
        print("确认后加 --yes 重新执行。")
        sys.exit(3)

    write_baton_gitignore(project)
    top = store.top
    pre_ref, err = try_checkpoint(project, args.task, f"pre-rollback-{stamp(fmt='%Y%m%d-%H%M%S')}", st=st)
    if err:
        die(f"回滚前的状态没能存下来，已放弃回滚：{err}")
    pre = store.git("rev-parse", pre_ref).strip()
    backup_dir = os.path.join(project.state_dir, "rollback-backup", os.path.basename(pre_ref))
    entries = diff_tree_entries(store, pre, target)
    gitlinks = set()
    for tree in (pre, target):
        for line in store.git("ls-tree", "-r", "-z", tree, cwd=top).split("\0"):
            if line.startswith("160000 "):
                gitlinks.add(line.split("\t", 1)[1])
    nested_cache = {}

    def nested_owner(path):
        """The nested project (its own baton.json or .baton/) that owns path, if any."""
        d = os.path.dirname(os.path.join(top, path))
        chain = []
        while d.startswith(root + os.sep):
            if d in nested_cache:
                found = nested_cache[d]
                break
            chain.append(d)
            if is_project_dir(d):
                found = d
                break
            d = os.path.dirname(d)
        else:
            found = None
        for c in chain:
            nested_cache[c] = found
        return found

    tracked = store.user_tracked() if store.kind == "shadow" else set()
    exclusion_candidates = []
    for src, dst, status_, path in entries:
        if status_ not in ("A", "M", "T") or "160000" in (src, dst) or _under(path, gitlinks):
            continue
        owner = nested_owner(path)
        if not owner and not _leading_symlink(top, path):
            exclusion_candidates.append(path)
    excluded_set = shadow_excluded_many(store, exclusion_candidates, tracked)
    deletes, writes, skipped = [], [], []
    for src, dst, status_, path in entries:
        owner = nested_owner(path)
        if "160000" in (src, dst) or _under(path, gitlinks):
            skipped.append(f"{path}（子模块）")
        elif owner:
            skipped.append(f"{path}（属于嵌套项目 {os.path.relpath(owner, root)}）")
        elif _leading_symlink(top, path):
            skipped.append(f"{path}（路径经过符号链接）")
        elif status_ == "D":
            deletes.append(path)
        elif status_ in ("A", "M", "T"):
            if path in excluded_set:
                skipped.append(f"{path}（已排除）")
            else:
                writes.append(path)

    # 0. back up every existing path that will be deleted or overwritten, before changing anything
    backed, seen, backup_manifest = [], set(), []
    try:
        for path in deletes + writes:
            full = os.path.join(top, path)
            if os.path.lexists(full) and not (os.path.isdir(full) and not os.path.islink(full)):
                st_ = os.lstat(full)
                if (st_.st_dev, st_.st_ino) in seen:
                    continue  # same file under another name (case-insensitive disk)
                seen.add((st_.st_dev, st_.st_ino))
                backed.append(_backup(top, full, backup_dir, backup_manifest))
    except Exception as e:  # noqa: BLE001
        die(f"备份要改动的文件时出错，已放弃回滚，工作区没有改动：{e}")

    deleted, written = [], []
    try:
        # 1. deletions first, so a case-only rename on a case-insensitive disk ends with the target file
        for path in deletes:
            full = os.path.join(top, path)
            if os.path.lexists(full) and (os.path.islink(full) or not os.path.isdir(full)):
                os.unlink(full)
                deleted.append(path)
                _prune_empty_dirs(os.path.dirname(full), root)
        # 2. clear what stands in the way of the target files (moved into the backup, never lost)
        for path in writes:
            parts = path.split("/")
            for i in range(1, len(parts)):
                full_lead = os.path.join(top, "/".join(parts[:i]))
                if os.path.islink(full_lead) or (os.path.lexists(full_lead) and not os.path.isdir(full_lead)):
                    backed.append(_backup(top, full_lead, backup_dir, backup_manifest))
                    os.unlink(full_lead)
                    break
            full = os.path.join(top, path)
            if os.path.isdir(full) and not os.path.islink(full):
                if os.path.lexists(os.path.join(full, ".git")):
                    raise RuntimeError(f"{path} 是一个嵌套的 git 仓库，不会移动它")
                dest = _backup_destination(backup_dir, path, backup_manifest)
                shutil.move(full, dest)
                backed.append(dest)
        # 3. write the target versions through a throwaway index (git applies modes and filters);
        #    .gitattributes first so the other files are converted with the target's attributes
        if writes:
            fd, tmp_index = tempfile.mkstemp(prefix="baton-index-")
            os.close(fd)
            os.remove(tmp_index)
            try:
                env = {"GIT_INDEX_FILE": tmp_index}
                store.git("read-tree", target, env=env, cwd=top)
                attrs = [p for p in writes if p.split("/")[-1] == ".gitattributes"]
                rest = [p for p in writes if p not in attrs]
                for batch in (attrs, rest):
                    if batch:
                        store.git("checkout-index", "-f", "-z", "--stdin", env=env, cwd=top,
                            input_text="".join(p + "\0" for p in batch))
            finally:
                if os.path.exists(tmp_index):
                    os.remove(tmp_index)
            written = writes
        if backup_manifest:
            write_json(os.path.join(backup_dir, "manifest.json"), {"case_conflicts": backup_manifest})
    except Exception as e:  # noqa: BLE001
        msg = f"回滚中途出错：{e}\n回滚前的状态在 {pre_ref}，可以用 baton rollback {args.task} {pre_ref} --yes 恢复。"
        if backed:
            msg += f"\n改动前的文件原样备份在 {backup_dir}（{len(backed)} 项），其中被 git 忽略的文件只在这里有副本。"
        die(msg)
    print(f"已把工作区恢复到 {args.ref}：写回 {len(written)} 个文件，删除 {len(deleted)} 个。回滚前状态：{pre_ref}。")
    if backed:
        print(f"改动前的文件原样备份在 {backup_dir}（{len(backed)} 项）。")
    if skipped:
        print("未处理：" + "，".join(skipped[:20]))
    nested = nested_project_dirs_for_code(root)
    if nested and not any("属于嵌套项目" in item for item in skipped):
        print(f"另有 {len(nested)} 个嵌套项目，回滚不涉及（属于嵌套项目 " + "、".join(nested) + "）")
    if gitlinks:
        print("注意：项目里有子模块或嵌套仓库，它们内部的改动不在回滚范围内。")
    staged = [x for x in git(root, "diff", "--cached", "--name-only", "-z", "--", ".", *EXCLUDE,
                             check=False).split("\0") if x] if is_git(root) else []
    if staged:
        print("注意：暂存区里还有这些改动（回滚不碰暂存区）：" + "，".join(staged[:20])
              + "。如需丢弃，问过用户后运行 git restore --staged -- <路径>")


def cmd_list(project, args):
    project.require_init()
    for task in project.tasks():
        st = project.read_state(task, {}) or {}
        leg = current_leg(st) if st else None
        print(f"{task:<24} {st.get('status', 'draft'):<8} 轮次 {len(st.get('legs', []))}"
              + (f"  最新：{leg['status']} @ {stamp(leg['started_at'])}" if leg else ""))


def cmd_config(project, args):
    cfg = project.config
    conductor = cfg["conductor"]
    monitor = cfg["monitor"]
    ex = cfg["executor"]
    print(f"BATON::CONFIG judge={conductor['model']} executor={ex['model']} effort={ex['reasoning_effort']}"
          f" conductor={conductor['model']} backend={ex['backend']} project_id={project.project_id} monitor={monitor['model']} mode={cfg['mode']}")
    if cfg.get("mode") == "lean":
        print(LEAN_HINT)
    if project.needs_backend_migration:
        print("迁移提示：旧项目继续使用 codex；v2 默认执行者将改为 Hermes，第 4 阶段提供 baton init --migrate。",
              file=sys.stderr if args.json else sys.stdout)
    if args.json:
        print(json.dumps(cfg, ensure_ascii=False, indent=2))
        return
    print(f"conductor.model：{conductor['model']}")
    print(f"conductor.council_for_major_decisions：{conductor['council_for_major_decisions']}")
    print(f"judge.council_for_major_decisions（v1 旧名，等同上一行）：{conductor['council_for_major_decisions']}")
    for key in ("model", "reasoning_effort", "final_reasoning_effort", "timeout_minutes"):
        print(f"monitor.{key}：{monitor.get(key)}")
    if project.judge_renamed_to_monitor:
        print("迁移提示：项目层的 judge 节已按 v2 早期草案改名为 monitor。")
    for key in ("backend", "model", "reasoning_effort", "service_tier", "disable_fast_mode", "network_access", "sandbox"):
        print(f"executor.{key}：{ex.get(key)}")
    print(f"项目目录：{project.root}")
    print(f"项目 id：{project.project_id}")
    print(f"项目外状态目录：{project.state_dir}")
    print(f"supervision.test_command：{cfg['supervision'].get('test_command', '')}")
    print("配置层：")
    for path, exists in project.config_paths():
        print(f"- {path}：{'存在' if exists else '缺失'}")


def cmd_quota(project, args):
    argv = (["--json"] if args.json else []) + (["--refresh"] if args.refresh else []) + ["--notify"]
    sys.exit(codex_quota.main(argv))


COUNCIL_BUILTIN = ["council-cc-glm", "council-codex-gpt", "council-cc-kimi"]  # council.sh's own fallback


def council_dir():
    return os.path.join(skills_dir(), "council")


def council_check():
    """(level, message) about council's config.json and the councilor functions it names."""
    cfg_path = os.path.join(council_dir(), "config.json")
    cfg = read_json(cfg_path)
    note = ""
    if not isinstance(cfg, dict):
        cfg, note = {}, f"{cfg_path} 不是合法 JSON，council.sh 会退回内置顾问；"
    names = [n for n in (cfg.get("councilors") or []) if isinstance(n, str)]
    if not names:
        names, note = list(COUNCIL_BUILTIN), note or "config.json 没有列出顾问，council.sh 会用内置顾问；"
    if cfg.get("chief_backend"):
        names.append(cfg["chief_backend"])
    lib = os.environ.get("COUNCILOR_LIB") or os.path.expanduser("~/.local/bin/council-def.sh")
    if not os.path.exists(lib):
        return "WARN", f"council 后端定义 {lib} 不存在"
    script = 'source "$1" >/dev/null 2>&1; shift; for f in "$@"; do type "$f" >/dev/null 2>&1 || echo "$f"; done'
    missing = run(["bash", "-c", script, "_", lib, *names], check=False).split()
    if missing or note:
        return "WARN", f"council 后端（{lib}）：{note}" + (f"缺少 {'、'.join(missing)}" if missing else "")
    return "OK", f"council 后端（{lib}）：配置里的顾问都已定义"


def cmd_doctor(project, args):
    ok = True

    def line(level, msg):
        nonlocal ok
        ok = ok and level != "FAIL"
        print(f"[{level}] {msg}")
    codex = shutil.which("codex")
    line("OK" if codex else "FAIL", f"codex CLI：{codex or '未安装'}")
    if codex:
        line("OK", run(["codex", "--version"], check=False).strip())
    try:
        cfg = project.config
    except ConfigError as e:
        line("FAIL", str(e))
        sys.exit(1)
    ex = cfg["executor"]
    baton_state = "存在" if os.path.exists(project.baton_config_path) else "缺失"
    line("OK" if baton_state == "存在" else "WARN",
         f"conductor 模型：{cfg['conductor']['model']}；monitor 模型：{cfg['monitor']['model']}；"
         f"baton.json：{baton_state}（{project.baton_config_path}）")
    cache = read_json(os.path.expanduser("~/.codex/models_cache.json"), {})
    models = cache.get("models", cache) if isinstance(cache, dict) else cache
    model = next((m for m in models or [] if isinstance(m, dict) and m.get("slug") == ex["model"]), None)
    if model:
        efforts = [x.get("effort") for x in model.get("supported_reasoning_levels") or []]
        line("OK" if ex["reasoning_effort"] in efforts else "FAIL",
             f"模型 {ex['model']}，思考深度 {ex['reasoning_effort']}（可选：{'/'.join(efforts)}）")
    else:
        line("WARN", f"models_cache 里没找到 {ex['model']}（可能只是缓存未刷新）")
    line("OK" if ex.get("service_tier") != "priority" and ex.get("disable_fast_mode") else "WARN",
         f"service_tier={ex.get('service_tier')}，disable_fast_mode={ex.get('disable_fast_mode')}（加速模式应关闭）")
    data = safe_quota(block=True)
    desc = codex_quota.describe(data)
    if codex_quota.is_low(data):
        print(desc.splitlines()[0])
        line("WARN", desc.splitlines()[-1])
    else:
        line("OK" if data and data.get("source") == "live" else "WARN", desc.splitlines()[-1])
    launcher = os.path.realpath(os.path.join(SKILL_DIR, "bin", "baton"))
    on_path = shutil.which("baton")
    if not on_path:
        line("WARN", f"PATH 上没有 baton；可以直接用 {launcher}")
    elif os.path.realpath(on_path) != launcher:
        line("WARN", f"PATH 上的 baton（{on_path}）不是这个 skill 的，请改用 {launcher} 或处理同名冲突")
    else:
        line("OK", f"baton 命令：{on_path}")
    council = os.path.join(council_dir(), "scripts", "launch.sh")
    line("OK" if os.path.exists(council) else "WARN",
         "council skill：" + ("已安装" if os.path.exists(council) else f"未安装（{council_dir()}，./install.sh --council）"))
    if os.path.exists(council):
        line(*council_check())
    top = git_repo_top(project.root)
    line("OK", f"项目 {project.root}（id {project.project_id}）："
         + (f"在 git 仓库里，git 顶层 {top}" if top else "不是 git 仓库"))
    shadow_ok = os.path.isfile(os.path.join(project.snap_git_dir, "HEAD"))
    line("OK" if shadow_ok else "WARN",
         f"快照：影子仓库 {project.snap_git_dir}" + ("" if shadow_ok else "（尚未建立，baton init 会建立）"))
    line("OK" if os.path.isdir(project.dir) else "WARN", ".baton/：" + ("已初始化" if os.path.isdir(project.dir) else "未初始化（baton init）"))
    sl = statusline_report(project).splitlines()[0]
    good = "已安装 Baton 版本" in sl
    line("OK" if good else "WARN", sl + ("" if good else "；baton init 或 baton statusline install"))
    if good and is_git(project.root) and (settings_tracked(project) or not settings_ignored(project)):
        line("WARN", ".claude/settings.local.json 没有被 git 忽略，里面的本机路径可能被提交")
    sys.exit(0 if ok else 1)


def main():
    ap = argparse.ArgumentParser(prog="baton", description="Claude 指挥、Codex 执行的工作流工具")
    ap.add_argument("--root", help="项目目录（默认：从当前目录往上第一个含 baton.json 的目录，不越过 git 根）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("init", help="在项目里创建 .baton/，并配置带 Codex 额度的 statusline")
    s.add_argument("--no-statusline", action="store_true", help="不改项目的 statusline")
    s.add_argument("--accept-boundary", action="store_true", help="确认项目目录可以是 git 顶层下的子目录")
    s.add_argument("--relink", action="store_true", help="项目目录换过位置或名字时，把原来的项目外状态接过来")
    s.add_argument("--fresh", action="store_true", help="复制项目或原状态不存在时，开始新的项目外状态")
    s = sub.add_parser("statusline", help="项目 statusline 的 Codex 额度行")
    s.add_argument("action", nargs="?", default="status", choices=["install", "uninstall", "status"])
    s.add_argument("--force", action="store_true", help="settings.local.json 被 git 跟踪或指向项目外时也安装")
    s = sub.add_parser("new", help="新建任务并生成简报模板")
    s.add_argument("task")
    s.add_argument("--brief", help="用已有文件作为简报")
    s = sub.add_parser("start", help="启动首轮（codex exec）")
    s.add_argument("task")
    for name, helptext in (("resume", "带指挥消息续跑（codex exec resume）"), ("steer", "停止当前轮并带纠偏消息续跑")):
        s = sub.add_parser(name, help=helptext)
        s.add_argument("task")
        s.add_argument("message_file", nargs="?")
        s.add_argument("--message")
        if name == "resume":
            s.add_argument("--kind", default="回复", help="消息类型：决策/审阅/纠偏/回复")
    s = sub.add_parser("wait", help="阻塞到本轮结束、监管时间到或额度告警（请在后台运行）")
    s.add_argument("task")
    s.add_argument("--tick", type=float, help="监管间隔分钟数（默认取配置）")
    s = sub.add_parser("status", help="监管快照")
    s.add_argument("task")
    s = sub.add_parser("supervised", help="记录一次监管结论并重置计时")
    s.add_argument("task")
    s.add_argument("--verdict", required=True, choices=["on_track", "drifting", "breaking"])
    s.add_argument("--note", required=True)
    s = sub.add_parser("stop", help="中断当前轮")
    s.add_argument("task")
    s.add_argument("--reason")
    s = sub.add_parser("checkpoint", help="手动建回滚点")
    s.add_argument("task")
    s.add_argument("label", nargs="?")
    s = sub.add_parser("refs", help="列出回滚点")
    s.add_argument("task")
    s = sub.add_parser("diff", help="相对回滚点的改动")
    s.add_argument("task")
    s.add_argument("--since", default="leg", help="base / leg / tick / report / baton refs 列出的回滚点")
    s.add_argument("--stat", action="store_true")
    s = sub.add_parser("rollback", help="把工作区文件恢复到某个回滚点（先自动保存当前状态）")
    s.add_argument("task")
    s.add_argument("ref")
    s.add_argument("--yes", action="store_true")
    sub.add_parser("list", help="列出任务")
    s = sub.add_parser("config", help="显示生效配置")
    s.add_argument("--json", action="store_true", help="输出完整合并后的 JSON 配置")
    s = sub.add_parser("quota", help="Codex 额度")
    s.add_argument("--json", action="store_true")
    s.add_argument("--refresh", action="store_true")
    sub.add_parser("doctor", help="环境自检")
    s = sub.add_parser("_run")
    s.add_argument("task")
    s.add_argument("n")
    args = ap.parse_args()
    home = os.environ.get("BATON_HOME")
    if home and not os.path.isabs(os.path.expanduser(home)):
        # the detached runner starts in the project dir: pin a relative BATON_HOME before it inherits it
        os.environ["BATON_HOME"] = os.path.abspath(os.path.expanduser(home))
    root = canonical_dir(args.root) if args.root else find_project_dir(os.getcwd(), legacy=args.cmd != "init")
    project = Project(root)
    if args.cmd not in ("init", "quota", "doctor", "config", "statusline"):
        project.require_init()
    if args.cmd not in ("init", "quota") and is_initialized(project.root):
        if not project.recorded_id():
            project.record_id()
        project.check_identity(warn_only=args.cmd in ("doctor", "config"))
    handler = {
        "init": cmd_init, "statusline": cmd_statusline, "new": cmd_new, "start": cmd_start,
        "resume": cmd_resume, "steer": cmd_steer, "wait": cmd_wait, "status": cmd_status,
        "supervised": cmd_supervised, "stop": cmd_stop, "checkpoint": cmd_checkpoint, "refs": cmd_refs,
        "diff": cmd_diff, "rollback": cmd_rollback, "list": cmd_list, "config": cmd_config, "quota": cmd_quota,
        "doctor": cmd_doctor, "_run": cmd_run,
    }[args.cmd]
    if args.cmd == "steer":
        args.kind = "纠偏"
    try:
        handler(project, args)
    except ConfigError as e:
        die(str(e))


if __name__ == "__main__":
    main()
