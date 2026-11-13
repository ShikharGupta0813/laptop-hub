"""Graded safety levels for the assistant's tool calls (see docs/api/assistant-safety.md).

N1  read-only: runs freely.
N2  changes something: the normal approval card (automatic in Full Control).
N3  CRITICAL: can lose data or take the laptop away. Never automatic, not even in Full Control: the owner types
    CONFIRM on the card and then has a cancellable countdown before anything runs.

`classify(tool_name, args)` is a pure function and deliberately conservative: when a command line cannot be
understood (encoded PowerShell, Invoke-Expression, a pipe into a shell) it is N3. Anything that is not N3 and
changes something is N2, so a false alarm costs the owner one typed word, never the laptop.
"""
from __future__ import annotations

import re
from typing import Any

N1, N2, N3 = "N1", "N2", "N3"
_RANK = {N1: 1, N2: 2, N3: 3}
CONFIRM_WORD = "CONFIRM"
N3_DELAY_SECONDS = 10.0


def worst(*levels: str | None) -> str:
    """The most severe of the given levels (None and unknown values count as N1)."""
    best = N1
    for level in levels:
        if level in _RANK and _RANK[level] > _RANK[best]:
            best = level
    return best


# Tools the assistant ships with, and what they are at least. Other tools declare `level` on ExtraTool.
BUILTIN_BASE = {"run_command": N2, "run_script": N2, "write_file": N2, "add_note": N2}

# laptop_action ids: what they are, whatever the tool's own approval rule says.
LAPTOP_N3 = {"shutdown", "restart", "sleep", "hibernate", "lock_screen", "logoff", "log_off", "sign_out",
             "lock", "format", "wipe"}
LAPTOP_N1 = {"list", "system_info", "battery", "disk_usage", "network_info", "uptime", "list_processes",
             "volume_up", "volume_down", "mute", "play_pause", "next_track", "previous_track"}

# Paths that hold credentials, the hub's own settings, or the way back into the machine.
_SECRET_PATH = re.compile(
    r"(?:^|[\\/\s=])(?:\.laptop-hub|hub\.json|\.ssh|\.aws|\.gnupg|\.kube|\.docker[\\/]config\.json|\.npmrc|\.netrc|"
    r"\.pypirc|\.git-credentials|\.env(?:\.[a-z0-9_]+)?|id_rsa|id_ed25519|id_ecdsa|authorized_keys|known_hosts|"
    r"credentials(?:\.[a-z]+)?|secrets?\.(?:json|ya?ml|env|txt|toml|ini)|login data|cookies\.sqlite|ntds\.dit|keychains?|"
    r"shadow|passwd|sudoers|hub_token|\.pem|\.pfx|\.p12|\.kdbx)(?=$|[\\/\s\"';|&)])")
_SYSTEM_PATH = re.compile(r"(?:^|[\\/])(?:windows[\\/]system32|windows[\\/]syswow64|etc|boot|startup|"
                          r"start menu[\\/]programs[\\/]startup)(?:[\\/]|$)")

_QUOTES = re.compile(r"[\"'`^]")
_SEGMENT_SPLIT = re.compile(r"\r?\n|;|&&|\|\||\||&(?!>)")
_SUDO_LIKE = {"sudo", "doas", "runas", "su", "env", "nohup", "time", "timeout", "start", "call", "cmd", "powershell",
              "pwsh", "sh", "bash", "zsh", "dash", "fish", "wsl", "xargs", "parallel", "exec", "command", "builtin",
              "start-process", "invoke-command", "icm", "invoke-expression", "iex", "nice", "ionice", "setsid",
              "stdbuf", "busybox", "eval", "find", "wmic", "psexec", "start-job", "conhost", "cscript", "wscript",
              "mshta", "rundll32", "forfiles", "for", "if", "do", "then", "else", "foreach-object", "foreach", "%",
              "invoke-item", "ii", "where-object", "?"}

DELETE_CMDS = {"rm", "del", "erase", "rd", "rmdir", "ri", "remove-item", "unlink", "shred", "srm", "sdelete",
               "sdelete64", "trash", "trash-put", "rimraf", "remove-itemproperty", "rp", "clear-recyclebin",
               "remove-recyclebin", "clear-content", "clc"}
POWER_CMDS = {"shutdown", "poweroff", "halt", "reboot", "telinit", "stop-computer", "restart-computer", "psshutdown",
              "logoff", "tsdiscon", "hibernate", "slidetoshutdown", "rundll32.exe"}
