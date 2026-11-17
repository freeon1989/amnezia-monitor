# -*- coding: utf-8 -*-
"""Включение и выключение DNS-журнала на сервере AmneziaVPN по SSH.

Делает ту же настройку, что можно выполнить вручную на сервере:
  * Unbound в контейнере amnezia-dns: log-queries: yes, logfile: unbound.log;
  * ротация и очистка раз в 15 минут, хранение 24 часа, потолок 50 МБ;
  * при необходимости — журнал AdGuard Home (querylog.json).

Идемпотентно. Перезапускается только контейнер DNS, и только если конфиг
реально изменился. Контейнеры VPN (awg/wg/openvpn) и службы каскада
(cascade-xray и т.п.) не трогаются.
"""
from __future__ import annotations

import re
import sys
import time
from pathlib import Path
from typing import Optional

UNBOUND_CONF = "/opt/unbound/etc/unbound/unbound.conf"
UNBOUND_BAK = "/opt/unbound/etc/unbound/unbound.conf.bak-before-querylog"
UNBOUND_LOG = "/opt/unbound/etc/unbound/unbound.log"
UNBOUND_ARCH = "/opt/unbound/etc/unbound/log-archive"
ADGUARD_YAML_CANDIDATES = (
    "/opt/adguardhome/AdGuardHome.yaml",
    "/opt/adguardhome/AdGuardHome.yml",
    "/opt/adguardhome/work/AdGuardHome.yaml",
)
ADGUARD_QUERYLOG = "/opt/adguardhome/work/data/querylog.json"
CLEANUP_SCRIPT_REMOTE = "/usr/local/sbin/amnezia-dns-log-cleanup.sh"
CLEANUP_SERVICE_REMOTE = "/etc/systemd/system/amnezia-dns-log-cleanup.service"
CLEANUP_TIMER_REMOTE = "/etc/systemd/system/amnezia-dns-log-cleanup.timer"
RETAIN_HOURS = 24
CAP_MB = 50
DNS_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
# Имена, которые ни при каких условиях нельзя restart/kill/rm
_FORBIDDEN_TOUCH = re.compile(r"cascade|xray|awg|wireguard|\bwg\b|openvpn|ipsec|ocserv", re.I)

_SYSTEMCTL_OK = {
    "daemon-reload",
    "enable --now amnezia-dns-log-cleanup.timer",
    "disable --now amnezia-dns-log-cleanup.timer",
    "is-enabled amnezia-dns-log-cleanup.timer",
    "is-active amnezia-dns-log-cleanup.timer",
    "is-active amnezia-dns-log-cleanup.service",
    "is-active cascade-xray.service",
}


def _assets_dir() -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    for p in (base / "deploy", Path(__file__).resolve().parent / "deploy", base):
        if (p / "amnezia-dns-log-cleanup.sh").is_file():
            return p
    raise FileNotFoundError("не найдены файлы deploy/amnezia-dns-log-cleanup.*")


def load_cleanup_assets() -> dict:
    d = _assets_dir()
    return {
        "script": (d / "amnezia-dns-log-cleanup.sh").read_text(encoding="utf-8"),
        "service": (d / "amnezia-dns-log-cleanup.service").read_text(encoding="utf-8"),
        "timer": (d / "amnezia-dns-log-cleanup.timer").read_text(encoding="utf-8"),
    }


def patch_unbound_conf(text: str, enable: bool) -> str:
    """Меняет только раскомментированные log-queries и logfile. Остальное не трогает."""
    lines = text.replace("\r\n", "\n").split("\n")
    out, changed_q, changed_l = [], False, False
    for line in lines:
        if line.lstrip().startswith("#"):
            out.append(line)
            continue
        m = re.match(r"^(\s*)log-queries\s*:", line)
        if m:
            out.append(f"{m.group(1)}log-queries: {'yes' if enable else 'no'}")
            changed_q = True
            continue
        m = re.match(r"^(\s*)logfile\s*:", line)
        if m:
            out.append(f"{m.group(1)}logfile: {'unbound.log' if enable else '/dev/null'}")
            changed_l = True
            continue
        out.append(line)
    if not changed_q:
        raise ValueError("в unbound.conf нет параметра log-queries")
    if not changed_l:
        raise ValueError("в unbound.conf нет параметра logfile")
    return "\n".join(out)


def unbound_enabled(text: str) -> bool:
    q = lf = None
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        m = re.match(r"^\s*log-queries\s*:\s*(\S+)", line)
        if m:
            q = m.group(1).strip().strip('"').lower()
        m = re.match(r"^\s*logfile\s*:\s*(\S+)", line)
        if m:
            lf = m.group(1).strip().strip('"')
    if q != "yes":
        return False
    if not lf or lf in {"/dev/null", '""', ""}:
        return False
    return True


