"""Deteção automática de servidores de bases de dados (só com autorização do cliente).

Âmbitos:
  LOCAL  este computador: serviços do Windows, instâncias do SQL Server no registo
         (com a porta TCP configurada), portas habituais em 127.0.0.1 e o SQL Server
         Browser local.
  REDE   a rede local: pedido ao SQL Server Browser (UDP 1434, o mesmo que o SSMS usa
         para listar servidores) e verificação das portas habituais de bases de dados,
         limitada a redes privadas até /24 e com tempos curtos.

Nada é lido das bases de dados nesta fase: só se descobre ONDE estão os servidores.
"""

from __future__ import annotations

import ipaddress
import re
import socket
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

DEFAULT_PORTS = {"mssql": 1433, "postgresql": 5432, "mysql": 3306, "oracle": 1521, "firebird": 3050}
ENGINE_LABELS = {"mssql": "SQL Server", "postgresql": "PostgreSQL", "mysql": "MySQL", "mariadb": "MariaDB",
                 "oracle": "Oracle", "firebird": "Firebird"}

# Nomes de serviços do Windows -> motor. Padrões públicos dos instaladores de cada SGBD.
SERVICE_PATTERNS = [
    (re.compile(r"^MSSQLSERVER$", re.I), "mssql"),
    (re.compile(r"^MSSQL\$(?P<instance>.+)$", re.I), "mssql"),
    (re.compile(r"^postgresql", re.I), "postgresql"),
    (re.compile(r"^MariaDB", re.I), "mariadb"),
    (re.compile(r"^MySQL", re.I), "mysql"),
    (re.compile(r"^OracleService(?P<instance>.+)$", re.I), "oracle"),
    (re.compile(r"^Firebird", re.I), "firebird"),
]

MAX_NETWORK_HOSTS = 254
PORT_TIMEOUT = 0.4
BROWSER_TIMEOUT = 1.5


class DiscoveryError(Exception):
    pass


@dataclass
class DetectedServer:
    engine: str
    host: str
    port: int | None = None
    instance: str = ""
    version: str = ""
    running: bool | None = None
    tcp_enabled: bool | None = None
    evidence: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return ENGINE_LABELS.get(self.engine, self.engine)

    @property
    def server_name(self) -> str:
        """Nome a usar na ligação (SQL Server com instância: HOST\\INSTANCIA)."""
        if self.engine == "mssql" and self.instance and self.instance.upper() != "MSSQLSERVER":
            return f"{self.host}\\{self.instance}"
        return self.host

    @property
    def key(self) -> tuple:
        host = "localhost" if self.host.lower() in ("localhost", "127.0.0.1", "::1", "(local)", ".") else self.host.lower()
        if self.engine == "mssql":
            # SQL Server identifica-se pela instância (a principal chama-se MSSQLSERVER).
            return ("mssql", host, (self.instance or "MSSQLSERVER").upper())
        # MySQL e MariaDB partilham protocolo e porta: o mesmo servidor visto de duas formas.
        family = "mysql" if self.engine == "mariadb" else self.engine
        return (family, host, self.port)

    def merge(self, other: "DetectedServer") -> None:
        for attr in ("port", "version", "running", "tcp_enabled"):
            if getattr(self, attr) in (None, "") and getattr(other, attr) not in (None, ""):
                setattr(self, attr, getattr(other, attr))
        self.evidence += [e for e in other.evidence if e not in self.evidence]


@dataclass
class DiscoveryResult:
    servers: list[DetectedServer] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    scanned_hosts: int = 0

    def add(self, server: DetectedServer) -> None:
        for existing in self.servers:
            if existing.key == server.key:
                existing.merge(server)
                return
        self.servers.append(server)


# --------------------------------------------------------------- local


def _run(cmd: list[str], timeout: float = 15) -> str:
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return done.stdout or ""
    except (OSError, subprocess.SubprocessError):
        return ""