DISK_CMDS = {"format", "format-volume", "diskpart", "fdisk", "sfdisk", "cfdisk", "gdisk", "parted", "wipefs",
             "clear-disk", "remove-partition", "initialize-disk", "new-partition", "set-disk", "repair-volume",
             "mkswap", "cryptsetup", "blkdiscard", "hdparm"}
KILL_NAMED = {"killall", "pkill", "taskkill", "stop-process", "spps", "kill", "wmic"}
SYSTEM_PROCS = {"winlogon", "csrss", "lsass", "svchost", "wininit", "services", "smss", "system", "explorer",
                "dwm", "systemd", "init", "sshd", "tailscaled", "tailscale", "msmpeng", "python", "pythonw", "uvicorn"}
REGISTRY_CMDS = {"regedit", "regini", "regsvr32"}
SECURITY_SERVICES = re.compile(r"tailscale|windefend|defender|sense|wscsvc|mpssvc|firewall|wuauserv|eventlog|"
                               r"securityhealth|bfe|sshd|termservice|winrm")
INSTALL_VERBS = {"install", "add", "i", "upgrade", "update", "uninstall", "remove", "purge", "reinstall", "autoremove",
                 "erase", "localinstall", "groupinstall", "dist-upgrade", "full-upgrade", "upgrade-all"}
PKG_MGRS = {"winget", "choco", "choco.exe", "scoop", "apt", "apt-get", "aptitude", "dpkg", "dnf", "yum", "zypper",
            "pacman", "apk", "snap", "flatpak", "brew", "port", "nix", "nix-env", "conda", "mamba", "gem",
            "install-module", "install-package", "install-script", "uninstall-package", "uninstall-module",
            "add-appxpackage", "remove-appxpackage", "add-windowspackage", "msiexec", "dism", "dism.exe",
            "pkgadd", "rpm", "installutil", "setup", "winrm"}
NODE_MGRS = {"npm", "yarn", "pnpm", "bun", "pip", "pip3", "pipx", "uv", "poetry", "cargo", "go", "gem", "dotnet",
             "composer", "deno"}