def patch_adguard_yaml(text: str, enable: bool) -> str:
    """В блоке querylog выставляет enabled / file_enabled / interval: 24h. Остальное не переписывает."""
    lines = text.replace("\r\n", "\n").split("\n")
    out = []
    in_ql = False
    seen = {"enabled": False, "file_enabled": False, "interval": False}
    ql_indent = "  "
    for line in lines:
        stripped = line.strip()
        if re.match(r"^querylog\s*:", line):
            in_ql = True
            out.append(line)
            continue
        if in_ql and stripped and not line.startswith((" ", "\t")) and not stripped.startswith("#"):
            # конец блока — дописать недостающие ключи
            _flush_adguard(out, ql_indent, seen, enable)
            in_ql = False
            out.append(line)
            continue
        if in_ql:
            m = re.match(r"^(\s*)(enabled|file_enabled|interval)\s*:", line)
            if m and not stripped.startswith("#"):
                key = m.group(2)
                ql_indent = m.group(1) or "  "
                seen[key] = True
                if key == "interval":
                    out.append(f"{ql_indent}interval: 24h" if enable else line)
                else:
                    out.append(f"{ql_indent}{key}: {'true' if enable else 'false'}")
                continue
        out.append(line)
    if in_ql:
        _flush_adguard(out, ql_indent, seen, enable)
    elif "querylog:" not in text:
        out.append("querylog:")
        _flush_adguard(out, "  ", {"enabled": False, "file_enabled": False, "interval": False}, enable)
    return "\n".join(out)


def _flush_adguard(out: list, indent: str, seen: dict, enable: bool) -> None:
    if not seen["enabled"]:
        out.append(f"{indent}enabled: {'true' if enable else 'false'}")
        seen["enabled"] = True
    if not seen["file_enabled"]:
        out.append(f"{indent}file_enabled: {'true' if enable else 'false'}")
        seen["file_enabled"] = True
    if enable and not seen["interval"]:
        out.append(f"{indent}interval: 24h")
        seen["interval"] = True


def adguard_enabled(text: str) -> bool:
    in_ql = False
    enabled = file_en = None
    for line in text.splitlines():
        if re.match(r"^querylog\s*:", line):
            in_ql = True
            continue
        if in_ql and line.strip() and not line.startswith((" ", "\t")) and not line.strip().startswith("#"):
            break
        if in_ql and not line.lstrip().startswith("#"):
            m = re.match(r"^\s*enabled\s*:\s*(\S+)", line)
            if m:
                enabled = m.group(1).strip().lower()
            m = re.match(r"^\s*file_enabled\s*:\s*(\S+)", line)
            if m:
                file_en = m.group(1).strip().lower()
    return enabled in {"true", "yes"} and file_en in {None, "true", "yes"}  # file_enabled может отсутствовать


def assert_safe_dns_name(name: str) -> str:
    if not DNS_NAME_RE.fullmatch(name):
        raise ValueError("некорректное имя DNS-контейнера")
    if _FORBIDDEN_TOUCH.search(name) and "dns" not in name.lower():
        raise ValueError(f"отказано: {name} не похож на DNS-контейнер")
    return name


def parse_container_names(docker_ps: str) -> list:
    import amnezia_monitor as am
    return am.parse_docker_ps(docker_ps)


class Remote:
    """Узкий интерфейс к серверу. Реализации: SSHRemote и тестовая память."""

    def run(self, cmd: str, timeout: int = 60) -> tuple:  # (rc, text)
        raise NotImplementedError

    def put(self, data: bytes, path: str, mode: int = 0o644) -> None:
        raise NotImplementedError

    def close(self):
        pass


class SSHRemote(Remote):
    def __init__(self, srv, runner=None):
        import amnezia_monitor as am
        self._own = runner is None
        self.runner = runner or am.SSHRunner(srv)
        self.client = self.runner.client

    def run(self, cmd: str, timeout: int = 60) -> tuple:
        _, out, err = self.client.exec_command(cmd, timeout=timeout)
        rc = out.channel.recv_exit_status()
        text = out.read().decode("utf-8", "replace") + err.read().decode("utf-8", "replace")
        return rc, text

    def put(self, data: bytes, path: str, mode: int = 0o644) -> None:
        sftp = self.client.open_sftp()
        try:
            with sftp.file(path, "wb") as fh:
                fh.write(data)
            sftp.chmod(path, mode)
        finally:
            sftp.close()

    def close(self):
        if self._own:
            try:
                self.runner.close()
            except Exception:
                pass