def windows_services() -> list[tuple[str, str, bool]]:
    """(nome, nome visível, a correr?) dos serviços do Windows."""
    if not sys.platform.startswith("win"):
        return []
    output = _run(["sc", "query", "type=", "service", "state=", "all"])
    services, name, display = [], None, ""
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("SERVICE_NAME:"):
            name = line.split(":", 1)[1].strip()
            display = ""
        elif line.startswith("DISPLAY_NAME:"):
            display = line.split(":", 1)[1].strip()
        elif line.startswith("STATE") and name:
            services.append((name, display, "RUNNING" in line.upper()))
            name = None
    return services


def classify_service(name: str) -> tuple[str, str] | None:
    for pattern, engine in SERVICE_PATTERNS:
        match = pattern.match(name)
        if match:
            return engine, (match.groupdict().get("instance") or "")
    return None


def sqlserver_registry_instances() -> list[dict]:
    """Instâncias SQL Server no registo, com a porta TCP configurada e se o TCP está ativo."""
    try:
        import winreg
    except ImportError:
        return []
    instances = []
    base = r"SOFTWARE\Microsoft\Microsoft SQL Server"
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base + r"\Instance Names\SQL") as key:
            index = 0
            while True:
                try:
                    name, internal, _ = winreg.EnumValue(key, index)
                except OSError:
                    break
                index += 1
                info = {"instance": name, "internal": internal, "port": None, "tcp_enabled": None, "version": ""}
                tcp = rf"{base}\{internal}\MSSQLServer\SuperSocketNetLib\Tcp"
                info["tcp_enabled"] = _reg_value(winreg, tcp, "Enabled") in (1, "1")
                port = _reg_value(winreg, tcp + r"\IPAll", "TcpPort") or _reg_value(winreg, tcp + r"\IPAll", "TcpDynamicPorts")
                info["port"] = _first_port(port)
                info["version"] = str(_reg_value(winreg, rf"{base}\{internal}\Setup", "Version") or "")
                instances.append(info)
    except OSError:
        pass
    return instances


def _reg_value(winreg, path, name):
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as key:
            return winreg.QueryValueEx(key, name)[0]
    except OSError:
        return None


def _first_port(value) -> int | None:
    for part in str(value or "").replace(";", ",").split(","):
        part = part.strip()
        if part.isdigit() and 0 < int(part) < 65536:
            return int(part)
    return None


def port_open(host: str, port: int, timeout: float = PORT_TIMEOUT) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def discover_local() -> DiscoveryResult:
    result = DiscoveryResult()
    for name, display, running in windows_services():
        found = classify_service(name)
        if not found:
            continue
        engine, instance = found
        server = DetectedServer(engine, "localhost", instance=instance, running=running,
                                evidence=[f"serviço do Windows «{display or name}»" + (" (a correr)" if running else " (parado)")])
        if engine != "mssql":
            server.port = DEFAULT_PORTS.get("mysql" if engine == "mariadb" else engine)
        result.add(server)

    for info in sqlserver_registry_instances():
        result.add(DetectedServer(
            "mssql", "localhost", port=info["port"], instance=info["instance"], version=info["version"],
            tcp_enabled=info["tcp_enabled"], evidence=["instância registada no Windows"],
        ))

    for engine, port in DEFAULT_PORTS.items():
        if port_open("127.0.0.1", port):
            existing = [s for s in result.servers if s.engine in (engine, "mariadb" if engine == "mysql" else engine)
                        and (s.port == port or s.port is None)]
            if existing:
                existing[0].port = existing[0].port or port
                existing[0].evidence.append(f"porta {port} aberta")
            else:
                result.add(DetectedServer(engine, "localhost", port=port, evidence=[f"porta {port} aberta"]))

    for server in sqlserver_browser("127.0.0.1"):
        server.host = "localhost"
        result.add(server)
    return result


# ------------------------------------------------------- SQL Server Browser


