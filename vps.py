import docker, time, re, subprocess

client = docker.from_env()
IMAGE = "ubuntu:22.04"

# 32GB RAM, 4 cores, 80GB disk
MEM = "32g"
CPUS = 4.0
DISK = "80G"

def create_vps(username, log_cb):
    log_cb("[+] Allocating resources...")
    time.sleep(0.5)
    log_cb(f"[+] Requesting {MEM} RAM, {int(CPUS)} vCPU, {DISK} disk")
    time.sleep(0.5)
    log_cb("[+] Pulling base image...")

    name = f"vps_{username}_{int(time.time())}"
    try:
        container = client.containers.run(
            IMAGE,
            name=name,
            detach=True,
            tty=True,
            stdin_open=True,
            mem_limit=MEM,
            nano_cpus=int(CPUS * 1e9),
            storage_opt={"size": DISK} if _supports_storage_opt() else None,
            command="/bin/bash",
            cap_add=["NET_ADMIN"],
        )
    except TypeError:
        container = client.containers.run(
            IMAGE, name=name, detach=True, tty=True, stdin_open=True,
            mem_limit=MEM, nano_cpus=int(CPUS * 1e9), command="/bin/bash",
        )

    log_cb(f"[+] Container {container.short_id} created")
    log_cb("[+] Installing tmate...")
    container.exec_run("apt-get update -qq", tty=True)
    container.exec_run("apt-get install -y tmate openssh-client -qq", tty=True)
    log_cb("[+] Starting tmate session...")

    container.exec_run("tmate -F -S /tmp/tmate.sock new-session -d", detach=True)
    time.sleep(4)
    container.exec_run("tmate -S /tmp/tmate.sock wait tmate-ready")
    result = container.exec_run("tmate -S /tmp/tmate.sock display -p '#{tmate_ssh}'")
    ssh = result.output.decode().strip()

    log_cb(f"[+] SSH ready: {ssh}")
    log_cb("[+] Deployment complete.")
    return container.id, ssh

def _supports_storage_opt():
    try:
        info = client.info()
        return info.get("Driver") in ("overlay2", "btrfs", "zfs", "devicemapper")
    except Exception:
        return False

def stats(container_id):
    try:
        c = client.containers.get(container_id)
        if c.status != "running":
            return {"status": c.status, "cpu": 0, "ram": 0, "uptime": 0}
        s = c.stats(stream=False)
        cpu_delta = s["cpu_stats"]["cpu_usage"]["total_usage"] - s["precpu_stats"]["cpu_usage"]["total_usage"]
        sys_delta = s["cpu_stats"].get("system_cpu_usage", 0) - s["precpu_stats"].get("system_cpu_usage", 0)
        cpu_pct = 0.0
        if sys_delta > 0 and cpu_delta > 0:
            n = len(s["cpu_stats"]["cpu_usage"].get("percpu_usage", [1]))
            cpu_pct = (cpu_delta / sys_delta) * n * 100.0
        ram_used = s["memory_stats"].get("usage", 0) / (1024 * 1024)
        ram_lim = s["memory_stats"].get("limit", 1) / (1024 * 1024)
        started = c.attrs["State"]["StartedAt"]
        return {
            "status": "running",
            "cpu": round(cpu_pct, 2),
            "ram": round(ram_used, 1),
            "ram_limit": round(ram_lim, 1),
            "uptime": started,
        }
    except Exception as e:
        return {"status": "error", "cpu": 0, "ram": 0, "uptime": 0, "error": str(e)}

def suspend(container_id):
    try:
        c = client.containers.get(container_id)
        c.pause()
        return True
    except Exception:
        return False

def resume(container_id):
    try:
        c = client.containers.get(container_id)
        c.unpause()
        return True
    except Exception:
        return False

def destroy(container_id):
    try:
        c = client.containers.get(container_id)
        c.remove(force=True)
        return True
    except Exception:
        return False