def connect(srv, runner_factory=None) -> Remote:
    runner = runner_factory(srv) if runner_factory else None
    if runner is not None and not hasattr(runner, "client"):
        # тестовый runner без настоящего SSH: если это уже Remote — вернём его
        if isinstance(runner, Remote):
            return runner
        raise TypeError("для настройки журнала нужен SSH с возможностью записи файлов")
    return SSHRemote(srv, runner)


def _dcmd(docker_cmd: str, rest: str) -> str:
    d = docker_cmd if docker_cmd in ("docker", "sudo -n docker") else "docker"
    return f"{d} {rest}"


def _pick_dns(containers: list) -> Optional[dict]:
    ups = [c for c in containers if c.get("type") == "dns"]
    if not ups:
        ups = [c for c in containers if "dns" in (c.get("name") or "").lower()
               or "adguard" in (c.get("name") or "").lower()]
    running = [c for c in ups if c.get("up")]
    return (running or ups or [None])[0]


def inspect(remote: Remote, docker_cmd: str = "docker") -> dict:
    """Состояние журнала без изменений. В ответе нет доменов и адресов клиентов."""
    info = {
        "ok": True, "status": "no_container", "mode": None, "enabled": False,
        "timer": False, "parseable": 0, "container": None, "message": "",
        "lines": [], "cascade_xray": None,
    }
    rc, ps = remote.run(_dcmd(docker_cmd, "ps -a --format '{{.Names}}|{{.Status}}|{{.Image}}'"))
    if rc != 0:
        info["ok"] = False
        info["message"] = "docker ps не выполнен (нужны права на Docker)"
        return info
    containers = parse_container_names(ps)
    dns = _pick_dns(containers)
    if not dns:
        info["message"] = "На сервере нет контейнера amnezia-dns, журнал DNS включить негде."
        return info
    name = assert_safe_dns_name(dns["name"])
    info["container"] = name
    if not dns.get("up"):
        info["status"] = "container_down"
        info["ok"] = False
        info["message"] = f"Контейнер {name} остановлен — сначала запустите его в Amnezia."
        return info

    rc, conf = remote.run(_dcmd(docker_cmd, f"exec {name} sh -c 'cat {UNBOUND_CONF} 2>/dev/null'"))
    if rc == 0 and "server:" in conf and "log-queries" in conf:
        info["mode"] = "unbound"
        info["enabled"] = unbound_enabled(conf)
    else:
        yaml_text = ""
        yaml_path = None
        for path in ADGUARD_YAML_CANDIDATES:
            rc, yaml_text = remote.run(_dcmd(docker_cmd, f"exec {name} sh -c 'cat {path} 2>/dev/null'"))
            if rc == 0 and yaml_text.strip():
                yaml_path = path
                break
        if yaml_path:
            info["mode"] = "adguard"
            info["enabled"] = adguard_enabled(yaml_text)
        else:
            info["status"] = "unknown_dns"
            info["ok"] = False
            info["message"] = f"В {name} не найден ни Unbound, ни AdGuard Home."
            return info

    rc, tstate = remote.run("systemctl is-active amnezia-dns-log-cleanup.timer")
    info["timer"] = (rc == 0 and tstate.strip() == "active")

    # Считаем разбираемые строки, сами домены наружу не отдаём.
    log_cmd = (
        f"exec {name} sh -c '"
        f"tail -c 400000 {UNBOUND_LOG} 2>/dev/null; "
        f"ls -1t {UNBOUND_ARCH}/unbound-*.log 2>/dev/null | head -3 | while read f; do tail -c 80000 \"$f\"; done; "
        f"tail -c 200000 {ADGUARD_QUERYLOG} 2>/dev/null'"
    )
    _rc, log_text = remote.run(_dcmd(docker_cmd, log_cmd), timeout=45)
    try:
        import amnezia_monitor as am
        info["parseable"] = len(am.parse_dns_log(log_text or ""))
    except Exception:
        info["parseable"] = 0

    rc, xr = remote.run("systemctl is-active cascade-xray.service")
    if rc == 0 and xr.strip() == "active":
        info["cascade_xray"] = "active"
    elif xr.strip():
        info["cascade_xray"] = xr.strip().splitlines()[0][:40]

    if info["enabled"] and info["parseable"] > 0:
        info["status"] = "ok"
        info["message"] = (
            f"Журнал включён ({'Unbound' if info['mode']=='unbound' else 'AdGuard'}), "
            f"в выборке {info['parseable']} разбираемых запросов"
            + (", таймер очистки 24 ч / 50 МБ активен" if info["timer"] else ", таймер очистки не активен")
            + "."
        )
    elif info["enabled"]:
        info["status"] = "enabled_empty"
        info["message"] = (
            "Журнал включён, но пока нет разбираемых записей. "
            "Появятся, когда устройства начнут спрашивать DNS через этот сервер."
        )
    else:
        info["status"] = "no_log"
        info["message"] = "DNS-журнал сейчас выключен."
    return info