# Whole-line patterns, run on the normalised text (lower case, quoting removed).
_LINE_N3 = [
    (re.compile(r"-(?:enc|ec|e|encodedcommand|encodedc\w*)\s+[a-z0-9+/=]{12,}"), "encoded PowerShell"),
    (re.compile(r"\b(?:invoke-expression|iex)\b"), "Invoke-Expression"),
    (re.compile(r"\|\s*(?:sh|bash|zsh|dash|iex|invoke-expression|powershell|pwsh|cmd|python3?|sudo)\b"),
     "pipe into a shell"),
    (re.compile(r"\b(?:rmtree|os\.remove|os\.unlink|os\.rmdir|os\.removedirs|shutil\.move|os\.system|"
                r"subprocess\.(?:run|popen|call))\b"), "script that deletes or shells out"),
    (re.compile(r"::delete\s*\(|\.delete\s*\(\s*\$?true"), ".NET delete"),
    (re.compile(r">\s*/dev/(?:sd|nvme|hd|disk|mmcblk)"), "write to a raw disk"),
    (re.compile(r"\bof=/dev/|\bof=\\\\\.\\"), "dd to a raw disk"),
    (re.compile(r":\(\)\s*\{"), "fork bomb"),
    (re.compile(r"-delete\b|-exec(?:dir)?\s+(?:rm|del|shred)\b"), "find that deletes"),
    (re.compile(r"\b(?:set-mppreference|add-mppreference|remove-mppreference|disable-windowsoptionalfeature|"
                r"disable-netfirewallrule|set-netfirewallprofile|remove-netfirewallrule|new-netfirewallrule|"
                r"set-netfirewallrule|disable-networkadapter|set-executionpolicy|disable-computerrestore|"
                r"clear-eventlog|remove-eventlog|disable-bitlocker|clear-tpm|uninstall-windowsfeature)\b"),
     "security setting"),
    (re.compile(r"\bnet(?:sh)?\s+(?:advfirewall|firewall)\b|\bnetsh\s+(?:winsock\s+reset|int(?:erface)?\s+ip\s+reset)"),
     "firewall/network reset"),
    (re.compile(r"\bufw\s+(?:disable|reset|delete|allow|deny|default)|\biptables\b.*\s-[fxpd]\b|\bnft\s+(?:flush|delete)|"
                r"\bfirewall-cmd\b.*--(?:remove|add|panic|set-default)"), "firewall rule"),
    (re.compile(r"\btailscale\s+(?:down|logout|set\b.*--(?:accept-routes|exit-node|shields-up|ssh)|"
                r"cert|serve|funnel|file\s+cp)|\btailscaled?\b.*--(?:cleanup|remove)"), "Tailscale"),
    (re.compile(r"\b(?:stop|disable|remove|delete)-?service\b.*(?:tailscale|windefend|defender|mpssvc|firewall)"),
     "security service"),
    (re.compile(r"\bmpcmdrun\b.*-(?:removedefinitions|disablerealtime)|\bdefender\b.*(?:disable|exclusion|off)"),
     "Defender"),
    (re.compile(r"\bcmdkey\b|\bvaultcmd\b|\bmimikatz\b|\bsecurity\s+(?:find|dump|export|delete)-|"
                r"\bgh\s+auth\s+(?:token|logout)|\bgit\s+credential\b|\bssh-keygen\b|\bopenssl\s+(?:enc|rsa|pkcs)|"
                r"\bgpg\b.*--(?:export|decrypt|delete)|\bprintenv\b.*(?:token|secret|key|pass)"), "credentials"),
    (re.compile(r"\brobocopy\b.*\s/(?:mir|purge|move|mov)\b"), "robocopy mirror/purge"),
    (re.compile(r"\bcipher\b.*\s/w\b"), "cipher wipe"),
    (re.compile(r"\b(?:vssadmin|wbadmin)\s+delete|\bbcdedit\b|\bwevtutil\s+(?:cl|clear-log)|\bfsutil\s+(?:usn\s+delete|"
                r"behavior\s+set)|\btakeown\b|\bicacls\b.*\s/(?:grant|deny|reset|setowner)|\bcacls\b|\bnet\s+user\b|"
                r"\bnet\s+localgroup\b|\bschtasks\b.*\s/(?:create|delete|change)|\bsc(?:\.exe)?\s+(?:create|delete|"
                r"config|stop|failure)|\bnet\s+stop\b|\bnew-service\b|\bset-service\b|\bstop-service\b|"
                r"\bremove-service\b|\bsystemctl\s+(?:stop|disable|mask|kill|poweroff|reboot|halt|suspend|hibernate|"
                r"hybrid-sleep|isolate|rescue|emergency|kexec)|\bservice\s+\S+\s+(?:stop|disable)|"
                r"\bchmod\s+(?:-r|--recursive)|\bchown\s+(?:-r|--recursive)|\bicacls\b.*\s/t\b"),
     "system administration"),
    (re.compile(r"\bcrontab\s+-r|\bat\s+now|\bwall\b|\bkillall5\b|\bchattr\b|\bmount\b\s+-o\s+remount"), "system"),
]

# git is checked per command (so `echo git push --force` stays harmless): text = everything after `git`.
_GIT_OPTS = r"(?:(?:-c|-C|--git-dir|--work-tree)\s+\S+\s+)*"
_GIT_PUSH = re.compile(r"^" + _GIT_OPTS + r"push\b.*(?:\s-[a-z]*f[a-z]*(?:\s|$)|--force|--mirror|--delete|\s\+\S|\s:\S|\s-d\b)")
_GIT_LOSS = re.compile(r"^" + _GIT_OPTS + r"(?:clean\b.*\s-[a-z]*[fdx]|reset\b.*--hard|filter-(?:branch|repo)|"
                       r"reflog\s+expire|gc\b.*--prune|update-ref\s+-d|branch\s+-[a-z]*d[a-z]*\s+(?:main|master)\b|"
                       r"checkout\s+(?:-f|--force)|stash\s+clear)")

# First-word rules: (set of command names) -> checker(rest_tokens, rest_text) -> reason or None
_LOCK_DLL = re.compile(r"user32(?:\.dll)?,?\s*lockworkstation|powrprof(?:\.dll)?,?\s*setsuspendstate|"
                       r"shell32.*,\s*shutdown|\bsetsuspendstate\b|\blockworkstation\b|\bexitwindowsex\b")


def normalise(command: str) -> str:
    """Lower case, quoting/escape characters and `^` or backtick evasions removed, spaces collapsed."""
    text = str(command or "")
    text = text.replace("\x00", "")
    text = _QUOTES.sub("", text)
    text = text.replace("\\\n", " ")
    return re.sub(r"[ \t]+", " ", text.lower()).strip()