def parse_browser_response(data: bytes, address: str) -> list[DetectedServer]:
    """Resposta SSRP: 0x05 + tamanho (2 bytes) + "ServerName;X;InstanceName;Y;...;tcp;1433;;"."""
    if len(data) < 3 or data[0] != 0x05:
        return []
    text = data[3:].decode("latin-1", errors="replace")
    servers = []
    for chunk in text.split(";;"):
        parts = chunk.split(";")
        if len(parts) < 2:
            continue
        values = {parts[i].lower(): parts[i + 1] for i in range(0, len(parts) - 1, 2)}
        if "instancename" not in values:
            continue
        port = _first_port(values.get("tcp"))
        servers.append(DetectedServer(
            "mssql", address, port=port, instance=values.get("instancename", ""), version=values.get("version", ""),
            tcp_enabled=port is not None, evidence=["SQL Server Browser"],
        ))
    return servers


def sqlserver_browser(target: str, timeout: float = BROWSER_TIMEOUT, broadcast: bool = False) -> list[DetectedServer]:
    found: list[DetectedServer] = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            if broadcast:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.settimeout(timeout)
            sock.sendto(b"\x02", (target, 1434))
            while True:
                try:
                    data, (address, _) = sock.recvfrom(65535)
                except (socket.timeout, OSError):
                    break
                found += parse_browser_response(data, address)
                if not broadcast:
                    break
    except OSError:
        pass
    return found


# ----------------------------------------------------------------- rede


def local_networks() -> list[ipaddress.IPv4Network]:
    """Redes privadas deste computador, limitadas a /24."""
    addresses = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addresses.add(info[4][0])
    except OSError:
        pass
    try:  # endereço usado para sair para a rede (sem enviar nada)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("10.255.255.255", 1))
            addresses.add(probe.getsockname()[0])
    except OSError:
        pass
    return networks_for(addresses)


def networks_for(addresses) -> list[ipaddress.IPv4Network]:
    networks = []
    for address in addresses:
        try:
            ip = ipaddress.IPv4Address(address)
        except ValueError:
            continue
        if not ip.is_private or ip.is_loopback or ip.is_link_local:
            continue
        network = ipaddress.IPv4Network(f"{ip}/24", strict=False)
        if network not in networks:
            networks.append(network)
    return networks


def validate_network(value: str) -> ipaddress.IPv4Network:
    try:
        network = ipaddress.IPv4Network(value.strip(), strict=False)
    except ValueError as exc:
        raise DiscoveryError(f"Rede inválida: {value!r} (ex.: 192.168.1.0/24).") from exc
    if not network.is_private:
        raise DiscoveryError("Só é permitido procurar em redes privadas (ex.: 192.168.x.x, 10.x.x.x).")
    if network.num_addresses > MAX_NETWORK_HOSTS + 2:
        raise DiscoveryError("Rede demasiado grande: o máximo é /24 (254 endereços).")
    return network


def discover_network(networks: list[ipaddress.IPv4Network], *, ports=None, timeout: float = PORT_TIMEOUT,
                     own_addresses=None) -> DiscoveryResult:
    result = DiscoveryResult()
    ports = ports or DEFAULT_PORTS
    own = set(own_addresses or ())
    hosts = []
    for network in networks:
        network = validate_network(str(network))
        hosts += [str(h) for h in network.hosts() if str(h) not in own]
        for server in sqlserver_browser(str(network.broadcast_address), broadcast=True):
            result.add(server)
    hosts = hosts[: MAX_NETWORK_HOSTS * 2]
    result.scanned_hosts = len(hosts)
    jobs = [(host, engine, port) for host in hosts for engine, port in ports.items()]

    def probe(job):
        host, engine, port = job
        return job if port_open(host, port, timeout) else None

    with ThreadPoolExecutor(max_workers=64) as pool:
        for job in filter(None, pool.map(probe, jobs)):
            host, engine, port = job
            result.add(DetectedServer(engine, host, port=port, evidence=[f"porta {port} aberta na rede"]))
    return result