def _restart_dns_only(remote: Remote, docker_cmd: str, name: str, logs: list) -> None:
    assert_safe_dns_name(name)
    rc, out = remote.run(_dcmd(docker_cmd, f"restart {name}"), timeout=90)
    if rc != 0:
        raise RuntimeError(f"не удалось перезапустить {name}: {(out or '')[:200]}")
    logs.append(f"перезапущен только {name}")
    healthy = False
    status_line = ""
    for _ in range(20):
        time.sleep(2)
        rc, st = remote.run(
            _dcmd(docker_cmd, f"inspect -f '{{{{.State.Status}}}} {{{{if .State.Health}}}}{{{{.State.Health.Status}}}}{{{{end}}}}' {name}")
        )
        status_line = (st or "").strip()
        if rc == 0 and "running" in status_line and "unhealthy" not in status_line and "starting" not in status_line:
            healthy = True
            break
    if not healthy:
        rc, st = remote.run(_dcmd(docker_cmd, f"inspect -f '{{{{.State.Status}}}}' {name}"))
        if rc != 0 or "running" not in (st or ""):
            raise RuntimeError(f"{name} не запустился после перезапуска")
        logs.append(f"{name} запущен, healthcheck ещё не зелёный")
    else:
        logs.append(f"{name} снова в работе")


def _install_cleanup(remote: Remote, logs: list, enable: bool) -> None:
    assets = load_cleanup_assets()
    if enable:
        remote.put(assets["script"].encode("utf-8"), CLEANUP_SCRIPT_REMOTE, 0o700)
        remote.put(assets["service"].encode("utf-8"), CLEANUP_SERVICE_REMOTE, 0o644)
        remote.put(assets["timer"].encode("utf-8"), CLEANUP_TIMER_REMOTE, 0o644)
        rc, out = remote.run("systemctl daemon-reload")
        if rc != 0:
            raise RuntimeError("systemctl daemon-reload не удался: " + (out or "")[:160])
        rc, out = remote.run("systemctl enable --now amnezia-dns-log-cleanup.timer")
        if rc != 0:
            raise RuntimeError("не удалось включить таймер очистки: " + (out or "")[:160])
        logs.append("таймер очистки: каждые 15 мин, хранение 24 ч, потолок 50 МБ")
        # один прогон создаёт каталог архива и права на файл журнала
        remote.run(CLEANUP_SCRIPT_REMOTE, timeout=90)
    else:
        remote.run("systemctl disable --now amnezia-dns-log-cleanup.timer")
        logs.append("таймер очистки остановлен")


def _apply_unbound(remote: Remote, docker_cmd: str, name: str, enable: bool, logs: list) -> bool:
    """Пишет конфиг. Возвращает True, если нужно перезапустить контейнер."""
    rc, conf = remote.run(_dcmd(docker_cmd, f"exec {name} sh -c 'cat {UNBOUND_CONF}'"))
    if rc != 0 or not conf.strip():
        raise RuntimeError("не удалось прочитать unbound.conf")
    patched = patch_unbound_conf(conf, enable)
    changed = patched != conf
    if changed:
        # резервная копия только при первом включении
        remote.run(
            _dcmd(docker_cmd,
                  f"exec {name} sh -c 'if [ ! -f {UNBOUND_BAK} ]; then "
                  f"cp {UNBOUND_CONF} {UNBOUND_BAK}; fi'")
        )
        tmp = "/tmp/amnezia-unbound.conf"
        remote.put(patched.encode("utf-8"), tmp, 0o644)
        rc, out = remote.run(_dcmd(docker_cmd, f"cp {tmp} {name}:{UNBOUND_CONF}"))
        remote.run(f"rm -f {tmp}")
        if rc != 0:
            raise RuntimeError("не удалось записать unbound.conf: " + (out or "")[:160])
        logs.append("Unbound: log-queries " + ("yes, logfile unbound.log" if enable else "no, logfile /dev/null"))
    else:
        logs.append("конфиг Unbound уже в нужном состоянии")
    return changed