def _base(token: str) -> str:
    """`C:\\Windows\\System32\\Shutdown.EXE` -> `shutdown`; `/usr/bin/rm` -> `rm`; `.\\rm` -> `rm`."""
    token = token.strip("()&{}$@ ").rstrip(",")
    token = re.split(r"[\\/]", token)[-1]
    for suffix in (".exe", ".com", ".bat", ".cmd", ".ps1"):
        if token.endswith(suffix):
            token = token[: -len(suffix)]
    return token


def _flags(tokens: list[str]) -> str:
    return " ".join(t for t in tokens if t.startswith(("-", "/")))


def _trash_only(tokens: list[str]) -> bool:
    """A delete whose every target is inside a hub trash folder (the one place a delete is routine)."""
    targets = [t for t in tokens if not t.startswith(("-", "/")) or re.match(r"^/[^/]*[\\/.]", t) or t.count("/") > 1]
    targets = [t for t in targets if not re.fullmatch(r"-+[a-z-]+|/[a-z?]", t)]
    return bool(targets) and all(re.search(r"\.laptop-hub[\\/]trash[\\/]\S|[\\/]\.trash[\\/]\S", t)
                                 and ".." not in t and "*" not in t for t in targets)


def _command_danger(tokens: list[str]) -> str | None:
    """Reason a single command (first token = program) is critical, else None."""
    if not tokens:
        return None
    cmd, rest = _base(tokens[0]), tokens[1:]
    text = " ".join(rest)
    if cmd == "git":
        if _GIT_PUSH.search(text):
            return "git push --force"
        return "git history/data loss" if _GIT_LOSS.search(text) else None
    if cmd in DELETE_CMDS:
        if cmd == "clear-content" or cmd == "clc":
            return "clears file contents"
        if _trash_only(rest):
            return None
        return "delete"
    if cmd in POWER_CMDS:
        if cmd == "shutdown" and re.search(r"(?:^| )[/-]a(?: |$)", text):
            return None                       # `shutdown /a` only ABORTS a pending shutdown
        if cmd == "rundll32":
            return "rundll32 power/lock" if _LOCK_DLL.search(text) else None
        return "power"
    if cmd == "init" and re.fullmatch(r"[0156]", (rest[0] if rest else "")):
        return "power"
    if cmd == "systemctl" and re.search(r"\b(?:poweroff|reboot|halt|suspend|hibernate|hybrid-sleep|kexec)\b", text):
        return "power"
    if cmd == "loginctl" and re.search(r"\b(?:lock-session|lock-sessions|terminate|poweroff|reboot|suspend|hibernate)", text):
        return "power/lock"
    if cmd == "pmset" and re.search(r"sleepnow|displaysleepnow|restart|shutdown", text):
        return "power"
    if cmd in ("dbus-send", "gdbus") and re.search(r"poweroff|reboot|suspend|hibernate|lock", text):
        return "power"
    if cmd in ("osascript",) and re.search(r"shut ?down|restart|sleep|log ?out", text):
        return "power"
    if cmd in ("wmic",) and re.search(r"\b(?:shutdown|reboot|delete|call\s+(?:terminate|delete))\b|\bos\b.*\bwin32shutdown", text):
        return "wmic"
    if cmd in ("rundll32",):
        return "rundll32 power/lock" if _LOCK_DLL.search(text) else None
    if cmd == "mkfs" or cmd.startswith("mkfs.") or cmd in DISK_CMDS:
        return "disk"
    if cmd == "diskutil" and re.search(r"erase|partition|apfs\s+delete|zero", text):
        return "disk"
    if cmd == "dd" and re.search(r"\bof=", text):
        return "dd"
    if cmd in ("reg",) and rest and rest[0] in ("add", "delete", "import", "restore", "load", "unload", "copy",
                                                  "save", "flags"):
        return "registry write"
    if cmd in REGISTRY_CMDS and cmd != "regsvr32":
        return "registry"
    if cmd in ("set-itemproperty", "sp", "new-itemproperty", "remove-itemproperty", "rp", "new-item", "ni", "set-item",
               "si", "rename-itemproperty", "copy-itemproperty", "cpp", "move-itemproperty", "mp", "clear-itemproperty",
               "clp") and re.search(r"\bhk|registry::", text):
        return "registry write"
    if cmd in KILL_NAMED:
        procs = re.findall(r"[a-z0-9_.*-]+", text)
        if any(p.removesuffix(".exe") in SYSTEM_PROCS for p in procs):
            return "kills a system process"
        if cmd in ("killall", "pkill"):
            return "kills processes by name"
        if cmd == "taskkill":
            if re.search(r"/im\b|/t\b|\*|/fi\b", text):
                return "kills processes by name or tree"
            return None                        # /pid N alone: one process, a normal N2
        if cmd in ("stop-process", "spps"):
            if re.search(r"-(?:name|n)\b|\*|-inputobject|-whatif:\$false", text) or not re.search(r"-id\b|\b\d{2,}\b", text):
                return "kills processes by name or by pipeline"
            return None
        if cmd == "kill":
            if re.search(r"(?:^| )-?(?:1|0)(?: |$)|(?:^| )-9?\s*-1(?: |$)|\$\(|`|pgrep|pidof|\*", text) or len(
                    [t for t in rest if re.fullmatch(r"\d+", t)]) > 3:
                return "kills pid 1/0/-1 or many"
            return None
    if cmd in PKG_MGRS:
        if cmd in ("winget", "choco", "scoop", "apt", "apt-get", "aptitude", "dnf", "yum", "zypper", "pacman", "apk",
                   "snap", "flatpak", "brew", "port", "nix-env", "conda", "mamba", "gem", "dpkg", "rpm"):
            verbs = [t for t in rest if not t.startswith("-")]
            if verbs and (verbs[0] in INSTALL_VERBS or verbs[0] in ("-s", "-u", "-i", "-e", "-r", "-r")
                          or re.fullmatch(r"-[a-z]*[siurde][a-z]*", rest[0] if rest else "")):
                return "installs or removes software"
            if cmd in ("dpkg", "rpm") and re.search(r"(?:^| )-[a-z]*[ier]\b|--install|--remove|--purge|--erase", text):
                return "installs or removes software"
            return None
        if cmd in ("msiexec", "dism", "installutil", "pkgadd", "install-module", "install-package", "install-script",
                   "uninstall-package", "uninstall-module", "add-appxpackage", "remove-appxpackage",
                   "add-windowspackage", "setup"):
            return "installs or removes software"
    if cmd in NODE_MGRS or cmd in ("python", "python3", "py", "pythonw"):
        words = [t for t in rest if not t.startswith("-") or t in ("-g", "--global")]
        if cmd in ("python", "python3", "py", "pythonw"):
            if re.search(r"-m\s+pip\s+(?:install|uninstall|download)|-m\s+ensurepip|-m\s+venv\b.*--clear", text):
                return "installs software"
            return None
        if cmd in ("pip", "pip3", "pipx", "uv", "poetry", "conda"):
            if words and words[0] in ("install", "uninstall", "add", "remove", "tool", "sync", "self"):
                if words[0] == "tool" and not (len(words) > 1 and words[1] in ("install", "uninstall", "upgrade")):
                    return None
                return "installs software"
            return None
        if cmd in ("cargo", "go", "gem", "dotnet", "composer", "deno"):
            if words and (words[0] == "install" or (cmd == "dotnet" and "tool" in words and "install" in words)
                          or (cmd == "composer" and words[0] in ("require", "global"))):
                return "installs software"
            return None
        if cmd in ("npm", "yarn", "pnpm", "bun"):
            is_global = bool(re.search(r"(?:^| )(?:-g|--global|global)(?: |$)", text))
            if words and words[0] in ("install", "i", "add", "uninstall", "remove", "rm", "un", "update", "upgrade",
                                       "link", "unlink", "dlx", "x", "exec", "create"):
                if is_global or words[0] in ("dlx", "x", "exec", "create", "add") or len(words) > 1:
                    return "installs software"
                return "installs software"            # a bare `npm install` runs package install scripts
            if words and words[0] == "global":
                return "installs software"
            return None
    if cmd in ("curl", "wget", "invoke-webrequest", "iwr", "irm", "invoke-restmethod", "bitsadmin", "certutil",
               "start-bitstransfer"):
        if re.search(r"-(?:o|outfile|out-file|output)\b.*\.(?:exe|msi|dll|ps1|bat|cmd|sh|vbs|jar|scr)|"
                     r"urlcache|-decode|-encode|\.exe\b|\.msi\b", text):
            return "downloads a program"
        return None
    if cmd in ("chmod", "chown", "chgrp") and re.search(r"(?:^| )-[a-z]*r|--recursive|777|\s/(?:\s|$)", text):
        return "permissions"
    if cmd in ("mv", "move", "ren", "rename", "cp", "copy", "xcopy", "robocopy") and re.search(
            r"\s/(?:\s|$)|c:\\?(?:\s|$)|\.laptop-hub|hub\.json", text):
        return "moves system files"
    if cmd in ("ssh-keygen", "ssh-add", "keytool", "cmdkey", "vaultcmd", "mimikatz", "gpg", "pass", "op", "bw"):
        return "credentials"
    if cmd in ("sc", "net", "net1") and re.search(r"^(?:stop|delete|config|create|failure|user|localgroup|accounts)", text):
        return "system administration"
    if cmd in ("tailscale",) and re.match(r"(?:down|logout|set|up|cert|serve|funnel|switch|lock|ssh)\b", text):
        return "Tailscale"
    if cmd in ("netsh", "ufw", "iptables", "ip6tables", "nft", "firewall-cmd", "pfctl", "set-netfirewallprofile"):
        return "firewall"
    if cmd in ("set-mppreference", "add-mppreference", "mpcmdrun"):
        return "Defender"
    if cmd in ("bcdedit", "vssadmin", "wbadmin", "takeown", "icacls", "cacls", "wevtutil", "fsutil", "schtasks", "at",
               "crontab", "visudo", "passwd", "usermod", "userdel", "useradd", "groupadd", "chpasswd", "lusrmgr",
               "bitlockertogo", "manage-bde", "cipher", "attrib", "setx", "sysprep", "reagentc", "dsregcmd",
               "powercfg", "gpupdate", "secedit", "auditpol", "lodctr", "unlodctr", "mountvol"):
        if cmd == "attrib" or cmd == "powercfg" or cmd == "gpupdate" or cmd == "crontab" and "-l" in rest:
            return None
        if cmd in ("setx",):
            return "changes machine environment" if re.search(r"\s/m\b", text) else None
        if cmd == "cipher":
            return "cipher" if re.search(r"/w|/d", text) else None
        if cmd == "schtasks":
            return "scheduled task" if re.search(r"/(?:create|delete|change|run|end)", text) else None
        if cmd == "fsutil":
            return "fsutil" if re.search(r"usn delete|behavior set|file setzerodata|volume dismount", text) else None
        return "system administration"
    return None


def command_reason(command: str, _depth: int = 0) -> str | None:
    """Why a command line is critical (N3), or None. Conservative; see the module docstring."""
    text = normalise(command)
    if not text:
        return None
    probe = re.sub(r"\.laptop-hub[\\/]trash[\\/]", "trash/", text)     # the hub's own trash is the one routine place
    if _SECRET_PATH.search(probe):
        return "touches credentials or the hub's own settings"
    for pattern, why in _LINE_N3:
        if pattern.search(text):
            return why
    if _depth > 4:
        return "nested too deeply to check"
    for raw in _SEGMENT_SPLIT.split(text):
        tokens = [t for t in raw.strip().strip("(){}").split(" ") if t]
        tokens = _strip_prefix(tokens)
        if not tokens:
            continue
        why = _command_danger(tokens)
        if why:
            return why
        if _base(tokens[0]) in _SUDO_LIKE:
            # A wrapper (sudo, cmd /c, powershell -c, xargs, find -exec...): check the command it launches, wherever it
            # sits among the arguments, and the whole remainder as a fresh command line.
            tail = tokens[1:]
            for i in range(min(len(tail), 8)):
                found = _command_danger(_strip_prefix(tail[i:]))
                if found:
                    return found
            found = command_reason(" ".join(tail), _depth + 1) if tail else None
            if found:
                return found
    # Command substitution and subshells: $(...) and backticks-turned-plain, and `;` inside parentheses.
    for inner in re.findall(r"\$\(([^)]*)\)", text):
        found = command_reason(inner, _depth + 1)
        if found:
            return found
    return None