def _apply_adguard(remote: Remote, docker_cmd: str, name: str, enable: bool, logs: list) -> bool:
    path = None
    text = ""
    for cand in ADGUARD_YAML_CANDIDATES:
        rc, text = remote.run(_dcmd(docker_cmd, f"exec {name} sh -c 'cat {cand} 2>/dev/null'"))
        if rc == 0 and text.strip():
            path = cand
            break
    if not path:
        raise RuntimeError("не найден AdGuardHome.yaml")
    patched = patch_adguard_yaml(text, enable)
    if patched == text:
        logs.append("конфиг AdGuard уже в нужном состоянии")
        return False
    tmp = "/tmp/amnezia-adguard.yaml"
    remote.put(patched.encode("utf-8"), tmp, 0o644)
    rc, out = remote.run(_dcmd(docker_cmd, f"cp {tmp} {name}:{path}"))
    remote.run(f"rm -f {tmp}")
    if rc != 0:
        raise RuntimeError("не удалось записать AdGuardHome.yaml: " + (out or "")[:160])
    logs.append("AdGuard: журнал запросов " + ("включён, 24 ч" if enable else "выключен"))
    return True


def apply(remote: Remote, docker_cmd: str, action: str) -> dict:
    """action: enable | disable | check. Не трогает VPN и cascade-xray."""
    if action not in ("enable", "disable", "check"):
        return {"ok": False, "message": "неизвестное действие", "lines": []}
    if action == "check":
        return inspect(remote, docker_cmd)

    before = inspect(remote, docker_cmd)
    if not before.get("container"):
        before["ok"] = False
        return before
    if before.get("status") in {"container_down", "unknown_dns"}:
        return before

    name = before["container"]
    mode = before["mode"]
    logs = list(before.get("lines") or [])
    try:
        if mode == "unbound":
            changed = _apply_unbound(remote, docker_cmd, name, action == "enable", logs)
        elif mode == "adguard":
            changed = _apply_adguard(remote, docker_cmd, name, action == "enable", logs)
        else:
            return {**before, "ok": False, "message": "неизвестный тип DNS"}
        _install_cleanup(remote, logs, enable=(action == "enable"))
        if changed:
            _restart_dns_only(remote, docker_cmd, name, logs)
        else:
            logs.append("перезапуск не нужен")
        after = inspect(remote, docker_cmd)
        after["lines"] = logs
        if action == "enable":
            after["ok"] = bool(after.get("enabled"))
            if after["ok"] and after.get("status") == "enabled_empty":
                after["message"] = (
                    "Журнал включён (хранение 24 ч, потолок 50 МБ). "
                    "Записи появятся после DNS-запросов через этот сервер. "
                    "Каскад и VPN не трогались."
                )
            elif after["ok"]:
                after["message"] = (
                    f"Журнал включён: {after.get('parseable', 0)} разбираемых запросов, "
                    "хранение 24 ч, потолок 50 МБ. Каскад и VPN не трогались."
                )
            else:
                after["message"] = "Настройка выполнена, но журнал не выглядит включённым. " + (after.get("message") or "")
        else:
            after["ok"] = not after.get("enabled")
            after["message"] = "DNS-журнал выключен. Каскад и VPN не трогались." if after["ok"] else (
                "Не удалось выключить журнал: " + (after.get("message") or "")
            )
        return after
    except Exception as e:
        return {
            "ok": False,
            "status": before.get("status") or "error",
            "mode": mode,
            "enabled": before.get("enabled"),
            "timer": before.get("timer"),
            "parseable": before.get("parseable") or 0,
            "container": name,
            "cascade_xray": before.get("cascade_xray"),
            "message": f"ошибка настройки: {type(e).__name__}: {e}",
            "lines": logs,
        }


def run_for_server(srv, action: str, runner_factory=None, remote: Remote = None) -> dict:
    own = remote is None
    try:
        if remote is None:
            remote = connect(srv, runner_factory)
        return apply(remote, getattr(srv, "docker_cmd", None) or "docker", action)
    except Exception as e:
        name = type(e).__name__
        if name in ("AuthenticationException", "BadAuthenticationType"):
            msg = "SSH отклонил вход: неверный пользователь, пароль или ключ"
        elif name in ("NoValidConnectionsError",) or isinstance(e, ConnectionRefusedError):
            msg = "соединение отклонено"
        elif isinstance(e, (TimeoutError, OSError)):
            msg = "не удалось подключиться по SSH"
        else:
            msg = f"{name}: {e}"
        return {"ok": False, "message": msg, "lines": []}
    finally:
        if own and remote is not None:
            try:
                remote.close()
            except Exception:
                pass