def _strip_prefix(tokens: list[str]) -> list[str]:
    """Drop leading `&`, `.` (call operators), `@`/`!`, env assignments and `-` noise so tokens[0] is a program."""
    i = 0
    while i < len(tokens) and (tokens[i] in ("&", ".", "@", "!", "-", "|", "(", "{", "then", "do", "else", "elif")
                               or re.fullmatch(r"[a-z_][a-z0-9_]*=\S*", tokens[i])
                               or re.fullmatch(r"\$?[a-z_][a-z0-9_:]*=\S*", tokens[i])):
        i += 1
    return tokens[i:]


def path_reason(path: Any) -> str | None:
    text = normalise(path if isinstance(path, str) else "")
    if not text:
        return None
    if _SECRET_PATH.search(text):
        return "credentials or the hub's own settings"
    if _SYSTEM_PATH.search(text.replace("\\", "/")):
        return "system or startup folder"
    return None


def _command_binaries(command: str) -> tuple[str, ...]:
    """Every program a command line starts (used to scope a remembered approval)."""
    out: list[str] = []
    for raw in _SEGMENT_SPLIT.split(normalise(command)):
        tokens = _strip_prefix([t for t in raw.strip().strip("(){}").split(" ") if t])
        if tokens:
            out.append(_base(tokens[0]))
    return tuple(sorted(set(out))) or ("",)


def arg_shape(tool_name: str, args: dict) -> tuple:
    """The part of a call a remembered approval is keyed on: the tool plus a normalised argument shape.

    run_command: the program(s) it starts + the folder; write_file: the folder; laptop_action/jarvis: the action id;
    anything else: the argument names only (so 'asana_comment' on any task is one permission)."""
    args = args if isinstance(args, dict) else {}
    if tool_name == "run_command":
        cwd = re.sub(r"[\\/]+$", "", normalise(args.get("cwd") or "")).replace("\\", "/")
        return (tool_name, _command_binaries(args.get("command", "")), cwd, normalise(args.get("shell") or ""))
    if tool_name == "write_file":
        folder = re.sub(r"[\\/][^\\/]*$", "", normalise(args.get("path") or "")).replace("\\", "/")
        return (tool_name, folder)
    if tool_name == "run_script":
        return (tool_name, str(args.get("name", "")))
    if tool_name in ("laptop_action", "jarvis"):
        return (tool_name, normalise(args.get("action") or ""))
    return (tool_name, tuple(sorted(str(k) for k in args)))


def describe_shape(tool_name: str, args: dict) -> str:
    """A short human label for the Settings list."""
    shape = arg_shape(tool_name, args)
    if tool_name == "run_command":
        where = f" in {shape[2]}" if shape[2] else ""
        return f"run_command: {', '.join(shape[1])}{where}"
    if tool_name == "write_file":
        return f"write_file in {shape[1] or '(current folder)'}"
    if len(shape) > 1 and isinstance(shape[1], str) and shape[1]:
        return f"{tool_name}: {shape[1]}"
    return tool_name


def classify(tool_name: str, args: Any = None, *, base: str | None = None) -> str:
    """'N1' | 'N2' | 'N3' for one tool call. Pure: no I/O, no state.

    `base` is the tool's own floor (an ExtraTool that needs approval is at least N2; its declared `level`
    goes here). The result is never lower than `base` or than what the arguments reveal."""
    args = args if isinstance(args, dict) else {}
    level = worst(base, BUILTIN_BASE.get(str(tool_name)))
    if tool_name == "run_command":
        if command_reason(args.get("command", "")):
            return N3
    elif tool_name == "write_file":
        if path_reason(args.get("path")):
            return N3
    elif tool_name in ("read_file", "list_dir", "file_info", "show_file"):
        if _SECRET_PATH.search(normalise(args.get("path"))):
            return N3
    elif tool_name == "laptop_action":
        action = normalise(args.get("action") or "")
        if action in LAPTOP_N3:
            return N3
        if action == "kill_process":
            pid = (args.get("params") or {}).get("pid") if isinstance(args.get("params"), dict) else None
            try:
                if int(pid) <= 4:
                    return N3
            except (TypeError, ValueError):
                pass
        if action in LAPTOP_N1 and level == N1:
            return N1
        return worst(level, N2)
    elif tool_name == "jarvis":
        return worst(level, N2) if normalise(args.get("action") or "") != "status" else level
    return level


# Tools that send something out to other people (email, an application, a public post). They need the owner's tap EVERY
# time: Full Control, auto-approve and remembered approvals do not apply to them. (N3 is stricter: typed CONFIRM.)
ALWAYS_ASK = frozenset({"send_email", "send_application", "devto_publish"})
